# -*- coding: utf-8 -*-
"""WP4 第一阶段回归：不可变行情冻结快照与数据质量门槛（R7）。

验收对照（见 docs/quant-improvement-plan.md WP4）：

- 同源不同复权 → 不同快照身份
- 重复写入幂等、既有快照不被覆盖（重拉后旧记录仍可重放）
- 请求三年但数据只有一段 → 不得标成覆盖完整
- 口径/来源未知的快照不得用于默认策略收益计算

全部用例离线、确定性，使用内存 SQLite。
"""

import hashlib
import unittest
from datetime import date, timedelta

from src.repositories.market_snapshot_repo import (
    MarketDataSnapshotRepository,
    SnapshotRequest,
    assess_data_quality,
    compute_coverage,
    ensure_input_eligible,
    normalize_bars,
    stable_json,
)
from src.storage import DatabaseManager

START = date(2023, 9, 13)
END = date(2026, 9, 11)


def _bars(start: date, days: int, base: float = 10.0):
    return [
        {
            "date": start + timedelta(days=index),
            "open": base,
            "high": base + 1,
            "low": base - 1,
            "close": base + index * 0.01,
            "volume": 1000.0,
            "amount": 10000.0,
            "pct_chg": 0.1,
        }
        for index in range(days)
    ]


class SnapshotRepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")
        self.repo = MarketDataSnapshotRepository(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def _request(self, **overrides) -> SnapshotRequest:
        params = dict(
            instrument="588000",
            market="cn",
            bars=_bars(START, 5),
            source="YfinanceFetcher",
            price_adjustment="provider_default",
            currency="CNY",
            volume_unit="shares",
            requested_start=START,
            requested_end=START + timedelta(days=4),
        )
        params.update(overrides)
        return SnapshotRequest(**params)

    def test_same_source_different_adjustment_yields_different_identity(self) -> None:
        raw = self.repo.create(self._request(price_adjustment="provider_default"))
        qfq = self.repo.create(self._request(price_adjustment="qfq"))

        self.assertNotEqual(raw["snapshot_id"], qfq["snapshot_id"])
        self.assertEqual(len(self.repo.list(instrument="588000")), 2)
        self.assertEqual(raw["price_adjustment"], "provider_default")
        self.assertEqual(qfq["price_adjustment"], "qfq")

    def test_create_is_idempotent_and_keeps_first_created_at(self) -> None:
        first = self.repo.create(self._request())
        second = self.repo.create(self._request())

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(first["payload_hash"], second["payload_hash"])
        self.assertEqual(len(self.repo.list(instrument="588000")), 1)

    def test_payload_hash_matches_stable_serialization(self) -> None:
        created = self.repo.create(self._request())
        expected = hashlib.sha256(
            stable_json(normalize_bars(_bars(START, 5))).encode("utf-8")
        ).hexdigest()

        self.assertEqual(created["payload_hash"], expected)

    def test_reload_does_not_mutate_or_hide_existing_snapshot(self) -> None:
        original = self.repo.create(self._request())
        changed_bars = _bars(START, 5, base=20.0)
        reloaded = self.repo.create(self._request(bars=changed_bars))

        self.assertNotEqual(original["snapshot_id"], reloaded["snapshot_id"])
        replay = self.repo.get(original["snapshot_id"], detail=True)
        self.assertEqual(replay["payload_hash"], original["payload_hash"])
        self.assertEqual(replay["bars"], json_ready(normalize_bars(_bars(START, 5))))

    def test_repository_exposes_no_mutation_api(self) -> None:
        self.assertFalse(hasattr(self.repo, "update"))
        self.assertFalse(hasattr(self.repo, "delete"))
        self.assertFalse(hasattr(self.repo, "upsert"))


def json_ready(value):
    """把 datetime.date 归一为 ISO 字符串，便于与落库 JSON 比较。"""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


class SnapshotQualityGateTestCase(unittest.TestCase):
    """口径/覆盖未知时不得提升为已验证，也不得用于策略收益计算。"""

    def test_unknown_volume_unit_is_partial_and_not_eligible(self) -> None:
        quality = assess_data_quality(
            source="TencentFetcher", price_adjustment="provider_default",
            currency="CNY", volume_unit="unknown", coverage_complete=True,
        )
        self.assertEqual(quality["data_quality_status"], "partial")
        self.assertFalse(quality["input_eligibility"])
        self.assertIn("volume_unit", quality["missing"])

    def test_unknown_source_is_unknown_status(self) -> None:
        quality = assess_data_quality(
            source=None, price_adjustment="provider_default",
            currency="CNY", volume_unit="shares", coverage_complete=True,
        )
        self.assertEqual(quality["data_quality_status"], "unknown")
        self.assertFalse(quality["input_eligibility"])

    def test_unknown_adjustment_is_unknown_status(self) -> None:
        quality = assess_data_quality(
            source="TencentFetcher", price_adjustment="unknown",
            currency="CNY", volume_unit="shares", coverage_complete=True,
        )
        self.assertEqual(quality["data_quality_status"], "unknown")
        self.assertFalse(quality["input_eligibility"])

    def test_incomplete_coverage_is_never_verified(self) -> None:
        quality = assess_data_quality(
            source="TencentFetcher", price_adjustment="provider_default",
            currency="CNY", volume_unit="shares", coverage_complete=False,
        )
        self.assertEqual(quality["data_quality_status"], "partial")
        self.assertFalse(quality["input_eligibility"])
        self.assertIn("coverage", quality["missing"])

    def test_all_known_and_complete_is_verified(self) -> None:
        quality = assess_data_quality(
            source="TencentFetcher", price_adjustment="provider_default",
            currency="CNY", volume_unit="shares", coverage_complete=True,
        )
        self.assertEqual(quality["data_quality_status"], "verified")
        self.assertTrue(quality["input_eligibility"])
        self.assertEqual(quality["missing"], [])

    def test_coverage_requires_explicit_requested_range(self) -> None:
        self.assertFalse(compute_coverage(START, END, None, None))
        self.assertFalse(compute_coverage(None, None, START, END))
        self.assertFalse(compute_coverage(START, START, START, END))
        self.assertTrue(compute_coverage(START, END, START, END))


class SnapshotEligibilityGuardTestCase(unittest.TestCase):
    """快照落库后：verified 可重放，unknown 必须在策略入口被拒绝。"""

    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")
        self.repo = MarketDataSnapshotRepository(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def _request(self, **overrides) -> SnapshotRequest:
        params = dict(
            instrument="588000",
            market="cn",
            bars=_bars(START, 5),
            source="TencentFetcher",
            price_adjustment="provider_default",
            currency="CNY",
            volume_unit="shares",
            requested_start=START,
            requested_end=START + timedelta(days=4),
        )
        params.update(overrides)
        return SnapshotRequest(**params)

    def test_verified_snapshot_is_eligible_and_replayable(self) -> None:
        created = self.repo.create(self._request())
        self.assertEqual(created["data_quality_status"], "verified")
        self.assertTrue(created["coverage_complete"])

        stored = self.repo.get(created["snapshot_id"], detail=True)
        ensure_input_eligible(stored)
        self.assertEqual(stored["bars"], json_ready(normalize_bars(_bars(START, 5))))

    def test_unknown_snapshot_cannot_be_used_for_strategy(self) -> None:
        created = self.repo.create(self._request(volume_unit="unknown"))
        stored = self.repo.get(created["snapshot_id"], detail=True)

        self.assertFalse(stored["input_eligibility"])
        with self.assertRaises(ValueError) as ctx:
            ensure_input_eligible(stored)
        self.assertIn("not eligible", str(ctx.exception))

    def test_requested_three_years_is_not_marked_complete(self) -> None:
        created = self.repo.create(self._request(
            bars=_bars(START, 30),
            requested_start=START,
            requested_end=END,
        ))

        self.assertFalse(created["coverage_complete"])
        self.assertNotEqual(created["data_quality_status"], "verified")
        self.assertFalse(created["input_eligibility"])
        self.assertIn("coverage", created["quality"]["missing"])


if __name__ == "__main__":
    unittest.main()
