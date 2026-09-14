# -*- coding: utf-8 -*-
"""WP4 收尾回归：backtest_runs 引用冻结快照，并按引擎类型强制资格门槛。

规则（见 docs/quant-improvement-plan.md WP4 与设计 §8，R3 修订）：

- 只实现报告事后评估（`ai_report_evaluation`）；其它 `engine_kind` 一律被拒绝，
  因为旧引擎并不消费冻结行情，换个标签会让运行记录与真实计算不符。
- 报告评估可以附加 `snapshot_id` 作为**引用**（核对当时口径与质量），但证据里
  写明 `consumed=false`：引用不等于消费。
- 未知名/不存在的快照一律拒绝。
"""

import unittest
from datetime import date, timedelta

from src.repositories.market_snapshot_repo import (
    MarketDataSnapshotRepository,
    SnapshotRequest,
)
from src.services.backtest_service import BacktestService
from src.storage import DatabaseManager

START = date(2023, 9, 13)


def _bars(start: date, days: int):
    return [
        {"date": start + timedelta(days=index), "open": 10.0, "high": 11.0,
         "low": 9.0, "close": 10.0 + index * 0.01, "volume": 1000.0,
         "amount": 10000.0, "pct_chg": 0.1}
        for index in range(days)
    ]


class BacktestSnapshotLinkTestCase(unittest.TestCase):
    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")
        self.service = BacktestService(self.db)
        self.snapshots = MarketDataSnapshotRepository(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def _snapshot(self, *, volume_unit="shares", source="TencentFetcher", adjustment="provider_default"):
        return self.snapshots.create(SnapshotRequest(
            instrument="588000", market="cn", bars=_bars(START, 5),
            source=source, price_adjustment=adjustment,
            currency="CNY", volume_unit=volume_unit,
            requested_start=START, requested_end=START + timedelta(days=4),
        ))

    def _run(self, **kwargs):
        params = dict(eval_window_days=3, min_age_days=0, limit=10)
        params.update(kwargs)
        return self.service.run_backtest(**params)

    # ------------------------------------------------------------------
    # 未实现引擎一律拒绝（不再允许「换个标签跑旧引擎」）
    # ------------------------------------------------------------------
    def test_unimplemented_engine_kind_is_rejected_without_snapshot(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._run(engine_kind="portfolio_daily")
        self.assertIn("is not implemented", str(ctx.exception))
        self.assertIn("portfolio_daily", str(ctx.exception))

    def test_unimplemented_engine_kind_is_rejected_even_with_eligible_snapshot(self) -> None:
        created = self._snapshot()
        self.assertTrue(created["input_eligibility"])

        with self.assertRaises(ValueError) as ctx:
            self._run(engine_kind="portfolio_daily", snapshot_id=created["snapshot_id"])
        self.assertIn("is not implemented", str(ctx.exception))

    def test_rejected_engine_kind_does_not_write_a_run_record(self) -> None:
        with self.assertRaises(ValueError):
            self._run(engine_kind="portfolio_daily")

        self.assertEqual(self.service.get_runs(), [])

    def test_report_evaluation_records_reference_without_claiming_consumption(self) -> None:
        created = self._snapshot()

        stats = self._run(snapshot_id=created["snapshot_id"])

        record = self.service.get_run(stats["run_id"])
        self.assertEqual(record["snapshot_id"], created["snapshot_id"])
        self.assertEqual(record["data_quality_status"], "verified")
        self.assertIs(record["input_eligibility"], True)
        self.assertEqual(record["engine_kind"], "ai_report_evaluation")
        self.assertIsNotNone(record["engine_version"])
        self.assertEqual(record["evidence"]["kind"], "ai_report_evaluation")
        snapshot_block = record["evidence"]["snapshot"]
        self.assertEqual(snapshot_block["snapshot_id"], created["snapshot_id"])
        self.assertIs(snapshot_block["consumed"], False)
        self.assertTrue(any(
            "not consumed" in limitation for limitation in record["evidence"]["limitations"]
        ))

    def test_unknown_snapshot_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._run(snapshot_id="0" * 32)
        self.assertIn("Unknown snapshot_id", str(ctx.exception))

    # ------------------------------------------------------------------
    # 报告评估保持向后兼容，但如实记录
    # ------------------------------------------------------------------
    def test_report_evaluation_keeps_legacy_behaviour_without_snapshot(self) -> None:
        stats = self._run()

        record = self.service.get_run(stats["run_id"])
        self.assertIsNone(record["snapshot_id"])
        self.assertIsNone(record["input_eligibility"])
        self.assertEqual(record["engine_kind"], "ai_report_evaluation")
        self.assertEqual(record["evidence"]["kind"], "ai_report_evaluation")

    def test_report_evaluation_allows_ineligible_snapshot_but_records_it(self) -> None:
        created = self._snapshot(volume_unit="unknown")

        stats = self._run(snapshot_id=created["snapshot_id"])

        record = self.service.get_run(stats["run_id"])
        self.assertEqual(record["snapshot_id"], created["snapshot_id"])
        self.assertEqual(record["data_quality_status"], "partial")
        self.assertIs(record["input_eligibility"], False)
        self.assertEqual(record["engine_kind"], "ai_report_evaluation")

    def test_run_record_schema_exposes_snapshot_fields(self) -> None:
        from api.v1.schemas.backtest import BacktestRunRecord

        created = self._snapshot()
        stats = self._run(snapshot_id=created["snapshot_id"])
        record = self.service.get_run(stats["run_id"])

        dumped = BacktestRunRecord.model_validate(record).model_dump()
        for key in ("snapshot_id", "data_quality_status", "input_eligibility",
                    "engine_kind", "engine_version"):
            self.assertIn(key, BacktestRunRecord.model_fields, msg=f"{key} 未在 schema 声明")
            self.assertIsNotNone(dumped[key], msg=f"{key} 被 response_model 剥离")


if __name__ == "__main__":
    unittest.main()
