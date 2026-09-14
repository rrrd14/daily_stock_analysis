# -*- coding: utf-8 -*-
"""WP4 覆盖判定补充回归：按交易日历核对 session 数（R7 剩余项）。

要点：

- 日历可用 → 区间应有 session 数参与覆盖判定，能抓「首尾齐全但中间缺一大段」；
- 日历不可用 → ``basis=endpoints_only``，行为与之前一致，不伪装成已核对完整；
- 赤字容差只吸收日历噪声，不放过大段缺失。

全部用例离线、确定性：日历行为用 patch 注入，不依赖真实交易日历数据。
"""

import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import src.core.trading_calendar as trading_calendar
from src.repositories import market_snapshot_repo
from src.repositories.market_snapshot_repo import (
    SESSION_DEFICIT_TOLERANCE_RATIO,
    MarketDataSnapshotRepository,
    SnapshotRequest,
    compute_coverage,
    describe_coverage,
)
from src.storage import DatabaseManager

START = date(2023, 9, 13)  # 周三
END_WITH_WEEKEND = date(2023, 9, 17)  # 周日（首尾判定要能容忍周末/休市）


def _bars(start: date, days: int):
    return [
        {
            "date": start + timedelta(days=index),
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0 + index * 0.01,
            "volume": 1000.0,
            "amount": 10000.0,
            "pct_chg": 0.1,
        }
        for index in range(days)
    ]


class _FakeCalendar:
    """最小 fake 日历：只实现 count_sessions 需要的 ``sessions_in_range``。"""

    def __init__(self, sessions):
        self._sessions = list(sessions)

    def sessions_in_range(self, start, end):
        return [session for session in self._sessions if start <= session <= end]


def _fake_xcals(calendar):
    return SimpleNamespace(get_calendar=lambda _exchange: calendar)


class CountSessionsTestCase(unittest.TestCase):
    """``trading_calendar.count_sessions``：拿不到就返回 None，绝不猜测。"""

    def test_returns_none_when_calendar_unavailable(self) -> None:
        with patch.object(trading_calendar, "_XCALS_AVAILABLE", False):
            self.assertIsNone(trading_calendar.count_sessions("cn", START, END_WITH_WEEKEND))

    def test_returns_none_for_unknown_market(self) -> None:
        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar, "xcals", _fake_xcals(_FakeCalendar([])), create=True,
        ):
            self.assertIsNone(trading_calendar.count_sessions(None, START, END_WITH_WEEKEND))
            self.assertIsNone(trading_calendar.count_sessions("jp", START, END_WITH_WEEKEND))

    def test_returns_none_for_inverted_range(self) -> None:
        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar, "xcals", _fake_xcals(_FakeCalendar([])), create=True,
        ):
            self.assertIsNone(trading_calendar.count_sessions("cn", END_WITH_WEEKEND, START))

    def test_counts_sessions_from_calendar(self) -> None:
        sessions = [datetime(2023, 9, day) for day in (13, 14, 15)]
        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar, "xcals", _fake_xcals(_FakeCalendar(sessions)), create=True,
        ):
            self.assertEqual(
                trading_calendar.count_sessions("cn", START, END_WITH_WEEKEND), 3
            )

    def test_returns_none_when_calendar_raises(self) -> None:
        def boom(_exchange):
            raise RuntimeError("date outside calendar range")

        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar, "xcals", SimpleNamespace(get_calendar=boom), create=True,
        ):
            self.assertIsNone(trading_calendar.count_sessions("cn", START, END_WITH_WEEKEND))



class ComputeCoverageSessionParityTestCase(unittest.TestCase):
    """首尾齐全但会话数大段缺失 → 覆盖不完整；小缺口属于日历噪声。"""

    def test_endpoint_complete_but_large_deficit_is_not_complete(self) -> None:
        self.assertFalse(compute_coverage(
            START, END_WITH_WEEKEND, START, END_WITH_WEEKEND,
            rows=6, expected_sessions=250,
        ))

    def test_small_deficit_within_tolerance_is_complete(self) -> None:
        self.assertTrue(compute_coverage(
            START, END_WITH_WEEKEND, START, END_WITH_WEEKEND,
            rows=95, expected_sessions=100,
        ))

    def test_deficit_tolerance_is_proportional(self) -> None:
        expected = 20
        allowed = int(expected * SESSION_DEFICIT_TOLERANCE_RATIO)
        self.assertTrue(compute_coverage(
            START, END_WITH_WEEKEND, START, END_WITH_WEEKEND,
            rows=expected - allowed, expected_sessions=expected,
        ))
        self.assertFalse(compute_coverage(
            START, END_WITH_WEEKEND, START, END_WITH_WEEKEND,
            rows=expected - allowed - 1, expected_sessions=expected,
        ))

    def test_missing_expected_sessions_keeps_endpoints_only_behaviour(self) -> None:
        self.assertTrue(compute_coverage(
            START, END_WITH_WEEKEND, START, END_WITH_WEEKEND,
            rows=1, expected_sessions=None,
        ))


