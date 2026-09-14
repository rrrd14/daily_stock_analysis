# -*- coding: utf-8 -*-
"""WP3 回归：来源失败分类与显式来源不静默 fallback。

覆盖计划 WP3 的交付项：

- 来源失败分类为超时 / 网络 / 鉴权 / 权限额度 / 不支持 / 无效载荷，
  与「确实取到空数据」区分开；
- 不把一次超时等同于「该标的没有历史」；
- 显式来源模式下不悄悄 fallback 到其它来源，并保留 attempts 摘要。

全部用例离线、确定性，不访问网络。
"""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from data_provider.base import (
    FETCH_ERROR_CATEGORIES,
    DataFetcherManager,
    DataFetchError,
    classify_fetch_error,
)


def _fetcher(name: str, priority: int, start: str = "2023-09-13", rows: int = 10):
    return SimpleNamespace(
        name=name,
        priority=priority,
        get_daily_data=Mock(return_value=pd.DataFrame({
            "date": pd.date_range(start, periods=rows),
            "close": [1.0] * rows,
        })),
    )


class ClassifyFetchErrorTestCase(unittest.TestCase):
    """异常到稳定类别的映射。"""

    def test_known_categories(self) -> None:
        cases = {
            TimeoutError("request timed out"): "timeout",
            ConnectionResetError("connection reset by peer"): "network",
            RuntimeError("429 Too Many Requests"): "rate_limit",
            RuntimeError("401 Unauthorized"): "auth",
            PermissionError("permission denied"): "permission",
            NotImplementedError("not supported for hk"): "unsupported",
            ValueError("Invalid JSON response"): "invalid_payload",
            RuntimeError("plain boom"): "unknown",
        }
        for exc, expected in cases.items():
            with self.subTest(exc=type(exc).__name__, message=str(exc)):
                self.assertEqual(classify_fetch_error(exc), expected)

    def test_category_values_are_declared(self) -> None:
        self.assertIn(classify_fetch_error(RuntimeError("boom")), FETCH_ERROR_CATEGORIES)
        self.assertEqual(FETCH_ERROR_CATEGORIES[-1], "unknown")

    def test_category_never_leaks_exception_message(self) -> None:
        secret = "private-token-abc123"
        self.assertNotIn(secret, classify_fetch_error(RuntimeError(secret)))


class DailyHistoryFailureReasonTestCase(unittest.TestCase):
    """attempts 必须携带分类，且超时不被当作“无历史”。"""

    def test_timeout_is_classified_and_raises_instead_of_empty(self) -> None:
        timed_out = _fetcher("AkshareFetcher", 0)
        timed_out.get_daily_data.side_effect = TimeoutError("read timed out")
        attempts = []

        with self.assertRaises(DataFetchError):
            DataFetcherManager([timed_out]).get_daily_data(
                "600519", days=10, min_records=10, diagnostics=attempts,
            )

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["source"], "AkshareFetcher")
        self.assertEqual(attempts[0]["status"], "error")
        self.assertEqual(attempts[0]["error_type"], "TimeoutError")
        self.assertEqual(attempts[0]["reason"], "timeout")

    def test_unsupported_source_is_categorised(self) -> None:
        fetcher = _fetcher("AkshareFetcher", 0)
        fetcher.get_daily_data.side_effect = RuntimeError("该市场不支持 港股 日线")
        attempts = []

        with self.assertRaises(DataFetchError):
            DataFetcherManager([fetcher]).get_daily_data(
                "600519", days=10, min_records=10, diagnostics=attempts,
            )

        self.assertEqual(attempts[0]["reason"], "unsupported")

    def test_explicit_source_does_not_silently_fallback(self) -> None:
        requested = _fetcher("BaostockFetcher", 0)
        requested.get_daily_data.side_effect = ConnectionResetError("connection reset")
        other = _fetcher("AkshareFetcher", 1)
        attempts = []

        with self.assertRaises(DataFetchError):
            DataFetcherManager([requested, other]).get_daily_data(
                "588000", source="BaostockFetcher", days=10, min_records=10,
                diagnostics=attempts,
            )

        other.get_daily_data.assert_not_called()
        self.assertEqual([item["source"] for item in attempts], ["BaostockFetcher"])
        self.assertEqual(attempts[0]["reason"], "network")


class HistoryLoaderTimeoutSurfacingTestCase(unittest.TestCase):
    """加载器层：一次超时必须能从 attempts 分辨，不能等价于“没有历史”。"""

    def test_timeout_is_surfaced_via_source_attempts(self) -> None:
        from src.services.history_loader import load_history_df

        def _explode(*args, **kwargs):
            kwargs["diagnostics"].append({
                "source": "AkshareFetcher",
                "status": "error",
                "error_type": "TimeoutError",
                "reason": classify_fetch_error(TimeoutError("read timed out")),
            })
            raise DataFetchError("all sources failed")

        manager = SimpleNamespace(get_daily_data=Mock(side_effect=_explode))
        with patch("src.storage.get_db") as db, \
             patch("src.services.history_loader._get_fetcher_manager", return_value=manager):
            db.return_value.get_data_range.return_value = []
            df, source = load_history_df("600519", days=60, target_date=date(2024, 1, 31))

        self.assertTrue(df is None or df.empty)
        self.assertEqual(source, "none")
        surfaced = (df.attrs or {}).get("source_attempts") if df is not None else None
        self.assertTrue(surfaced, msg="超时必须通过 source_attempts 暴露给调用方")
        self.assertEqual(surfaced[0]["reason"], "timeout")


if __name__ == "__main__":
    unittest.main()
