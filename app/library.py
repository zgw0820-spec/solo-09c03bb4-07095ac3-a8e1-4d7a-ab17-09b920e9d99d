"""在库目录的登记、查询、外送审批与持久化。

职责
----
* 登记一份在库目录（沿用排程请求格式）和灾备装带次数上限；
* 审批“外送一盘在库磁带”的申请：扣除该盘后，**无故障**以及**库内再坏任意
  一盘**的每个场景，都必须能在上限次装带内恢复全部文件，否则拒绝；
* 批准即把该盘从在库目录移除并原子落盘——重启后状态从磁盘恢复，已外送的
  磁带不会被“算回来”；拒绝则不改动任何状态；
* 审批的“试算 + 落盘 + 生效”在同一把锁内完成，并发审批不会凭过时目录放行；
  查询只读当前已生效的快照，试算期间照常可用。

持久化
------
状态以 JSON 单文件保存：先写临时文件并 fsync，再 ``os.replace`` 原子替换，
最后 fsync 目录，保证崩溃时要么看到旧状态、要么看到新状态，不会看到半个
文件。状态文件损坏时启动直接报错，绝不静默重置（否则等于把已外送的磁带
算回在库）。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .models import (
    ApproveResponse,
    FileIn,
    LibraryRegistration,
    LibraryStatus,
    ScenarioResult,
    ScheduleRequest,
    SentTapeRecord,
)
from .solver import solve

STATE_VERSION = 1
DEFAULT_STATE_PATH = Path("data/library_state.json")


class LibraryError(Exception):
    """审批/登记领域的业务错误基类。"""


class NotRegisteredError(LibraryError):
    """尚未登记在库目录。"""


class UnknownTapeError(LibraryError):
    """申请外送的磁带不在在库目录中。"""


class LibraryState(LibraryRegistration):
    """落盘状态：登记内容 + 已外送审计记录。"""

    version: int = STATE_VERSION
    sent_tapes: list[SentTapeRecord] = []


# --------------------------------------------------------------------------- #
# 目录（dict 形式）上的纯函数
# --------------------------------------------------------------------------- #
def files_to_dicts(files: Sequence[FileIn]) -> list[dict]:
    """把校验过的目录转成普通字典，便于去带、落盘与重新校验。"""
    return [
        {
            "id": f.id,
            "shards": [
                {
                    "id": s.id,
                    "size": s.size,
                    "replicas": [
                        {"tape": r.tape, "position": r.position} for r in s.replicas
                    ],
                }
                for s in f.shards
            ],
        }
        for f in files
    ]


def tapes_in_catalog(files: Sequence[dict]) -> set[str]:
    """目录中出现的全部磁带带号。"""
    return {r["tape"] for f in files for s in f["shards"] for r in s["replicas"]}


def catalog_without_tape(files: Sequence[dict], tape: str) -> list[dict]:
    """去掉某盘磁带上的所有副本（分片可能因此缺失，由求解器判定）。"""
    return [
        {
            "id": f["id"],
            "shards": [
                {
                    "id": s["id"],
                    "size": s["size"],
                    "replicas": [r for r in s["replicas"] if r["tape"] != tape],
                }
                for s in f["shards"]
            ],
        }
        for f in files
    ]


def _run_scenario(
    capacity: float,
    objective: str,
    files: list[dict],
    failed_tape: Optional[str],
    limit: int,
) -> ScenarioResult:
    """核算一个场景：在给定目录下恢复全部文件，装带次数是否不超过上限。"""
    label = "无故障" if failed_tape is None else f"库内再坏一盘 {failed_tape}"
    req = ScheduleRequest.model_validate(
        {"staging_capacity": capacity, "objective": objective, "files": files}
    )
    res = solve(req)
    if not res.feasible:
        return ScenarioResult(
            failed_tape=failed_tape,
            ok=False,
            feasible=False,
            min_mounts=None,
            missing_shards=res.missing_shards,
            capacity_issues=res.capacity_issues,
            detail=f"场景[{label}]无解：{res.message}",
        )
    ok = res.min_mounts <= limit
    if ok:
        detail = f"场景[{label}]最少需要 {res.min_mounts} 次装带，不超过上限 {limit}"
    else:
        detail = (
            f"场景[{label}]最少需要 {res.min_mounts} 次装带，"
            f"超过灾备装带次数上限 {limit}"
        )
    return ScenarioResult(
        failed_tape=failed_tape,
        ok=ok,
        feasible=True,
        min_mounts=res.min_mounts,
        detail=detail,
    )


def evaluate_removal(state: LibraryState, tape: str) -> ApproveResponse:
    """试算外送 ``tape``：无故障 + 库内再坏任意一盘，全部满足上限才批准。"""
    limit = state.max_dr_mounts
    catalog = files_to_dicts(state.files)
    remaining_files = catalog_without_tape(catalog, tape)
    remaining_tapes = sorted(tapes_in_catalog(remaining_files))

    scenarios: list[ScenarioResult] = [
        _run_scenario(state.staging_capacity, state.objective, remaining_files, None, limit)
    ]
    for other in remaining_tapes:
        degraded = catalog_without_tape(remaining_files, other)
        scenarios.append(
            _run_scenario(state.staging_capacity, state.objective, degraded, other, limit)
        )

    failing = next((s for s in scenarios if not s.ok), None)
    approved = failing is None
    worst = max((s.min_mounts or 0) for s in scenarios) if approved else None

    if approved:
        message = (
            f"批准外送 {tape}：{len(scenarios)} 个场景均可在上限 {limit} 次装带内"
            f"恢复全部文件（最坏 {worst} 次），该盘已从在库目录移除"
        )
    else:
        message = f"拒绝外送 {tape}：{failing.detail}。在库目录未变。"

    return ApproveResponse(
        approved=approved,
        tape=tape,
        max_dr_mounts=limit,
        scenarios_checked=len(scenarios),
        worst_case_mounts=worst,
        failing_scenario=failing,
        scenarios=scenarios,
        remaining_tapes=(
            remaining_tapes if approved else sorted(tapes_in_catalog(catalog))
        ),
        message=message,
    )


class LibraryStore:
    """在库目录的线程安全存储：内存快照 + JSON 文件持久化。

    状态对象一经生效绝不原地修改，只整体替换，因此查询无需持锁；登记与
    审批在同一把锁内“基于最新状态决策并落盘”，保证并发审批不会凭过时
    目录放行。
    """

    def __init__(self, path: os.PathLike | str = DEFAULT_STATE_PATH):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._state: Optional[LibraryState] = None
        self._load()

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            state = LibraryState.model_validate(raw)
        except Exception as exc:
            raise RuntimeError(
                f"在库目录状态文件 {self._path} 无法解析（为避免把已外送磁带"
                f"算回在库，服务拒绝启动，请人工核对后修复或删除该文件）: {exc}"
            ) from exc
        if state.version != STATE_VERSION:
            raise RuntimeError(
                f"在库目录状态文件版本 {state.version} 不受支持（当前 {STATE_VERSION}）"
            )
        self._state = state

    def _persist_locked(self, state: LibraryState) -> None:
        """原子落盘：临时文件 + fsync + os.replace + 目录 fsync。调用方须持锁。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, prefix=self._path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(state.model_dump_json(indent=2))
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
            dir_fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ #
    # 查询（无需持锁：状态只整体替换，读到的总是某个已生效快照）
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Optional[LibraryState]:
        return self._state

    def schedule_request(self) -> ScheduleRequest:
        """当前在库目录，格式与 ``POST /schedule`` 的请求完全一致。"""
        state = self._state
        if state is None:
            raise NotRegisteredError("尚未登记在库目录，请先 PUT /library")
        return ScheduleRequest.model_validate(
            {
                "staging_capacity": state.staging_capacity,
                "objective": state.objective,
                "files": files_to_dicts(state.files),
            }
        )

    def status(self) -> LibraryStatus:
        state = self._state
        if state is None:
            return LibraryStatus(registered=False)
        catalog = files_to_dicts(state.files)
        return LibraryStatus(
            registered=True,
            staging_capacity=state.staging_capacity,
            max_dr_mounts=state.max_dr_mounts,
            tapes=sorted(tapes_in_catalog(catalog)),
            file_count=len(state.files),
            shard_count=sum(len(f.shards) for f in state.files),
            sent_tapes=list(state.sent_tapes),
        )

    # ------------------------------------------------------------------ #
    # 登记与审批（持锁，串行执行）
    # ------------------------------------------------------------------ #
    def register(self, registration: LibraryRegistration) -> LibraryStatus:
        """登记/重设在库目录与灾备装带上限；已外送记录随之重置。"""
        with self._lock:
            state = LibraryState(
                staging_capacity=registration.staging_capacity,
                objective=registration.objective,
                files=files_to_dicts(registration.files),
                max_dr_mounts=registration.max_dr_mounts,
                sent_tapes=[],
            )
            self._persist_locked(state)
            self._state = state
        return self.status()

    def approve(self, tape: str) -> ApproveResponse:
        """审批外送一盘在库磁带。

        试算、落盘、生效在同一把锁内完成：并发审批总是基于最新已生效的
        目录，不会凭过时快照放行。拒绝时不改动任何状态。
        """
        with self._lock:
            state = self._state
            if state is None:
                raise NotRegisteredError("尚未登记在库目录，请先 PUT /library")
            catalog = files_to_dicts(state.files)
            if tape not in tapes_in_catalog(catalog):
                raise UnknownTapeError(
                    f"磁带 {tape!r} 不在在库目录中"
                    f"（在库：{sorted(tapes_in_catalog(catalog))}）"
                )

            decision = evaluate_removal(state, tape)
            if not decision.approved:
                return decision  # 目录不变，也不落盘

            new_state = LibraryState(
                staging_capacity=state.staging_capacity,
                objective=state.objective,
                files=catalog_without_tape(catalog, tape),
                max_dr_mounts=state.max_dr_mounts,
                sent_tapes=[
                    *state.sent_tapes,
                    SentTapeRecord(
                        tape=tape,
                        approved_at=datetime.now(timezone.utc).isoformat(),
                        worst_case_mounts=decision.worst_case_mounts or 0,
                        remaining_tapes=list(decision.remaining_tapes),
                    ),
                ],
            )
            # 先落盘再生效：落盘失败则审批失败，内存状态保持原样，
            # 避免“内存里已移除、重启后又算回来”。
            self._persist_locked(new_state)
            self._state = new_state
            return decision
