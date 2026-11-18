"""商品维度最新版表：按业务键合并 DWD 商品明细的链路测试。

覆盖：
- 每个业务键只保留一行当前值，迟到更正能刷新当前值；
- 刷新水位、来源批次、结构版本与合并结果同批提交；
- 同一批记录不会因为水位没推进而被重复处理；
- 迟到更正与已合并记录业务时间相同时，两种处理边界各自的结果；
- 提交失败回滚到恢复点；
- 维度建模与结构注册围绕最新版表串联。
"""
from __future__ import annotations

from app.store import store
from app.services.dim_model import DimModelService
from app.services.product_dim_latest import (
    CONFLICT_POLICY_KEEP,
    CONFLICT_POLICY_OVERRIDE,
    DIM_TABLE,
    ProductDimLatestService,
)
from app.services.schema_registry import SchemaRegistryService


def reset_store() -> None:
    from app.seed import SEED_ROWS, SEED_TABLE_DATA

    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data = {
        name: [dict(row) for row in rows] for name, rows in SEED_TABLE_DATA.items()
    }
    store._table_meta.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    store.failpoint = None


def product_rows(rows, key):
    return [row for row in rows if row["业务键"] == key]


def clear_latest_table() -> None:
    """让个别用例从空的最新版表开始，只验证本次合并的产物。"""
    store.set_table_rows(DIM_TABLE, [])
    store.set_table_meta(DIM_TABLE, {
        "刷新水位": "",
        "来源批次": "",
        "结构版本": "",
        "水位序号": 0,
        "待确认更正": [],
        "版本历史": [],
        "提交记录": [],
        "已合并记录": {},
    })


