# -*- coding: utf-8 -*-
"""R2/R4 回归：行情行本身必须通过校验，且「要求」不得复用旧结论。

R2：缺失字段、NaN/Inf、日期非法或重复、价格关系不成立的行情**不得**获得
``verified`` 与策略输入资格，坏行/重复行也不能用来填满覆盖判定。

R4：``required_rows`` 与质量规则版本都影响持久化资格，因此必须进快照身份——
先以宽松要求创建、再以更严要求创建，第二次不能复用第一次的 ``eligible``。

全部用例离线、确定性（内存 SQLite + patch 日历）。
"""

import unittest
from datetime import date, timedelta
from unittest.mock import patch

import src.core.trading_calendar as trading_calendar
from src.repositories import market_snapshot_repo
from src.repositories.market_snapshot_repo import (
    SNAPSHOT_QC_VERSION,
    MarketDataSnapshotRepository,
    SnapshotRequest,
    validate_bars,
)
from src.storage import DatabaseManager

START = date(2023, 9, 13)  # 周三


def _row(day: date, **overrides):
    row = {
        "date": day, "open": 10.0, "high": 11.0, "low": 9.0,
        "close": 10.0, "volume": 1000.0, "amount": 10000.0, "pct_chg": 0.1,
    }
    row.update(overrides)
    return row


def _bars(start: date, days: int):
    return [_row(start + timedelta(days=index), close=10.0 + index * 0.01)
            for index in range(days)]


class ValidateBarsTestCase(unittest.TestCase):
    """校验函数本身：问题分类与有效交易日集合。"""

    def test_clean_bars_have_no_problems(self) -> None:
        result = validate_bars(_bars(START, 5))
        self.assertEqual(result["problems"], [])
        self.assertEqual(len(result["sessions"]), 5)

    def test_nan_close_is_non_finite_and_not_a_session(self) -> None:
        bars = [_row(START + timedelta(days=index), close=float("nan")) for index in range(5)]
        result = validate_bars(bars)
        self.assertIn("non_finite", result["problems"])
        self.assertEqual(result["sessions"], [])

    def test_missing_price_fields_are_reported(self) -> None:
        bars = [{"date": START + timedelta(days=index)} for index in range(5)]
        result = validate_bars(bars)
        self.assertIn("missing_field", result["problems"])
        self.assertEqual(result["sessions"], [])

    def test_duplicate_dates_are_reported_and_counted_once(self) -> None:
        bars = [_row(START) for _ in range(4)] + [_row(START + timedelta(days=4))]
        result = validate_bars(bars)
        self.assertIn("duplicate_date", result["problems"])
        self.assertEqual(result["sessions"], [START, START + timedelta(days=4)])

    def test_invalid_date_is_reported(self) -> None:
        bars = [_row(START), dict(_row(START + timedelta(days=1)), date="not-a-date")]
        result = validate_bars(bars)
        self.assertIn("invalid_date", result["problems"])

    def test_price_relation_is_reported(self) -> None:
        bars = [_row(START, high=8.0, low=9.0), _row(START + timedelta(days=1), close=20.0)]
        result = validate_bars(bars)
        self.assertIn("price_relation", result["problems"])

    def test_rows_outside_requested_range_are_ignored(self) -> None:
        bars = _bars(START, 5)
        result = validate_bars(
            bars, requested_start=START, requested_end=START + timedelta(days=1),
        )
        self.assertEqual(result["problems"], [])
        self.assertEqual(len(result["sessions"]), 2)


    def test_non_positive_price_is_reported(self) -> None:
        bars = [
            _row(START, open=0.0, high=0.0, low=0.0, close=0.0),
            _row(START + timedelta(days=1)),
        ]
        result = validate_bars(bars)
        self.assertIn("non_positive_price", result["problems"])
        self.assertEqual(result["sessions"], [START + timedelta(days=1)])


