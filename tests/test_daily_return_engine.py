# -*- coding: utf-8 -*-
"""WP4 第二阶段回归：`daily_return` 引擎真正消费冻结快照并如实记录回报率。

规则（见 docs/quant-improvement-plan.md WP4 与设计 §8）：

- 策略引擎必须引用 `input_eligibility=true` 的快照，且快照标的必须与请求 `code` 一致。
- 引擎从**冻结 bars**（而非 DB/抓取路径）计算日频简单收益率序列与总回报率。
- 证据里 `snapshot.consumed=true`、`bars_consumed` 与 `metrics`（含 `total_return`）
  可核对，`limitations` 如实写明「不建模资金/费用/滑点/持仓」。
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


def _bars(start: date, closes):
    return [
        {"date": start + timedelta(days=index), "open": close, "high": close + 1.0,
         "low": close - 1.0, "close": close, "volume": 1000.0,
         "amount": 10000.0, "pct_chg": 0.0}
        for index, close in enumerate(closes)
    ]


class DailyReturnEngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")
        self.service = BacktestService(self.db)
        self.snapshots = MarketDataSnapshotRepository(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def _snapshot(self, *, instrument="588000", closes=(100.0, 110.0, 121.0), **overrides):
        params = dict(
            instrument=instrument, market="cn", bars=_bars(START, closes),
            source="TencentFetcher", price_adjustment="provider_default",
            currency="CNY", volume_unit="shares",
            requested_start=START, requested_end=START + timedelta(days=len(closes) - 1),
        )
        params.update(overrides)
        return self.snapshots.create(SnapshotRequest(**params))

    # ------------------------------------------------------------------
    # 成功路径：从冻结 bars 计算回报率并如实记录
    # ------------------------------------------------------------------
    def test_daily_return_consumes_frozen_bars_and_computes_returns(self) -> None:
        created = self._snapshot()

        stats = self.service.run_backtest(
            code="588000", engine_kind="daily_return", snapshot_id=created["snapshot_id"],
        )

        record = self.service.get_run(stats["run_id"])
        self.assertEqual(record["engine_kind"], "daily_return")
        self.assertEqual(record["status"], "completed")

        evidence = record["evidence"]
        self.assertEqual(evidence["kind"], "daily_return")
        self.assertIs(evidence["snapshot"]["consumed"], True)
        self.assertEqual(evidence["snapshot"]["bars_consumed"], 3)
        self.assertEqual(evidence["snapshot"]["snapshot_id"], created["snapshot_id"])

        items = evidence["items"]
        self.assertEqual(len(items), 2)  # 3 个收盘价 → 2 个收益率观测
        self.assertAlmostEqual(items[0]["simple_return"], 0.1, places=12)
        self.assertAlmostEqual(items[1]["simple_return"], 0.1, places=12)
        self.assertEqual(items[0]["date"], "2023-09-14")
        self.assertEqual(items[1]["date"], "2023-09-15")

        self.assertEqual(evidence["metrics"]["bars"], 3)
        self.assertEqual(evidence["metrics"]["observations"], 2)
        self.assertAlmostEqual(evidence["metrics"]["first_close"], 100.0)
        self.assertAlmostEqual(evidence["metrics"]["last_close"], 121.0)
        self.assertAlmostEqual(evidence["metrics"]["total_return"], 0.21, places=12)
        self.assertIsInstance(evidence["metrics"]["annualized_return"], float)
        self.assertEqual(evidence["metrics"]["periods_per_year"], 252.0)
        self.assertTrue(any(
            "does not model" in limitation for limitation in evidence["limitations"]
        ))

    def test_daily_return_computes_annualized_return(self) -> None:
        """几何年化：total_return=0.21、2 个观测 → (1.21)^(252/2)-1。"""
        created = self._snapshot()  # closes 100 / 110 / 121
        stats = self.service.run_backtest(
            code="588000", engine_kind="daily_return", snapshot_id=created["snapshot_id"],
        )
        metrics = self.service.get_run(stats["run_id"])["evidence"]["metrics"]

        expected = (1.0 + 0.21) ** (252.0 / 2.0) - 1.0
        self.assertAlmostEqual(metrics["annualized_return"] / expected, 1.0, places=9)

    def test_daily_return_recomputes_from_snapshot_not_from_db(self) -> None:
        """冻结后改写快照之外的输入不影响结果：引擎只读冻结 bars。"""
        first = self._snapshot(closes=(100.0, 110.0, 121.0))
        # 另一个不同收盘价的快照，证明引擎按传入的 snapshot_id 取数，不混用。
        self._snapshot(closes=(100.0, 200.0, 300.0))

        stats = self.service.run_backtest(
            code="588000", engine_kind="daily_return", snapshot_id=first["snapshot_id"],
        )
        items = self.service.get_run(stats["run_id"])["evidence"]["items"]

        self.assertEqual(len(items), 2)
        self.assertAlmostEqual(items[0]["simple_return"], 0.1, places=12)
        self.assertAlmostEqual(items[1]["simple_return"], 0.1, places=12)


    # ------------------------------------------------------------------
    # 门槛：资格、标的、存在性
    # ------------------------------------------------------------------
    def test_daily_return_requires_an_eligible_snapshot(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.service.run_backtest(code="588000", engine_kind="daily_return")
        self.assertIn("eligible snapshot_id", str(ctx.exception))

    def test_daily_return_rejects_ineligible_snapshot(self) -> None:
        created = self._snapshot(volume_unit="unknown")  # partial
        self.assertFalse(created["input_eligibility"])

        with self.assertRaises(ValueError) as ctx:
            self.service.run_backtest(
                code="588000", engine_kind="daily_return",
                snapshot_id=created["snapshot_id"],
            )
        self.assertIn("not eligible", str(ctx.exception))

    def test_daily_return_rejects_instrument_mismatch(self) -> None:
        created = self._snapshot(instrument="588000")

        with self.assertRaises(ValueError) as ctx:
            self.service.run_backtest(
                code="601398", engine_kind="daily_return",
                snapshot_id=created["snapshot_id"],
            )
        self.assertIn("does not match", str(ctx.exception))

    def test_daily_return_rejects_unknown_snapshot_id(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.service.run_backtest(
                code="588000", engine_kind="daily_return", snapshot_id="0" * 32,
            )
        self.assertIn("Unknown snapshot_id", str(ctx.exception))

    def test_daily_return_writes_a_run_record_and_schema_survives(self) -> None:
        from api.v1.schemas.backtest import BacktestRunRecord

        created = self._snapshot()
        stats = self.service.run_backtest(
            code="588000", engine_kind="daily_return", snapshot_id=created["snapshot_id"],
        )
        record = self.service.get_run(stats["run_id"])

        dumped = BacktestRunRecord.model_validate(record).model_dump()
        self.assertIsNotNone(dumped["snapshot_id"])
        self.assertIs(dumped["input_eligibility"], True)
        self.assertEqual(dumped["engine_kind"], "daily_return")
        self.assertIsNotNone(dumped["engine_version"])


if __name__ == "__main__":
    unittest.main()