class TestProductDimMerge:
    def setup_method(self) -> None:
        reset_store()

    def test_merge_keeps_one_row_per_business_key(self) -> None:
        """按业务键合并后每个商品只有一行，且取业务时间最新的那次明细。"""
        clear_latest_table()
        store.set_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标", "price": "99", "dt": "2026-11-03"},
            {"product_id": "P2", "product_name": "键盘", "price": "199", "dt": "2026-11-03"},
            {"product_id": "P1", "product_name": "鼠标（新）", "price": "109", "dt": "2026-11-04"},
        ])
        result = ProductDimLatestService().merge_from_dwd(batch_id="RAW-20261104")
        assert result["ok"] is True
        rows = result["rows"]
        assert [row["业务键"] for row in rows] == ["P1", "P2"]
        assert len(product_rows(rows, "P1")) == 1
        assert product_rows(rows, "P1")[0]["product_name"] == "鼠标（新）"
        assert product_rows(rows, "P1")[0]["来源业务时间"] == "2026-11-04"

    def test_late_correction_refreshes_current_value(self) -> None:
        """迟到更正业务时间更新时刷新当前值，而不是保留首次出现行。"""
        store.set_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标", "price": "99", "dt": "2026-11-03"},
        ])
        svc = ProductDimLatestService()
        first = svc.merge_from_dwd(batch_id="RAW-20261103")
        assert first["刷新水位"] == "2026-11-03"

        # 后到批次带来业务时间更晚的更正
        store.append_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标（更正）", "price": "109",
             "dt": "2026-11-05", "_source_batch": "RAW-20261106"},
        ])
        second = svc.merge_from_dwd(batch_id="RAW-20261106")
        assert second["ok"] is True
        assert second["刷新水位"] == "2026-11-05"
        assert product_rows(second["rows"], "P1")[0]["product_name"] == "鼠标（更正）"
        assert product_rows(second["rows"], "P1")[0]["来源批次"] == "RAW-20261106"

    def test_result_watermark_and_schema_commit_together(self) -> None:
        """合并结果、刷新水位、结构版本与提交记录在同一次提交里可见。"""
        store.set_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标", "price": "99", "dt": "2026-11-03"},
        ])
        result = ProductDimLatestService().merge_from_dwd(batch_id="RAW-20261103")
        meta = ProductDimLatestService().get_meta()
        assert meta["刷新水位"] == result["刷新水位"] == "2026-11-03"
        assert meta["来源批次"] == "RAW-20261103"
        assert meta["结构版本"] == result["结构版本"]
        assert meta["最近提交"]["水位序号"] == 1
        assert meta["最近提交"]["结构版本"] == result["结构版本"]
        assert len(ProductDimLatestService().get_commits()) == 1

    def test_rerun_does_not_reprocess_same_records(self) -> None:
        """水位与合并结果一起落库后，重跑不会重复处理同一批记录。"""
        store.set_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标", "price": "99", "dt": "2026-11-03"},
            {"product_id": "P2", "product_name": "键盘", "price": "199", "dt": "2026-11-03"},
        ])
        svc = ProductDimLatestService()
        first = svc.merge_from_dwd(batch_id="RAW-20261103")
        assert first["合并行数"] == 2
        second = svc.merge_from_dwd(batch_id="RAW-20261103")
        assert second["合并行数"] == 0
        assert second["刷新水位"] == first["刷新水位"]
        assert len(svc.get_commits()) == 1
        assert svc.get_rows() == first["rows"]

    def test_pending_correction_not_reprocessed_next_run(self) -> None:
        """只进待确认清单、水位不推进的更正也不会在下一轮被重复处理。"""
        store.set_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标", "price": "99", "dt": "2026-11-03"},
        ])
        svc = ProductDimLatestService()
        svc.merge_from_dwd(batch_id="RAW-20261103")
        store.append_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标（迟到）", "price": "119", "dt": "2026-11-03"},
        ])
        first = svc.merge_from_dwd(batch_id="RAW-20261107")
        assert first["合并行数"] == 0
        assert len(first["待确认更正"]) == 1
        second = svc.merge_from_dwd(batch_id="RAW-20261107")
        assert second["合并行数"] == 0
        assert second["跳过行数"] >= 1
        assert len(second["待确认更正"]) == 1
        # 当前值仍是原值
        assert product_rows(svc.get_rows(), "P1")[0]["product_name"] == "鼠标"

    def test_same_business_time_keep_policy(self) -> None:
        """业务时间相同时保留原值，并生成更正待确认清单。"""
        store.set_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标", "price": "99", "dt": "2026-11-03"},
        ])
        svc = ProductDimLatestService()
        svc.merge_from_dwd(conflict_policy=CONFLICT_POLICY_KEEP, batch_id="RAW-20261103")
        store.append_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标（同刻更正）", "price": "129",
             "dt": "2026-11-03"},
        ])
        result = svc.merge_from_dwd(conflict_policy=CONFLICT_POLICY_KEEP, batch_id="RAW-20261108")
        assert result["ok"] is True
        assert product_rows(result["rows"], "P1")[0]["product_name"] == "鼠标"
        pending = svc.get_pending_corrections()
        assert len(pending) == 1
        assert pending[0]["业务键"] == "P1"
        assert pending[0]["更正"]["product_name"] == "鼠标（同刻更正）"
        assert pending[0]["判定原因"] == "业务时间与已合并记录相同"
        assert svc.get_history() == []

    def test_same_business_time_override_policy(self) -> None:
        """业务时间相同时覆盖当前值，原值保留在版本历史。"""
        store.set_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标", "price": "99", "dt": "2026-11-03"},
        ])
        svc = ProductDimLatestService()
        svc.merge_from_dwd(conflict_policy=CONFLICT_POLICY_OVERRIDE, batch_id="RAW-20261103")
        store.append_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标（同刻更正）", "price": "129",
             "dt": "2026-11-03"},
        ])
        result = svc.merge_from_dwd(
            conflict_policy=CONFLICT_POLICY_OVERRIDE, batch_id="RAW-20261108"
        )
        assert result["ok"] is True
        assert product_rows(result["rows"], "P1")[0]["product_name"] == "鼠标（同刻更正）"
        history = svc.get_history()
        assert len(history) == 1
        assert history[0]["原值"]["product_name"] == "鼠标"
        assert history[0]["更正"]["product_name"] == "鼠标（同刻更正）"
        assert svc.get_pending_corrections() == []

    def test_commit_failure_rolls_back_to_recovery_point(self) -> None:
        """提交阶段失败时，结果、水位、结构版本一起回滚，不留半提交状态。"""
        store.set_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标", "price": "99", "dt": "2026-11-03"},
        ])
        svc = ProductDimLatestService()
        before_meta = dict(store.table_meta(DIM_TABLE))
        before_rows = [dict(row) for row in store.table_rows(DIM_TABLE)]
        schema_count = len(store.rows("schema_registry"))

        def boom(stage: str) -> None:
            raise RuntimeError("模拟提交阶段写水位失败")

        store.failpoint = boom
        result = svc.merge_from_dwd(batch_id="RAW-20261109")
        store.failpoint = None

        assert result["ok"] is False
        assert result.get("rolled_back") is True
        assert "回滚到恢复点" in result["message"]
        assert store.table_meta(DIM_TABLE) == before_meta
        assert [dict(row) for row in store.table_rows(DIM_TABLE)] == before_rows
        assert len(store.rows("schema_registry")) == schema_count
        assert svc.get_commits() == []
        # 回滚后可以正常重试，不产生重复提交
        retried = svc.merge_from_dwd(batch_id="RAW-20261109")
        assert retried["ok"] is True
        assert len(svc.get_commits()) == 1

    def test_invalid_conflict_policy_is_rejected(self) -> None:
        """未知冲突策略直接拒绝，不落地任何合并结果。"""
        result = ProductDimLatestService().merge_from_dwd(conflict_policy="随便覆盖")
        assert result["ok"] is False
        assert "不在可选范围" in result["message"]
        assert ProductDimLatestService().get_commits() == []


