"""FastAPI wrapper around the tape restore scheduler."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .library import (
    DEFAULT_STATE_PATH,
    LibraryStore,
    NotRegisteredError,
    UnknownTapeError,
)
from .models import (
    DEFAULT_EXAMPLE,
    ApproveRequest,
    ApproveResponse,
    LibraryRegistration,
    LibraryStatus,
    ScheduleRequest,
    ScheduleResponse,
)
from .solver import solve

app = FastAPI(
    title="磁带跨带恢复排程 API",
    version="1.1.0",
    summary="用最少磁带装带次数把跨带文件恢复到中转盘并交付，并审批磁带外送单",
    description=(
        "提交中转盘空闲量、磁带上限与文件目录（分片、各分片副本所在带号与位置），"
        "返回最少装带次数的排程。\n\n"
        "* 每片只选一份副本，装带内可跳过分片但只能向前读；\n"
        "* 回到带头重读另算一次装带；\n"
        "* 中转盘必须先容纳所有暂存分片，文件读齐后校验、交付并立即释放其空间；\n"
        "* 最多 6 盘带、12 个分片、48 份副本。\n\n"
        "无解时返回缺失副本的分片，以及需要同时暂存却超出中转盘容量的文件。"
        "可直接用页面上的 *Try it out* 运行内置样例。\n\n"
        "**磁带外送审批**：`PUT /library` 按排程格式登记在库目录、中转盘容量与"
        "灾备装带次数上限；`POST /library/approvals` 申请外送一盘在库磁带——"
        "扣除该盘后，无故障以及库内再坏任意一盘的每个场景都必须能在上限内恢复"
        "全部文件，否则拒绝并给出未通过场景；批准后该盘即从在库目录移除并落盘，"
        "重启不会算回来。`GET /library` 查询当前在库目录，可直接提交给 "
        "`POST /schedule`。"
    ),
    openapi_tags=[
        {
            "name": "schedule",
            "description": "排程计算与样例",
        },
        {
            "name": "library",
            "description": "在库目录登记、查询与磁带外送审批",
        },
    ],
)

# 默认存储在导入时创建（容器内由 LIBRARY_STATE_PATH 指定到挂载卷）；
# 测试可直接替换 app.state.store。
app.state.store = LibraryStore(
    Path(os.environ.get("LIBRARY_STATE_PATH", str(DEFAULT_STATE_PATH)))
)


def get_store() -> LibraryStore:
    """请求作用域内取当前存储（测试可替换 app.state.store）。"""
    return app.state.store


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["schedule"], summary="健康检查")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/example",
    response_model=ScheduleRequest,
    tags=["schedule"],
    summary="获取可直接提交的样例请求",
)
def example() -> ScheduleRequest:
    return ScheduleRequest.model_validate(DEFAULT_EXAMPLE)


@app.post(
    "/schedule",
    response_model=ScheduleResponse,
    tags=["schedule"],
    summary="计算最少装带次数的恢复排程",
    responses={
        200: {"description": "排程结果（可行或不可行）"},
        422: {"description": "请求格式或上限校验失败"},
    },
)
def schedule(request: ScheduleRequest) -> ScheduleResponse:
    try:
        return solve(request)
    except Exception as exc:  # noqa: BLE001 - surface a clear API error
        raise HTTPException(status_code=500, detail=f"排程计算失败: {exc}") from exc


# --------------------------------------------------------------------------- #
# 磁带外送审批
# --------------------------------------------------------------------------- #
@app.put(
    "/library",
    response_model=LibraryStatus,
    tags=["library"],
    summary="登记（或重设）在库目录与灾备装带次数上限",
    responses={
        200: {"description": "登记成功，返回在库概览"},
        422: {"description": "目录格式或上限校验失败"},
    },
)
def register_library(
    registration: LibraryRegistration,
    store: LibraryStore = Depends(get_store),
) -> LibraryStatus:
    """按排程请求格式登记单库目录与中转盘容量，另设灾备装带次数上限。

    重复调用会整体替换目录与上限，并重置已外送记录（重新盘点入库）。
    """
    return store.register(registration)


@app.get(
    "/library",
    response_model=ScheduleRequest,
    tags=["library"],
    summary="查询当前在库目录（可直接提交给 /schedule）",
    responses={
        200: {"description": "当前在库目录，格式与排程请求一致"},
        404: {"description": "尚未登记在库目录"},
    },
)
def get_library(store: LibraryStore = Depends(get_store)) -> ScheduleRequest:
    """返回当前在库目录；审批试算期间也可查询，读到的是已生效的目录。"""
    try:
        return store.schedule_request()
    except NotRegisteredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/library/status",
    response_model=LibraryStatus,
    tags=["library"],
    summary="在库概览：上限、在库磁带与已外送记录",
)
def library_status(store: LibraryStore = Depends(get_store)) -> LibraryStatus:
    return store.status()


@app.post(
    "/library/approvals",
    response_model=ApproveResponse,
    tags=["library"],
    summary="审批一盘在库磁带的外送申请",
    responses={
        200: {"description": "审批结论（批准或拒绝，均含场景明细）"},
        404: {"description": "申请的磁带不在在库目录中"},
        409: {"description": "尚未登记在库目录"},
    },
)
def approve_sendout(
    request: ApproveRequest,
    store: LibraryStore = Depends(get_store),
) -> ApproveResponse:
    """扣除该盘后，无故障与库内再坏任意一盘的每个场景都必须能在灾备装带
    次数上限内恢复全部文件：满足则批准并落盘生效，否则拒绝且目录不变。

    试算与生效在同一把锁内完成，并发审批不会凭过时目录批准。
    """
    try:
        return store.approve(request.tape)
    except NotRegisteredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnknownTapeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