class SnapshotPayloadQualityTestCase(unittest.TestCase):
    """落库门槛：坏数据不得取得 verified / 策略资格。"""

    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")
        self.repo = MarketDataSnapshotRepository(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def _request(self, **overrides) -> SnapshotRequest:
        params = dict(
            instrument="588000", market="cn", bars=_bars(START, 5),
            source="TencentFetcher", price_adjustment="provider_default",
            currency="CNY", volume_unit="shares",
            requested_start=START, requested_end=START + timedelta(days=4),
        )
        params.update(overrides)
        return SnapshotRequest(**params)

    def test_clean_payload_stays_verified(self) -> None:
        created = self.repo.create(self._request())
        self.assertEqual(created["data_quality_status"], "verified")
        self.assertTrue(created["input_eligibility"])
        self.assertEqual(created["quality"]["payload"]["problems"], [])
        self.assertEqual(created["quality"]["payload"]["qc_version"], SNAPSHOT_QC_VERSION)

    def test_all_nan_prices_are_not_verified_or_eligible(self) -> None:
        bars = [_row(START + timedelta(days=index), close=float("nan")) for index in range(5)]
        created = self.repo.create(self._request(bars=bars))

        self.assertEqual(created["data_quality_status"], "unknown")
        self.assertFalse(created["input_eligibility"])
        self.assertFalse(created["coverage_complete"])
        self.assertIn("non_finite", created["quality"]["missing"])
        self.assertEqual(created["quality"]["payload"]["valid_sessions"], 0)

    def test_rows_without_prices_are_not_verified_or_eligible(self) -> None:
        bars = [{"date": START + timedelta(days=index)} for index in range(5)]
        created = self.repo.create(self._request(bars=bars))

        self.assertEqual(created["data_quality_status"], "unknown")
        self.assertFalse(created["input_eligibility"])
        self.assertIn("missing_field", created["quality"]["missing"])

    def test_duplicate_dates_cannot_fill_expected_sessions(self) -> None:
        bars = [_row(START) for _ in range(4)] + [_row(START + timedelta(days=4))]
        with patch.object(market_snapshot_repo, "count_sessions", return_value=5):
            created = self.repo.create(self._request(bars=bars))

        self.assertEqual(created["data_quality_status"], "unknown")
        self.assertFalse(created["input_eligibility"])
        self.assertFalse(created["coverage_complete"])
        self.assertIn("duplicate_date", created["quality"]["missing"])
        self.assertEqual(created["quality"]["coverage"]["counted_sessions"], 2)
        self.assertEqual(created["quality"]["coverage"]["rows"], 5)



    def test_zero_price_is_not_verified_or_eligible(self) -> None:
        bars = [
            _row(START, open=0.0, high=0.0, low=0.0, close=0.0),
            _row(START + timedelta(days=1)),
        ]
        created = self.repo.create(self._request(bars=bars))

        self.assertEqual(created["data_quality_status"], "unknown")
        self.assertFalse(created["input_eligibility"])
        self.assertIn("non_positive_price", created["quality"]["missing"])


class SnapshotRequirementIdentityTestCase(unittest.TestCase):
    """R4：影响资格的要求必须进身份，且不得靠写入顺序决定结论。"""

    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")
        self.repo = MarketDataSnapshotRepository(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def _request(self, **overrides) -> SnapshotRequest:
        params = dict(
            instrument="588000", market="cn", bars=_bars(START, 5),
            source="TencentFetcher", price_adjustment="provider_default",
            currency="CNY", volume_unit="shares",
            requested_start=START, requested_end=START + timedelta(days=4),
        )
        params.update(overrides)
        return SnapshotRequest(**params)

    def test_raising_required_rows_creates_a_distinct_snapshot(self) -> None:
        loose = self.repo.create(self._request(required_rows=5))
        self.assertTrue(loose["input_eligibility"])

        strict = self.repo.create(self._request(required_rows=100))

        self.assertNotEqual(loose["snapshot_id"], strict["snapshot_id"])
        self.assertFalse(strict["coverage_complete"])
        self.assertFalse(strict["input_eligibility"])
        self.assertIn("coverage", strict["quality"]["missing"])
        self.assertEqual(len(self.repo.list(instrument="588000")), 2)

    def test_requirement_order_does_not_change_the_verdict(self) -> None:
        strict = self.repo.create(self._request(required_rows=100))
        self.assertFalse(strict["input_eligibility"])

        loose = self.repo.create(self._request(required_rows=5))

        self.assertNotEqual(loose["snapshot_id"], strict["snapshot_id"])
        self.assertTrue(loose["input_eligibility"])

    def test_stricter_request_does_not_mutate_the_existing_snapshot(self) -> None:
        loose = self.repo.create(self._request(required_rows=5))
        self.repo.create(self._request(required_rows=100))

        stored = self.repo.get(loose["snapshot_id"])
        self.assertTrue(stored["input_eligibility"])
        self.assertTrue(stored["coverage_complete"])

    def test_identical_requirements_stay_idempotent(self) -> None:
        first = self.repo.create(self._request(required_rows=5))
        second = self.repo.create(self._request(required_rows=5))

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(len(self.repo.list(instrument="588000")), 1)


if __name__ == "__main__":
    unittest.main()