class TestProductDimLinkage:
    def setup_method(self) -> None:
        reset_store()

    def test_dim_model_merge_and_result_lookup(self) -> None:
        """维度建模列表发起刷新后，可从维度模型结果回查到最新版表。"""
        entry = store.find("dim_model", 3)  # 商品维度
        assert entry is not None
        result = DimModelService().build_dim_from_dwd(entry["id"])
        assert result["ok"] is True
        lookup = ProductDimLatestService().get_dimension_result(entry["id"])
        assert lookup is not None
        assert lookup["维度模型"]["模型编号"] == entry["模型编号"]
        assert lookup["刷新水位"] == "2026-11-04"
        assert lookup["当前值行数"] == len(lookup["当前值"])
        assert lookup["结构版本"] == ProductDimLatestService().get_meta()["结构版本"]

    def test_schema_registry_tracks_latest_table(self) -> None:
        """结构注册为最新版表登记结构版本，水位、来源批次与结构版本互相定位。"""
        ProductDimLatestService().merge_from_dwd(batch_id="RAW-20261104")
        svc = SchemaRegistryService()
        latest = svc.get_latest_schema(DIM_TABLE)
        assert latest is not None
        assert latest["定义版本"] == ProductDimLatestService().get_meta()["结构版本"]
        assert latest["生效批次"] == "RAW-20261104"
        assert latest["status"] == "已生效"
        assert "业务键" not in latest["字段名称"]

    def test_schema_version_bumps_only_on_field_change(self) -> None:
        """字段结构不变时不重复升级结构版本。"""
        svc = ProductDimLatestService()
        store.set_table_rows("dwd_product_detail", [
            {"product_id": "P1", "product_name": "鼠标", "price": "99", "dt": "2026-11-03"},
        ])
        first = svc.merge_from_dwd(batch_id="RAW-20261103")
        store.append_table_rows("dwd_product_detail", [
            {"product_id": "P2", "product_name": "键盘", "price": "199", "dt": "2026-11-04"},
        ])
        second = svc.merge_from_dwd(batch_id="RAW-20261104")
        assert second["结构版本"] == first["结构版本"]

    def test_version_number_compare_handles_double_digits(self) -> None:
        """结构版本按数值比较，v10 不会被 v2 字典序压住。"""
        svc = SchemaRegistryService()
        svc.register_structure({
            "定义编号": "SCH-BIG-1", "表名称": "dwd_big_table", "字段名称": "a",
            "字段类型": "STRING", "可空标识": "Y", "默认值": "",
            "定义版本": "v2", "生效批次": "RAW-1",
        })
        svc.register_structure({
            "定义编号": "SCH-BIG-2", "表名称": "dwd_big_table", "字段名称": "a,b",
            "字段类型": "STRING,STRING", "可空标识": "Y,Y", "默认值": ",",
            "定义版本": "v10", "生效批次": "RAW-2",
        })
        latest = svc.get_latest_schema("dwd_big_table")
        assert latest is not None and latest["定义版本"] == "v10"