class DescribeCoverageTestCase(unittest.TestCase):
    """覆盖依据必须可审计：区分「按日历核对」与「只看首尾」。"""

    def test_marks_endpoints_only_when_calendar_unavailable(self) -> None:
        detail = describe_coverage(
            requested_start=START, requested_end=END_WITH_WEEKEND,
            rows=5, expected_sessions=None,
        )
        self.assertEqual(detail["basis"], "endpoints_only")
        self.assertIsNone(detail["expected_sessions"])
        self.assertIsNone(detail["deficit"])

    def test_reports_deficit_when_calendar_available(self) -> None:
        detail = describe_coverage(
            requested_start=START, requested_end=END_WITH_WEEKEND,
            rows=5, expected_sessions=22,
        )
        self.assertEqual(detail["basis"], "trading_calendar")
        self.assertEqual(detail["expected_sessions"], 22)
        self.assertEqual(detail["deficit"], 17)

    def test_never_reports_negative_deficit(self) -> None:
        detail = describe_coverage(
            requested_start=START, requested_end=END_WITH_WEEKEND,
            rows=30, expected_sessions=22,
        )
        self.assertEqual(detail["deficit"], 0)



class SnapshotCoverageEvidenceTestCase(unittest.TestCase):
    """落到快照记录上的会话数证据与质量判定。"""

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
            requested_end=END_WITH_WEEKEND,
        )
        params.update(overrides)
        return SnapshotRequest(**params)

    def test_mid_range_gap_is_not_marked_complete(self) -> None:
        """首尾各有几根、中间整段缺失：首尾判定会放过，会话数判定必须拦住。"""
        requested_end = date(2024, 9, 11)
        bars = _bars(START, 3) + _bars(requested_end - timedelta(days=2), 3)
        with patch.object(market_snapshot_repo, "count_sessions", return_value=250):
            created = self.repo.create(self._request(bars=bars, requested_end=requested_end))

        self.assertFalse(created["coverage_complete"])
        self.assertEqual(created["data_quality_status"], "partial")
        self.assertFalse(created["input_eligibility"])
        coverage = created["quality"]["coverage"]
        self.assertEqual(coverage["basis"], "trading_calendar")
        self.assertEqual(coverage["expected_sessions"], 250)
        self.assertEqual(coverage["deficit"], 244)
        self.assertIn("coverage", created["quality"]["missing"])

    def test_endpoints_only_basis_when_calendar_unavailable(self) -> None:
        with patch.object(market_snapshot_repo, "count_sessions", return_value=None):
            created = self.repo.create(self._request())

        self.assertTrue(created["coverage_complete"])
        self.assertEqual(created["data_quality_status"], "verified")
        coverage = created["quality"]["coverage"]
        self.assertEqual(coverage["basis"], "endpoints_only")
        self.assertIsNone(coverage["expected_sessions"])
        self.assertIsNone(coverage["deficit"])

    def test_rows_matching_sessions_stays_verified(self) -> None:
        with patch.object(market_snapshot_repo, "count_sessions", return_value=3):
            created = self.repo.create(self._request())

        self.assertEqual(created["data_quality_status"], "verified")
        self.assertTrue(created["coverage_complete"])
        self.assertEqual(created["quality"]["coverage"]["deficit"], 0)

    def test_session_check_is_skipped_for_non_daily_interval(self) -> None:
        with patch.object(market_snapshot_repo, "count_sessions") as fake_count:
            created = self.repo.create(self._request(interval="weekly"))

        fake_count.assert_not_called()
        self.assertEqual(created["quality"]["coverage"]["basis"], "endpoints_only")

    def test_coverage_evidence_does_not_change_snapshot_identity(self) -> None:
        """同一份行情与口径：会话数证据不影响身份，幂等语义保持。"""
        with patch.object(market_snapshot_repo, "count_sessions", return_value=3):
            first = self.repo.create(self._request())
        with patch.object(market_snapshot_repo, "count_sessions", return_value=250):
            second = self.repo.create(self._request())

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["payload_hash"], second["payload_hash"])


if __name__ == "__main__":
    unittest.main()
