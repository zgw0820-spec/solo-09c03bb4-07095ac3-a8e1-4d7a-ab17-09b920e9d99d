"""磁带外送审批 API 的测试。

覆盖：登记/查询回环、批准、两类拒绝（故障场景无解、超装带上限）、拒绝后
目录不变、持久化（重启不把已外送磁带算回来）、顺序与并发交错审批、以及
试算期间查询不阻塞。
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.library as library_module
from app.library import LibraryStore
from app.main import app
from app.models import LIBRARY_EXAMPLE, ScheduleRequest

client = TestClient(app)


@pytest.fixture()
def store(tmp_path):
    """给每个测试一个独立的存储文件，并挂到 app 上。"""
    s = LibraryStore(tmp_path / "library_state.json")
    old = app.state.store
    app.state.store = s
    try:
        yield s
    finally:
        app.state.store = old


def register_example() -> None:
    response = client.put("/library", json=LIBRARY_EXAMPLE)
    assert response.status_code == 200, response.text


def tapes_in(payload: dict) -> set[str]:
    return {
        r["tape"]
        for f in payload["files"]
        for s in f["shards"]
        for r in s["replicas"]
    }


# --------------------------------------------------------------------------- #
# 登记与查询
# --------------------------------------------------------------------------- #
def test_unregistered_state(store):
    assert client.get("/library").status_code == 404
    assert client.post("/library/approvals", json={"tape": "T1"}).status_code == 409
    status = client.get("/library/status").json()
    assert status["registered"] is False
    assert status["tapes"] == []


def test_register_and_query_round_trip_to_schedule(store):
    register_example()
    status = client.get("/library/status").json()
    assert status["registered"] is True
    assert status["tapes"] == ["T1", "T2", "T3"]
    assert status["max_dr_mounts"] == LIBRARY_EXAMPLE["max_dr_mounts"]

    catalog = client.get("/library").json()
    # 查询结果必须能直接提交给原 /schedule。
    ScheduleRequest.model_validate(catalog)
    response = client.post("/schedule", json=catalog)
    assert response.status_code == 200
    body = response.json()
    assert body["feasible"] is True
    assert body["min_mounts"] == 2


def test_register_validation(store):
    assert client.put("/library", json={"staging_capacity": 8, "files": []}).status_code == 422
    too_many_tapes = {
        "staging_capacity": 100,
        "max_dr_mounts": 3,
        "files": [
            {
                "id": "F",
                "shards": [
                    {"id": f"s{i}", "size": 1, "replicas": [{"tape": f"T{i}", "position": 1}]}
                    for i in range(7)
                ],
            }
        ],
    }
    assert client.put("/library", json=too_many_tapes).status_code == 422
    no_limit = {"staging_capacity": 8, "files": LIBRARY_EXAMPLE["files"]}
    assert client.put("/library", json=no_limit).status_code == 422
    bad_limit = dict(LIBRARY_EXAMPLE, max_dr_mounts=0)
    assert client.put("/library", json=bad_limit).status_code == 422


# --------------------------------------------------------------------------- #
# 批准
# --------------------------------------------------------------------------- #
def test_approval_removes_tape_and_stays_schedulable(store):
    register_example()
    decision = client.post("/library/approvals", json={"tape": "T3"}).json()
    assert decision["approved"] is True
    # 无故障 + 库内再坏 T1/T2 两个场景，共 3 个。
    assert decision["scenarios_checked"] == 3
    assert decision["worst_case_mounts"] == 2
    assert decision["failing_scenario"] is None
    assert decision["remaining_tapes"] == ["T1", "T2"]
    assert {s["failed_tape"] for s in decision["scenarios"]} == {None, "T1", "T2"}

    catalog = client.get("/library").json()
    assert tapes_in(catalog) == {"T1", "T2"}
    # 新目录仍可直接提交 /schedule。
    body = client.post("/schedule", json=catalog).json()
    assert body["feasible"] is True and body["min_mounts"] == 2

    status = client.get("/library/status").json()
    assert status["tapes"] == ["T1", "T2"]
    assert [r["tape"] for r in status["sent_tapes"]] == ["T3"]


def test_approval_unknown_tape_is_404(store):
    register_example()
    assert client.post("/library/approvals", json={"tape": "T9"}).status_code == 404


# --------------------------------------------------------------------------- #
# 拒绝
# --------------------------------------------------------------------------- #
def test_rejection_when_one_more_failure_breaks_recovery(store):
    register_example()
    assert client.post("/library/approvals", json={"tape": "T3"}).json()["approved"]

    # T3 已出库，再送 T2：库内只剩 T1，T1 再坏则全部文件不可恢复。
    decision = client.post("/library/approvals", json={"tape": "T2"}).json()
    assert decision["approved"] is False
    failing = decision["failing_scenario"]
    assert failing["failed_tape"] == "T1"
    assert failing["feasible"] is False
    assert len(failing["missing_shards"]) == 4
    assert "在库目录未变" in decision["message"]

    # 拒绝后目录不变。
    catalog = client.get("/library").json()
    assert tapes_in(catalog) == {"T1", "T2"}
    status = client.get("/library/status").json()
    assert [r["tape"] for r in status["sent_tapes"]] == ["T3"]


def test_rejection_when_mounts_exceed_limit(store):
    register_example()
    # 同一目录，把上限压到 1：无故障场景本身就需要 2 次装带。
    client.put("/library", json=dict(LIBRARY_EXAMPLE, max_dr_mounts=1))
    decision = client.post("/library/approvals", json={"tape": "T3"}).json()
    assert decision["approved"] is False
    failing = decision["failing_scenario"]
    assert failing["failed_tape"] is None  # 无故障场景即不达标
    assert failing["feasible"] is True
    assert failing["min_mounts"] == 2  # 所需的最少装带次数
    assert "超过灾备装带次数上限 1" in failing["detail"]
    # 目录不变。
    assert tapes_in(client.get("/library").json()) == {"T1", "T2", "T3"}
    assert client.get("/library/status").json()["sent_tapes"] == []


# --------------------------------------------------------------------------- #
# 持久化：重启不把已外送的磁带算回来
# --------------------------------------------------------------------------- #
def test_restart_keeps_sent_tape(store, tmp_path):
    register_example()
    assert client.post("/library/approvals", json={"tape": "T3"}).json()["approved"]

    path = tmp_path / "library_state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert [r["tape"] for r in raw["sent_tapes"]] == ["T3"]

    # 模拟进程重启：用同一状态文件新建存储并挂到 app 上。
    app.state.store = LibraryStore(path)
    catalog = client.get("/library").json()
    assert tapes_in(catalog) == {"T1", "T2"}
    status = client.get("/library/status").json()
    assert [r["tape"] for r in status["sent_tapes"]] == ["T3"]


def test_rejection_writes_nothing(store, tmp_path):
    register_example()
    path = tmp_path / "library_state.json"
    client.put("/library", json=dict(LIBRARY_EXAMPLE, max_dr_mounts=1))
    before = path.read_bytes()
    decision = client.post("/library/approvals", json={"tape": "T3"}).json()
    assert decision["approved"] is False
    assert path.read_bytes() == before  # 拒绝不落盘、不改目录


def test_corrupt_state_file_fails_loudly(store, tmp_path):
    path = tmp_path / "library_state.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="无法解析"):
        LibraryStore(path)


# --------------------------------------------------------------------------- #
# 交错审批：不得凭过时目录批准
# --------------------------------------------------------------------------- #
INTERLEAVE_CATALOG = {
    "staging_capacity": 10,
    "max_dr_mounts": 1,
    "files": [
        {
            "id": "F",
            "shards": [
                {
                    "id": "s1",
                    "size": 1,
                    "replicas": [
                        {"tape": "T1", "position": 1},
                        {"tape": "T3", "position": 3},
                        {"tape": "T4", "position": 4},
                    ],
                }
            ],
        }
    ],
}


def test_sequential_interleave_reevaluates_latest_catalog(store):
    """单独看 T3、T4 都能送，但送走一盘后另一盘必须按新目录重新核算。"""
    client.put("/library", json=INTERLEAVE_CATALOG)
    first = client.post("/library/approvals", json={"tape": "T3"}).json()
    assert first["approved"] is True
    # 若第二个请求凭旧目录（T1,T3,T4 都在）核算会误判通过；
    # 正确行为是基于 T3 已出库的目录，得出 T1 再坏则无解。
    second = client.post("/library/approvals", json={"tape": "T4"}).json()
    assert second["approved"] is False
    assert second["failing_scenario"]["failed_tape"] == "T1"
    assert second["failing_scenario"]["feasible"] is False


def test_concurrent_approvals_are_serialized(store):
    """两个审批并发到达：恰好一个批准，且最终目录自洽。"""
    client.put("/library", json=INTERLEAVE_CATALOG)
    barrier = threading.Barrier(3)
    outcomes: dict[str, dict] = {}

    def ask(tape: str) -> None:
        barrier.wait()
        outcomes[tape] = client.post("/library/approvals", json={"tape": tape}).json()

    threads = [threading.Thread(target=ask, args=(t,)) for t in ("T3", "T4")]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    approved = [tape for tape, d in outcomes.items() if d["approved"]]
    assert len(approved) == 1, outcomes
    loser = "T4" if approved[0] == "T3" else "T3"
    assert outcomes[loser]["failing_scenario"]["feasible"] is False

    status = client.get("/library/status").json()
    assert status["tapes"] == sorted({"T1", loser})
    assert [r["tape"] for r in status["sent_tapes"]] == approved


def test_query_during_evaluation(store, monkeypatch):
    """试算（哪怕很慢）不阻塞查询；查询读到的是已生效目录。"""
    client.put("/library", json=INTERLEAVE_CATALOG)
    entered = threading.Event()
    release = threading.Event()
    real_solve = library_module.solve

    def slow_solve(req):
        entered.set()
        release.wait(timeout=10)
        return real_solve(req)

    monkeypatch.setattr(library_module, "solve", slow_solve)

    result: dict = {}

    def ask() -> None:
        result["decision"] = client.post("/library/approvals", json={"tape": "T3"}).json()

    thread = threading.Thread(target=ask)
    thread.start()
    assert entered.wait(timeout=5), "审批未进入试算"
    try:
        start = time.monotonic()
        response = client.get("/library")
        elapsed = time.monotonic() - start
        assert response.status_code == 200
        assert elapsed < 5, "查询被试算阻塞"
        # 审批尚未生效，查询到的仍是含 T3 的已生效目录。
        assert tapes_in(response.json()) == {"T1", "T3", "T4"}
    finally:
        release.set()
        thread.join(timeout=10)
    assert result["decision"]["approved"] is True
    assert tapes_in(client.get("/library").json()) == {"T1", "T4"}
