# -*- coding: utf-8 -*-
"""WP4 闭环回归：快照只读 API、Agent 工具与体积预算。

覆盖：

- `GET /backtest/snapshots` 只返回元数据，`GET /backtest/snapshots/{id}` 可选返回明细，未知 ID 404
- 不合格快照在响应中暴露 `data_quality_status` 与缺失原因
- 单份 payload 超预算时拒绝写入；`storage_stats()` 报告规模
- Agent 只读工具 `get_market_snapshot` 的列表/详情/不存在分支
"""

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from src.repositories.market_snapshot_repo import (
    MarketDataSnapshotRepository,
    SnapshotRequest,
)
from src.storage import DatabaseManager

START = date(2023, 9, 13)


def _bars(start: date, days: int = 5):
    return [
        {"date": start + timedelta(days=index), "open": 10.0, "high": 11.0,
         "low": 9.0, "close": 10.0 + index * 0.01, "volume": 1000.0,
         "amount": 10000.0, "pct_chg": 0.1}
        for index in range(days)
    ]


def _request(**overrides) -> SnapshotRequest:
    params = dict(
        instrument="588000", market="cn", bars=_bars(START),
        source="TencentFetcher", price_adjustment="provider_default",
        currency="CNY", volume_unit="shares",
        requested_start=START, requested_end=START + timedelta(days=4),
    )
    params.update(overrides)
    return SnapshotRequest(**params)


class MarketSnapshotApiTestCase(unittest.TestCase):
    """API 测试使用文件型 SQLite：TestClient 在工作线程执行，内存库不跨线程共享。"""

    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "snapshot_api.db")
        self.db = DatabaseManager(db_url=f"sqlite:///{db_path}")
        self.repo = MarketDataSnapshotRepository(self.db)
        self.snapshot = self.repo.create(_request())

    def tearDown(self) -> None:
        # 先释放连接再删目录，避免 Windows 文件占用
        DatabaseManager.reset_instance()
        self._temp_dir.cleanup()

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.deps import get_database_manager
        from api.v1.endpoints.backtest import router

        app = FastAPI()
        app.include_router(router, prefix="/backtest")
        app.dependency_overrides[get_database_manager] = lambda: self.db
        return TestClient(app)

    def test_list_returns_metadata_without_bars(self) -> None:
        with self._client() as client:
            response = client.get("/backtest/snapshots", params={"instrument": "588000"})

        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["snapshot_id"], self.snapshot["snapshot_id"])
        self.assertEqual(items[0]["data_quality_status"], "verified")
        self.assertNotIn("bars", items[0])

    def test_detail_returns_bars_only_when_requested_and_404_otherwise(self) -> None:
        snapshot_id = self.snapshot["snapshot_id"]
        with self._client() as client:
            meta = client.get(f"/backtest/snapshots/{snapshot_id}").json()
            detail = client.get(
                f"/backtest/snapshots/{snapshot_id}", params={"include_bars": True}
            ).json()
            missing = client.get("/backtest/snapshots/does-not-exist")

        self.assertIsNone(meta.get("bars"))
        self.assertEqual(len(detail["bars"]), 5)
        self.assertIs(detail["input_eligibility"], True)
        self.assertEqual(missing.status_code, 404)

    def test_ineligible_snapshot_exposes_status_and_missing_reasons(self) -> None:
        created = self.repo.create(_request(volume_unit="unknown"))

        with self._client() as client:
            payload = client.get(f"/backtest/snapshots/{created['snapshot_id']}").json()

        self.assertEqual(payload["data_quality_status"], "partial")
        self.assertFalse(payload["input_eligibility"])


class SnapshotBudgetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def test_payload_over_budget_is_rejected(self) -> None:
        repo = MarketDataSnapshotRepository(self.db, max_payload_bytes=50)

        with self.assertRaises(ValueError) as ctx:
            repo.create(_request())

        self.assertIn("exceeds the size budget", str(ctx.exception))
        self.assertEqual(repo.storage_stats()["rows"], 0)

    def test_storage_stats_reports_usage(self) -> None:
        repo = MarketDataSnapshotRepository(self.db)
        repo.create(_request())

        stats = repo.storage_stats()

        self.assertEqual(stats["rows"], 1)
        self.assertGreater(stats["payload_bytes"], 0)
        self.assertTrue(stats["within_budget"])
        self.assertEqual(stats["instruments"], ["588000"])
        self.assertEqual(stats["max_payload_bytes"], repo.max_payload_bytes)


class SnapshotAgentToolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")
        self.repo = MarketDataSnapshotRepository(self.db)
        self.snapshot = self.repo.create(_request())

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def _call(self, **kwargs):
        from src.agent.tools.backtest_tools import _handle_get_market_snapshot

        with patch("src.storage.DatabaseManager.get_instance", return_value=self.db):
            return _handle_get_market_snapshot(**kwargs)

    def test_lists_recent_snapshots_without_mutation(self) -> None:
        result = self._call(instrument="588000", limit=5)

        self.assertEqual(len(result["snapshots"]), 1)
        self.assertEqual(result["snapshots"][0]["snapshot_id"], self.snapshot["snapshot_id"])
        # 只读：不应新增快照
        self.assertEqual(self.repo.storage_stats()["rows"], 1)

    def test_detail_includes_quality_and_api_path(self) -> None:
        result = self._call(snapshot_id=self.snapshot["snapshot_id"])

        self.assertEqual(result["snapshot_id"], self.snapshot["snapshot_id"])
        self.assertTrue(result["input_eligibility"])
        self.assertEqual(
            result["api_path"], f"/api/v1/backtest/snapshots/{self.snapshot['snapshot_id']}"
        )

    def test_unknown_snapshot_reports_not_found(self) -> None:
        result = self._call(snapshot_id="0" * 32)

        self.assertEqual(result["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
