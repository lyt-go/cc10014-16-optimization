"""维度建模接口：维护维度模型，覆盖提交校验、标记生效、停用模型等动作。

另外暴露商品维度最新版表的结果回查与刷新接口：维度建模列表负责发起合并，
最新版表详情负责回查维度模型结果、刷新水位、来源批次和结构版本。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.dim_model import DimModelService
from app.services.product_dim_latest import (
    CONFLICT_POLICIES,
    ProductDimLatestService,
)

router = APIRouter(prefix="/api/dim_model", tags=["维度建模"])

service = DimModelService()
product_dim = ProductDimLatestService()

LIST_FIELDS = ["模型编号", "维度名称", "自然键列", "代理键列", "缓慢变化策略", "生效日期", "失效日期", "刷新周期"]
STATUSES = ["草稿", "待校验", "已生效", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按模型编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已生效、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、模型编号关键字与状态过滤维度建模列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="dim_model.csv"'})


@router.get("/product-dim/latest", response_model=dict)
def get_product_dim_latest() -> dict:
    """商品维度最新版表：每个业务键一行当前值，附带水位、来源批次与结构版本。"""
    return {
        "meta": product_dim.get_meta(),
        "rows": product_dim.get_rows(),
        "pending": product_dim.get_pending_corrections(),
        "history": product_dim.get_history(),
        "commits": product_dim.get_commits(),
        "conflict_policies": list(CONFLICT_POLICIES),
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条维度模型明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"维度模型 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/dimension-result", response_model=dict)
def get_dimension_result(entry_id: int) -> dict:
    """维度模型结果回查：定位商品维度最新版表的当前值、水位与结构版本。"""
    result = product_dim.get_dimension_result(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"维度模型 {entry_id} 不存在或已归档")
    return result


@router.post("/{entry_id}/product-dim/merge", response_model=dict)
def merge_product_dim(entry_id: int, payload: dict[str, Any] | None = None) -> dict:
    """从 DWD 商品明细按业务键合并到商品维度最新版表。

    conflict_policy 决定业务时间相同/更早的迟到更正怎么处理：
    保留原值（默认）只记待确认清单，覆盖当前值则更新并保留原版本历史。
    """
    payload = payload or {}
    policy = str(payload.get("conflict_policy") or "")
    batch_id = str(payload.get("batch_id") or "")
    if policy and policy not in CONFLICT_POLICIES:
        return {
            "ok": False,
            "message": f"冲突处理策略不在可选范围：{'、'.join(CONFLICT_POLICIES)}",
        }
    return product_dim.merge_from_dwd(
        conflict_policy=policy or CONFLICT_POLICIES[0],
        batch_id=batch_id,
        entry_id=entry_id,
    )


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条维度模型，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="维度模型已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条维度模型执行提交校验、标记生效、停用模型；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
