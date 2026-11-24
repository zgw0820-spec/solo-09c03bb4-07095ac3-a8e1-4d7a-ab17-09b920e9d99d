"""Pydantic models for the tape-restore scheduler API.

Semantics
---------
* A *shard* is the unit that must be read exactly once.  It has a size (the
  amount of staging space it needs) and one or more *replicas*.
* A *replica* lives at a position on a tape.  While a tape is mounted the read
  head only moves forward, so the positions read during one mount must be
  non-decreasing.  Shards may be skipped.  Mounting a tape again (including
  rewinding the same tape) starts a new mount.
* Shards read but not yet delivered occupy the staging disk.  As soon as every
  shard of a file has been read the file is verified, delivered, and the space
  of all of its shards is released.  The peak usage therefore has to fit into
  the staging capacity.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TAPES = 6
MAX_SHARDS = 12
MAX_REPLICAS = 48


class ReplicaIn(BaseModel):
    """One physical copy of a shard at a position on a tape."""

    tape: str = Field(..., min_length=1, description="Tape identifier, e.g. T1")
    position: float = Field(
        ...,
        ge=0,
        description=(
            "Offset of the replica on the tape.  Reads within one mount must "
            "use non-decreasing positions (forward-only head, skipping allowed)."
        ),
    )


class ShardIn(BaseModel):
    """A piece of a file that has to be read exactly once."""

    id: str = Field(..., min_length=1, description="Shard id, unique inside its file")
    size: float = Field(..., gt=0, description="Staging space this shard occupies")
    replicas: list[ReplicaIn] = Field(
        default_factory=list,
        description="Copies of this shard; an empty list marks the shard as missing",
    )


class FileIn(BaseModel):
    """A logical file assembled from shards."""

    id: str = Field(..., min_length=1, description="File id, unique in the catalogue")
    shards: list[ShardIn] = Field(..., min_length=1)


DEFAULT_EXAMPLE = {
    "staging_capacity": 10,
    "files": [
        {
            "id": "file-A",
            "shards": [
                {
                    "id": "a1",
                    "size": 2,
                    "replicas": [{"tape": "T1", "position": 10}, {"tape": "T2", "position": 5}],
                },
                {
                    "id": "a2",
                    "size": 3,
                    "replicas": [{"tape": "T1", "position": 30}, {"tape": "T3", "position": 7}],
                },
            ],
        },
        {
            "id": "file-B",
            "shards": [
                {
                    "id": "b1",
                    "size": 4,
                    "replicas": [{"tape": "T1", "position": 20}, {"tape": "T2", "position": 9}],
                },
                {
                    "id": "b2",
                    "size": 1,
                    "replicas": [{"tape": "T2", "position": 20}],
                },
            ],
        },
    ],
}


class ScheduleRequest(BaseModel):
    """Staging-disk free space plus the catalogue of tapes."""

    model_config = ConfigDict(json_schema_extra={"examples": [DEFAULT_EXAMPLE]})

    staging_capacity: float = Field(
        ...,
        gt=0,
        description="Free space on the staging disk; pending shards must fit at all times",
    )
    files: list[FileIn] = Field(..., min_length=1)
    objective: Literal["min_mounts"] = Field(
        "min_mounts",
        description="Optimisation target (only minimum number of tape mounts)",
    )

    @model_validator(mode="after")
    def _check_limits(self) -> "ScheduleRequest":
        tapes: set[str] = set()
        shard_count = 0
        replica_count = 0
        file_ids: set[str] = set()
        for f in self.files:
            if f.id in file_ids:
                raise ValueError(f"duplicate file id: {f.id!r}")
            file_ids.add(f.id)
            shard_ids: set[str] = set()
            for s in f.shards:
                if s.id in shard_ids:
                    raise ValueError(f"duplicate shard id {s.id!r} in file {f.id!r}")
                shard_ids.add(s.id)
                shard_count += 1
                replica_count += len(s.replicas)
                for r in s.replicas:
                    tapes.add(r.tape)
        if shard_count > MAX_SHARDS:
            raise ValueError(
                f"at most {MAX_SHARDS} shards are supported, got {shard_count}"
            )
        if replica_count > MAX_REPLICAS:
            raise ValueError(
                f"at most {MAX_REPLICAS} replicas are supported, got {replica_count}"
            )
        if len(tapes) > MAX_TAPES:
            raise ValueError(
                f"at most {MAX_TAPES} tapes are supported, got {len(tapes)}"
            )
        return self


class ReadAction(BaseModel):
    """One forward read during a mount."""

    shard_id: str
    file_id: str
    tape: str
    position: float
    size: float
    delivers_file: Optional[str] = Field(
        None,
        description="File that becomes complete (verified + delivered) at this read",
    )


class MountStep(BaseModel):
    """One tape mount: ordered reads, resulting deliveries and staging state."""

    mount: int = Field(..., ge=1, description="1-based mount order")
    tape: str
    read_from: float = Field(..., description="Position of the first read in this mount")
    read_to: float = Field(..., description="Position of the last read in this mount")
    reads: list[ReadAction] = Field(
        ..., description="Reads in tape order (positions are non-decreasing)"
    )
    delivered_files: list[str] = Field(
        default_factory=list, description="Files delivered by the end of this mount"
    )
    staging_before: float = Field(
        ..., description="Staging usage when this mount starts"
    )
    staging_peak: float = Field(
        ..., description="Peak staging usage during this mount"
    )
    staging_after: float = Field(
        ..., description="Staging usage after the deliveries of this mount"
    )


class MissingShard(BaseModel):
    shard_id: str
    file_id: str
    reason: str


class CapacityIssue(BaseModel):
    file_id: str
    required: float
    capacity: float
    message: str


class Explanation(BaseModel):
    """Readable justification for the optimality claim."""

    lower_bound: int = Field(..., description="Set-cover lower bound on mounts")
    constructive_value: int = Field(..., description="Mounts in the returned schedule")
    lower_bound_achieved: bool = Field(
        ..., description="True when the cover lower bound was reached"
    )
    detail: str


class ScheduleResponse(BaseModel):
    feasible: bool
    min_mounts: Optional[int] = None
    mounts: list[MountStep] = Field(default_factory=list)
    files_restored: list[str] = Field(default_factory=list)
    total_bytes_read: Optional[float] = None
    missing_shards: list[MissingShard] = Field(default_factory=list)
    capacity_issues: list[CapacityIssue] = Field(default_factory=list)
    message: str = ""
    explanation: Optional[Explanation] = None


# --------------------------------------------------------------------------- #
# 磁带外送审批（在库目录登记 + 灾备装带上限）
# --------------------------------------------------------------------------- #

LIBRARY_EXAMPLE = {
    "staging_capacity": 8,
    "max_dr_mounts": 2,
    "files": [
        {
            "id": "file-A",
            "shards": [
                {
                    "id": "a1",
                    "size": 3,
                    "replicas": [
                        {"tape": "T1", "position": 10},
                        {"tape": "T2", "position": 10},
                        {"tape": "T3", "position": 10},
                    ],
                },
                {
                    "id": "a2",
                    "size": 3,
                    "replicas": [
                        {"tape": "T1", "position": 30},
                        {"tape": "T2", "position": 30},
                        {"tape": "T3", "position": 30},
                    ],
                },
            ],
        },
        {
            "id": "file-B",
            "shards": [
                {
                    "id": "b1",
                    "size": 3,
                    "replicas": [
                        {"tape": "T1", "position": 20},
                        {"tape": "T2", "position": 20},
                        {"tape": "T3", "position": 20},
                    ],
                },
                {
                    "id": "b2",
                    "size": 3,
                    "replicas": [
                        {"tape": "T1", "position": 40},
                        {"tape": "T2", "position": 40},
                        {"tape": "T3", "position": 40},
                    ],
                },
            ],
        },
    ],
}


class LibraryRegistration(ScheduleRequest):
    """登记在库目录：沿用排程请求格式，另加灾备装带次数上限。"""

    model_config = ConfigDict(json_schema_extra={"examples": [LIBRARY_EXAMPLE]})

    max_dr_mounts: int = Field(
        ...,
        ge=1,
        description=(
            "灾备装带次数上限：外送一盘磁带后，无论无故障还是库内再坏任意一盘，"
            "恢复全部文件所需的装带次数都不得超过该值"
        ),
    )


class ApproveRequest(BaseModel):
    """一盘在库磁带的外送申请。"""

    model_config = ConfigDict(json_schema_extra={"examples": [{"tape": "T3"}]})

    tape: str = Field(..., min_length=1, description="申请外送的在库磁带带号")


class ScenarioResult(BaseModel):
    """一个灾备场景的核算结果（无故障，或库内再坏某一盘）。"""

    failed_tape: Optional[str] = Field(
        None, description="假设故障的在库磁带；null 表示无故障场景"
    )
    ok: bool = Field(..., description="该场景是否满足灾备要求")
    feasible: bool = Field(..., description="该场景下能否恢复全部文件")
    min_mounts: Optional[int] = Field(
        None, description="恢复全部文件所需的最少装带次数；无解时为 null"
    )
    missing_shards: list[MissingShard] = Field(default_factory=list)
    capacity_issues: list[CapacityIssue] = Field(default_factory=list)
    detail: str = ""


class ApproveResponse(BaseModel):
    """审批结论：批准则目录已更新并落盘，拒绝则目录不变。"""

    approved: bool
    tape: str = Field(..., description="申请外送的磁带")
    max_dr_mounts: int = Field(..., description="当前生效的灾备装带次数上限")
    scenarios_checked: int = Field(..., description="核算的场景总数（无故障 + 各单盘故障）")
    worst_case_mounts: Optional[int] = Field(
        None, description="所有场景中最坏的最少装带次数（全部可行时给出）"
    )
    failing_scenario: Optional[ScenarioResult] = Field(
        None, description="拒绝时给出一个未通过的场景及其原因"
    )
    scenarios: list[ScenarioResult] = Field(
        default_factory=list, description="全部场景的核算明细"
    )
    remaining_tapes: list[str] = Field(
        default_factory=list, description="审批生效后在库磁带列表（拒绝时为当前在库列表）"
    )
    message: str = ""


class SentTapeRecord(BaseModel):
    """一盘已获准外送磁带的审计记录。"""

    tape: str
    approved_at: str = Field(..., description="UTC ISO-8601 时间戳")
    worst_case_mounts: int = Field(..., description="批准时所有场景中最坏的装带次数")
    remaining_tapes: list[str] = Field(default_factory=list)


class LibraryStatus(BaseModel):
    """在库目录概览与已外送记录。"""

    registered: bool
    staging_capacity: Optional[float] = None
    max_dr_mounts: Optional[int] = None
    tapes: list[str] = Field(default_factory=list, description="当前在库磁带")
    file_count: int = 0
    shard_count: int = 0
    sent_tapes: list[SentTapeRecord] = Field(default_factory=list)
