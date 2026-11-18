"""商品维度最新版表：按业务键增量合并 DWD 商品明细，维护每个商品只有一行当前值。

设计要点（对应「迟到更正不能重复处理、不能半提交」的约束）：

* 每个业务键一行当前值，行上保留业务键、来源批次、来源业务时间、结构版本与
  水位序号，便于和表级刷新水位、来源批次、结构版本相互定位。
* 增量扫描按「记录指纹」（业务键 + 业务时间 + 业务属性摘要）去重，而不是只按
  业务时间水位去重。迟到更正往往带新批次、旧业务时间，保留原值的处理也不会
  推进水位，靠指纹仍然能保证下一轮不会重复处理同一批记录。
* 迟到更正与已合并记录业务时间相同时，处理边界可配置：
  - 保留原值（默认）：当前值不变，写入更正待确认清单；
  - 覆盖当前值：当前值更新为新属性，原值进入版本历史。
  待确认清单里的更正可人工确认后再落到当前值，优先级由使用者决定。
* 合并结果、刷新水位、结构版本、提交记录、已处理指纹在同一个事务里提交；任何
  一步失败都会回滚到提交前的恢复点，不会出现「合并了但水位没推进」。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.store import store

DIM_TABLE = "dim_product_latest"
MODULE = "dim_model"
SOURCE_TABLE = "dwd_product_detail"

CONFLICT_POLICY_KEEP = "保留原值"
CONFLICT_POLICY_OVERRIDE = "覆盖当前值"
CONFLICT_POLICIES = (CONFLICT_POLICY_KEEP, CONFLICT_POLICY_OVERRIDE)
_POLICY_ALIASES = {
    "保留原值": CONFLICT_POLICY_KEEP,
    "水位拦截": CONFLICT_POLICY_KEEP,
    "keep": CONFLICT_POLICY_KEEP,
    "覆盖当前值": CONFLICT_POLICY_OVERRIDE,
    "更新为新属性": CONFLICT_POLICY_OVERRIDE,
    "override": CONFLICT_POLICY_OVERRIDE,
}

# DWD 明细里可能出现的业务时间列，按顺序取第一个非空的作为“来源业务时间”。
BUSINESS_TIME_FIELDS = ("业务时间", "update_time", "updated_at", "dt")
KEY_FIELDS = ("product_id", "商品编号", "业务键")
BATCH_FIELDS = ("来源批次", "_source_batch", "批次编号")
ROW_META_FIELDS = {
    "业务键", "来源批次", "来源业务时间", "结构版本", "水位序号",
    "上级来源批次", "_source_batch", "_dim_key",
}


def normalize_policy(policy: str | None) -> str | None:
    if not policy:
        return CONFLICT_POLICY_KEEP
    return _POLICY_ALIASES.get(str(policy).strip(), None)


class ProductDimLatestService:
    # ---- 读 ----

    def get_rows(self) -> list[dict[str, Any]]:
        """最新版表每个业务键只有一行当前值。"""
        rows = [dict(row) for row in store.table_rows(DIM_TABLE)]
        rows.sort(key=lambda item: str(item.get("业务键", "")))
        return rows

    def get_meta(self) -> dict[str, Any]:
        meta = store.table_meta(DIM_TABLE)
        return {
            "最新版表": DIM_TABLE,
            "来源明细表": meta.get("来源明细表", SOURCE_TABLE),
            "刷新水位": meta.get("刷新水位", ""),
            "来源批次": meta.get("来源批次", ""),
            "结构版本": meta.get("结构版本", ""),
            "水位序号": meta.get("水位序号", 0),
            "冲突处理策略": meta.get("冲突处理策略", CONFLICT_POLICY_KEEP),
            "更新时间": meta.get("更新时间", ""),
            "维度模型id": meta.get("维度模型id"),
            "维度模型编号": meta.get("维度模型编号", ""),
            "当前值行数": len(store.table_rows(DIM_TABLE)),
            "待确认更正数": len(meta.get("待确认更正", [])),
            "最近提交": dict(meta.get("最近提交", {})),
        }

    def get_pending_corrections(self) -> list[dict[str, Any]]:
        return [dict(item) for item in store.table_meta(DIM_TABLE).get("待确认更正", [])]

    def get_history(self) -> list[dict[str, Any]]:
        return [dict(item) for item in store.table_meta(DIM_TABLE).get("版本历史", [])]

    def get_commits(self) -> list[dict[str, Any]]:
        return [dict(item) for item in store.table_meta(DIM_TABLE).get("提交记录", [])]

    def get_dimension_result(self, entry_id: int) -> dict[str, Any] | None:
        """维度模型回查：把维度模型配置和最新版表结果定位到一起。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        meta = self.get_meta()
        return {
            "维度模型": {
                "id": entry.get("id"),
                "模型编号": entry.get("模型编号", ""),
                "维度名称": entry.get("维度名称", ""),
                "自然键列": entry.get("自然键列", ""),
                "代理键列": entry.get("代理键列", ""),
                "缓慢变化策略": entry.get("缓慢变化策略", ""),
                "status": entry.get("status", ""),
            },
            "最新版表": meta["最新版表"],
            "来源明细表": meta["来源明细表"],
            "刷新水位": meta["刷新水位"],
            "来源批次": meta["来源批次"],
            "结构版本": meta["结构版本"],
            "冲突处理策略": meta["冲突处理策略"],
            "更新时间": meta["更新时间"],
            "当前值行数": len(self.get_rows()),
            "待确认更正数": len(self.get_pending_corrections()),
            "当前值": self.get_rows(),
        }

    # ---- 合并 ----

    def merge_from_dwd(
        self,
        *,
        conflict_policy: str = CONFLICT_POLICY_KEEP,
        batch_id: str = "",
        entry_id: int | None = None,
        source_table: str | None = None,
        key_field: str | None = None,
    ) -> dict[str, Any]:
        """把 DWD 商品明细按业务键合并进最新版表。

        合并（结果 + 水位 + 结构版本 + 提交记录）走一次事务，失败时回滚到恢复点
        并把错误返回给调用方。
        """
        policy = normalize_policy(conflict_policy)
        if policy is None:
            return {
                "ok": False,
                "message": (
                    f"冲突处理策略「{conflict_policy}」不在可选范围："
                    f"{'、'.join(CONFLICT_POLICIES)}"
                ),
            }
        if entry_id is not None and (source_table is None or key_field is None):
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return {"ok": False, "message": f"维度模型 {entry_id} 不存在或已归档"}
            source_table = source_table or self._source_table_for(entry)
            key_field = key_field or str(entry.get("自然键列") or "") or None
        try:
            return store.commit_transaction(
                lambda: self._merge_in_transaction(
                    policy, batch_id, entry_id, source_table=source_table, key_field=key_field
                )
            )
        except Exception as exc:  # noqa: BLE001 - 对外统一成可读结果
            return {
                "ok": False,
                "message": f"商品维度最新版表提交失败，已回滚到恢复点：{exc}",
                "rolled_back": True,
            }

    def _merge_in_transaction(
        self,
        conflict_policy: str,
        batch_id: str,
        entry_id: int | None,
        *,
        source_table: str | None = None,
        key_field: str | None = None,
    ) -> dict[str, Any]:
        meta = store.table_meta(DIM_TABLE)
        watermark = str(meta.get("刷新水位", "") or "")
        sequence = int(meta.get("水位序号", 0)) + 1
        current_rows = [dict(row) for row in store.table_rows(DIM_TABLE)]
        current_by_key = {str(row.get("业务键", "")): row for row in current_rows}
        processed: dict[str, Any] = dict(meta.get("已合并记录", {}))
        pending = [dict(item) for item in meta.get("待确认更正", [])]
        history = [dict(item) for item in meta.get("版本历史", [])]

        source_name = source_table or SOURCE_TABLE
        source_rows = [dict(row) for row in store.table_rows(source_name)]
        resolved_batch = batch_id or self._infer_batch(source_rows)
        key_fields = (key_field,) + KEY_FIELDS if key_field else KEY_FIELDS

        merged_keys: list[str] = []
        conflict_keys: list[str] = []
        skipped = 0
        merged_row_count = 0
        max_time = ""
        seen_in_run: dict[str, str] = {}

        for row in source_rows:
            key = self._row_key(row, key_fields)
            if not key:
                skipped += 1
                continue
            row_time = self._row_business_time(row)
            signature = self._signature(key, row, row_time)
            if signature in processed:
                skipped += 1
                continue
            # 同一轮里同一业务键出现多次时，只让业务时间最晚的一条参与合并；
            # 业务时间相同则继续走冲突判定，避免把真正的更正静默丢掉。
            prior_time = seen_in_run.get(key)
            if prior_time is not None and prior_time > row_time:
                skipped += 1
                processed[signature] = {"处理": "同一批次内被更晚记录覆盖"}
                continue

            existing = current_by_key.get(key)
            if existing is not None and self._same_business(row, existing):
                # 内容完全一致的重扫记录，不重复处理也不进待确认清单。
                skipped += 1
                processed[signature] = {"处理": "与当前值一致，跳过"}
                continue
            existing_time = self._row_business_time(existing) if existing else ""
            if existing is not None and (not row_time or row_time <= existing_time):
                record = {
                    "业务键": key,
                    "原值": dict(existing),
                    "更正": self._business_fields(row),
                    "来源业务时间": row_time,
                    "原来源业务时间": existing_time,
                    "判定原因": self._conflict_reason(row_time, existing_time),
                    "策略": conflict_policy,
                    "来源批次": resolved_batch,
                    "发现时间": datetime.now().isoformat(timespec="seconds"),
                }
                if conflict_policy == CONFLICT_POLICY_OVERRIDE:
                    history.append({
                        **record,
                        "处理": "已按覆盖当前值策略更新，原值保留在版本历史",
                        "水位序号": sequence,
                    })
                    current_by_key[key] = self._build_row(
                        key, row, resolved_batch, row_time, sequence, previous=existing,
                    )
                    merged_keys.append(key)
                    merged_row_count += 1
                    if row_time:
                        max_time = max(max_time, row_time)
                else:
                    record["状态"] = "待确认"
                    pending.append(record)
                    conflict_keys.append(key)
                processed[signature] = {
                    "业务键": key,
                    "来源批次": resolved_batch,
                    "来源业务时间": row_time,
                    "处理": conflict_policy,
                }
                if row_time:
                    seen_in_run[key] = row_time
                continue

            current_by_key[key] = self._build_row(
                key, row, resolved_batch, row_time, sequence, previous=existing,
            )
            processed[signature] = {
                "业务键": key,
                "来源批次": resolved_batch,
                "来源业务时间": row_time,
                "处理": "合并当前值",
            }
            merged_keys.append(key)
            merged_row_count += 1
            if row_time:
                seen_in_run[key] = row_time
                max_time = max(max_time, row_time)

        new_rows = sorted(current_by_key.values(), key=lambda item: str(item.get("业务键", "")))
        advanced_watermark = max([watermark, max_time]) if max_time else watermark

        if not merged_keys and not conflict_keys:
            return {
                "ok": True,
                "message": f"没有新增记录，刷新水位保持 {watermark or '—'}",
                "rows": new_rows,
                "刷新水位": watermark,
                "来源批次": meta.get("来源批次", ""),
                "结构版本": meta.get("结构版本", ""),
                "合并业务键": [],
                "合并行数": 0,
                "冲突业务键": [],
                "跳过行数": skipped,
                "待确认更正": pending,
                "版本历史": history,
                "commit": {},
            }

        # 结构版本随最新版表的业务字段结构推进；字段结构不变时保持原版本。
        structure_version = self._register_structure(
            new_rows, meta.get("结构版本", ""), resolved_batch
        )
        for row in new_rows:
            row["结构版本"] = structure_version
        commit_record = {
            "水位序号": sequence,
            "提交批次": resolved_batch,
            "合并业务键": list(dict.fromkeys(merged_keys)),
            "合并行数": merged_row_count,
            "冲突业务键": conflict_keys,
            "跳过行数": skipped,
            "刷新水位": advanced_watermark,
            "来源批次": resolved_batch,
            "结构版本": structure_version,
            "提交时间": datetime.now().isoformat(timespec="seconds"),
        }

        model_meta: dict[str, Any] = {}
        if entry_id is not None:
            entry = store.find(MODULE, entry_id)
            if entry is not None:
                model_meta = {
                    "维度模型id": entry.get("id"),
                    "维度模型编号": entry.get("模型编号", ""),
                }

        store.set_table_rows(DIM_TABLE, new_rows)
        store.set_table_meta(DIM_TABLE, {
            **model_meta,
            "来源明细表": source_name,
            "刷新水位": advanced_watermark,
            "来源批次": resolved_batch,
            "结构版本": structure_version,
            "水位序号": sequence,
            "冲突处理策略": conflict_policy,
            "更新时间": commit_record["提交时间"],
            "最近提交": commit_record,
            "待确认更正": pending,
            "版本历史": history,
            "提交记录": [*meta.get("提交记录", []), commit_record],
            "已合并记录": processed,
        })

        result: dict[str, Any] = {
            "ok": True,
            "message": (
                f"商品维度最新版表已合并 {merged_row_count} 行"
                f"（{len(set(merged_keys))} 个业务键），冲突 {len(conflict_keys)} 个，"
                f"跳过 {skipped} 行，刷新水位 {advanced_watermark or '—'}，"
                f"结构版本 {structure_version}"
            ),
            "rows": new_rows,
            "刷新水位": advanced_watermark,
            "来源批次": resolved_batch,
            "结构版本": structure_version,
            "水位序号": sequence,
            "合并业务键": list(dict.fromkeys(merged_keys)),
            "合并行数": merged_row_count,
            "冲突业务键": conflict_keys,
            "跳过行数": skipped,
            "待确认更正": pending,
            "版本历史": history,
            "commit": commit_record,
        }
        if entry_id is not None:
            dim_result = self.get_dimension_result(entry_id)
            if dim_result is None:
                return {**result, "ok": False, "message": f"维度模型 {entry_id} 不存在"}
            result["dimension_result"] = dim_result
        return result

    # ---- 内部工具 ----

    def _build_row(
        self,
        key: str,
        source_row: dict[str, Any],
        batch_id: str,
        row_time: str,
        sequence: int,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        business_fields = {
            field: value
            for field, value in source_row.items()
            if field not in ROW_META_FIELDS
        }
        return {
            **business_fields,
            "业务键": key,
            "来源批次": batch_id or self._resolve_row_batch(source_row),
            "来源业务时间": row_time,
            "结构版本": (previous or {}).get("结构版本", ""),
            "水位序号": sequence,
            "上级来源批次": (previous or {}).get("来源批次", ""),
        }

    def _register_structure(
        self, rows: list[dict[str, Any]], current_version: str, batch_id: str
    ) -> str:
        """把最新版表的业务字段结构登记到结构注册，版本号只增不减。"""
        fields: list[str] = []
        for row in rows:
            for field in row:
                if field in ROW_META_FIELDS or field in fields:
                    continue
                fields.append(field)
        if not fields:
            return current_version or "v1"
        from app.services.schema_registry import SchemaRegistryService

        svc = SchemaRegistryService()
        latest = svc.get_latest_schema(DIM_TABLE)
        signature = ",".join(fields)
        if latest and latest.get("字段名称") == signature:
            return str(latest.get("定义版本") or current_version or "v1")
        entry = svc.register_structure({
            "定义编号": f"SCH-{DIM_TABLE.upper()}",
            "表名称": DIM_TABLE,
            "字段名称": signature,
            "字段类型": ",".join("STRING" for _ in fields),
            "可空标识": ",".join("Y" for _ in fields),
            "默认值": ",".join("" for _ in fields),
            "定义版本": (
                self._bump_version(latest.get("定义版本"))
                if latest
                else (current_version or "v1")
            ),
            "生效批次": batch_id,
        })
        return str(entry.get("定义版本") or current_version or "v1")

    def _bump_version(self, version: str | None) -> str:
        text = str(version or "v1")
        try:
            return f"v{int(text.lstrip('v')) + 1}"
        except ValueError:
            return "v2"

    def _conflict_reason(self, row_time: str, existing_time: str) -> str:
        if not row_time:
            return "缺少业务时间无法判定先后"
        if row_time == existing_time:
            return "业务时间与已合并记录相同"
        return "业务时间早于已合并记录"

    def _signature(self, key: str, row: dict[str, Any], row_time: str) -> str:
        """记录指纹：业务键 + 业务字段 + 业务时间，与来源批次无关。

        迟到更正可能来自任意批次，是否处理过要按记录内容判断；这样即使本轮
        校正只进了待确认清单、水位没有推进，下一轮也不会重复处理同一批记录。
        """
        payload = json.dumps(
            {"key": key, "time": row_time, "row": self._business_fields(row)},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _business_fields(self, row: dict[str, Any]) -> dict[str, Any]:
        """去掉来源元数据后的业务字段，用于稳定比较与待确认记录。"""
        return {
            field: value
            for field, value in row.items()
            if field not in ROW_META_FIELDS and field != "_source_batch"
        }

    def _same_business(self, source_row: dict[str, Any], current_row: dict[str, Any]) -> bool:
        """来源行与当前值的业务属性是否完全一致（忽略批次、水位等元数据）。"""
        source_business = {
            field: value
            for field, value in source_row.items()
            if field not in ROW_META_FIELDS and field not in {"_source_batch"}
        }
        current_business = {
            field: value
            for field, value in current_row.items()
            if field not in ROW_META_FIELDS
        }
        # dt 在明细里是分区字段，和「来源业务时间」同源时不参与差异比较。
        source_business.pop("dt", None)
        current_business.pop("dt", None)
        return source_business == current_business

    def _row_key(self, row: dict[str, Any], key_fields: tuple[str, ...] = KEY_FIELDS) -> str:
        for field in key_fields:
            value = row.get(field)
            if value not in (None, ""):
                return str(value)
        return ""

    def _source_table_for(self, entry: dict[str, Any]) -> str:
        """按维度名称定位 DWD 明细表，默认商品明细表。"""
        name = str(entry.get("维度名称") or "")
        if "商品" in name:
            return SOURCE_TABLE
        if "用户" in name:
            return "dwd_user_detail"
        if "日期" in name:
            return "dwd_date_detail"
        return SOURCE_TABLE

    def _row_business_time(self, row: dict[str, Any]) -> str:
        for field in BUSINESS_TIME_FIELDS:
            value = row.get(field)
            if value not in (None, ""):
                return str(value)
        return ""

    def _resolve_row_batch(self, row: dict[str, Any]) -> str:
        for field in BATCH_FIELDS:
            value = row.get(field)
            if value not in (None, ""):
                return str(value)
        return ""

    def _infer_batch(self, rows: list[dict[str, Any]]) -> str:
        batches = [self._resolve_row_batch(row) for row in rows]
        batches = [batch for batch in batches if batch]
        return max(batches) if batches else f"MERGE-{datetime.now().strftime('%Y%m%d%H%M%S')}"


service = ProductDimLatestService()
