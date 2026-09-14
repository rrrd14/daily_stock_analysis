# -*- coding: utf-8 -*-
"""WP1 回归：指标有效性与评分保护（R1 / R5）。

覆盖 `.claude/reviews/current-review/REVIEW.md` 的确定性缺陷：

- R1：缺失指标仍参与正向评分，且上游风险被清空
- R5a：前5个交易日成交量缺一条时，分析器与基础指标入口判定不一致
- R5b：收盘价窗口存在缺口时仍产出看似有效的 RSI/MACD

这些用例全部离线、确定性，不访问网络或真实行情。
"""

import json
import math
import unittest
from unittest.mock import patch

import pandas as pd

from data_provider.base import BaseFetcher
from src.stock_analyzer import (
    BuySignal,
    IndicatorValidity,
    SignalStatus,
    StockTrendAnalyzer,
)
from src.agent.tools.analysis_tools import _handle_analyze_trend

REQUIRED_KEYS = ("ma", "volume", "macd", "rsi")


def _frame(rows: int = 40) -> pd.DataFrame:
    """构造缓慢上升、成交量恒定的确定性行情。"""
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=rows),
        "open": [10 + i * 0.01 for i in range(rows)],
        "high": [11.0] * rows,
        "low": [9.0] * rows,
        "close": [10 + i * 0.01 for i in range(rows)],
        "volume": [100.0] * rows,
    })


class MissingIndicatorScoringTestCase(unittest.TestCase):
    """R1：required 指标缺失时不得产出「强烈买入」。"""

    def test_all_nan_volume_does_not_produce_actionable_buy(self) -> None:
        df = _frame(20)
        df["volume"] = float("nan")

        result = StockTrendAnalyzer().analyze(df, "588000")

        self.assertIsNone(result.volume_ratio_5d)
        self.assertIsNone(result.macd_dif)
        self.assertIsNone(result.rsi_12)
        self.assertEqual(result.signal_status, SignalStatus.INSUFFICIENT_DATA)
        self.assertFalse(result.actionable)
        self.assertEqual(result.score_status, "partial")
        self.assertNotIn(result.buy_signal, (BuySignal.BUY, BuySignal.STRONG_BUY))

    def test_missing_indicator_risks_are_preserved(self) -> None:
        """上游写入的缺口提示不能被评分分支清空。"""
        df = _frame(20)
        df["volume"] = float("nan")

        result = StockTrendAnalyzer().analyze(df, "588000")
        joined = " ".join(result.risk_factors)

        self.assertIn("MA60", joined)
        self.assertIn("前5个交易日", joined)
        self.assertIn("MACD", joined)
        self.assertIn("RSI", joined)
        self.assertTrue(
            any("不生成可执行" in item for item in result.risk_factors),
            msg=f"expected an actionable=false note in {result.risk_factors}",
        )

    def test_indicator_quality_is_explicit(self) -> None:
        df = _frame(20)
        df["volume"] = float("nan")

        quality = StockTrendAnalyzer().analyze(df, "588000").indicator_quality

        self.assertEqual(quality["volume"], IndicatorValidity.INSUFFICIENT.value)
        self.assertEqual(quality["macd"], IndicatorValidity.INSUFFICIENT.value)
        self.assertEqual(quality["rsi"], IndicatorValidity.INSUFFICIENT.value)
        self.assertEqual(quality["ma"], IndicatorValidity.VALID.value)

    def test_frame_below_20_rows_is_not_actionable(self) -> None:
        result = StockTrendAnalyzer().analyze(_frame(10), "588000")

        self.assertEqual(result.signal_status, SignalStatus.INSUFFICIENT_DATA)
        self.assertFalse(result.actionable)
        self.assertEqual(result.buy_signal, BuySignal.HOLD)
        for key in REQUIRED_KEYS:
            self.assertEqual(
                result.indicator_quality[key], IndicatorValidity.INSUFFICIENT.value
            )


class PartialWindowConsistencyTestCase(unittest.TestCase):
    """R5a：前5日成交量缺一条时两个入口必须一致地判定为不足。"""

    def test_partial_prior_volume_window_is_insufficient_in_both_entries(self) -> None:
        df = _frame(40)
        df.loc[37, "volume"] = float("nan")
        df.loc[39, "volume"] = 200.0

        analyzer_result = StockTrendAnalyzer().analyze(df, "588000")
        base_ratio = BaseFetcher._calculate_indicators(df)["volume_ratio"].iloc[-1]

        self.assertIsNone(analyzer_result.volume_ratio_5d)
        self.assertTrue(
            base_ratio is None or (isinstance(base_ratio, float) and math.isnan(base_ratio)),
            msg=f"base volume_ratio should be unusable, got {base_ratio!r}",
        )
        self.assertEqual(
            analyzer_result.indicator_quality["volume"],
            IndicatorValidity.INSUFFICIENT.value,
        )

    def test_zero_denominator_never_yields_infinity(self) -> None:
        df = _frame(40)
        df.loc[34:38, "volume"] = 0.0

        base_ratio = BaseFetcher._calculate_indicators(df)["volume_ratio"].iloc[-1]

        self.assertFalse(
            isinstance(base_ratio, float) and math.isinf(base_ratio),
            msg="volume_ratio must not be Infinity when the denominator is zero",
        )


class PriceGapValidityTestCase(unittest.TestCase):
    """R5b：窗口内价格缺口不得被认证为有效 RSI/MACD。"""

    def test_internal_close_gap_invalidates_rsi_and_macd(self) -> None:
        df = _frame(40)
        df.loc[36, "close"] = float("nan")

        result = StockTrendAnalyzer().analyze(df, "588000")

        self.assertIsNone(result.rsi_12)
        self.assertIsNone(result.macd_dif)
        self.assertEqual(result.indicator_quality["rsi"], IndicatorValidity.INVALID.value)
        self.assertEqual(result.indicator_quality["macd"], IndicatorValidity.INVALID.value)
        self.assertFalse(result.actionable)
        self.assertNotIn(result.buy_signal, (BuySignal.BUY, BuySignal.STRONG_BUY))


class CompleteDataScoringTestCase(unittest.TestCase):
    """完整有效输入的原有评分口径必须保持一致。"""

    def test_complete_frame_is_fully_valid_and_actionable(self) -> None:
        result = StockTrendAnalyzer().analyze(_frame(60), "600519")

        self.assertEqual(result.signal_status, SignalStatus.OK)
        self.assertTrue(result.actionable)
        self.assertEqual(result.score_status, "complete")
        for key in REQUIRED_KEYS:
            self.assertEqual(result.indicator_quality[key], IndicatorValidity.VALID.value)

    def test_complete_frame_score_is_stable(self) -> None:
        """锁定完整数据下的分数，防止有效性门控意外改变既有权重。

        组件：趋势(BULL)+26、乖离(+18)、量能(正常)+10、支撑(+10)、
        MACD(多头)+8、RSI(超买)+0 = 72。
        """
        result = StockTrendAnalyzer().analyze(_frame(60), "600519")

        self.assertEqual(result.signal_score, 72)
        self.assertEqual(result.buy_signal, BuySignal.BUY)


class JsonFinitenessTestCase(unittest.TestCase):
    """展示层不得输出 NaN / Infinity。"""

    def test_to_dict_values_are_finite(self) -> None:
        df = _frame(20)
        df["volume"] = float("nan")

        payload = StockTrendAnalyzer().analyze(df, "588000").to_dict()

        for key, value in payload.items():
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value), msg=f"{key} is not finite: {value!r}")

    def test_agent_tool_payload_is_strict_json(self) -> None:
        df = _frame(20)
        df["volume"] = float("nan")

        with patch(
            "src.agent.tools.analysis_tools._fetch_trend_data", return_value=df
        ):
            payload = _handle_analyze_trend("588000")

        # allow_nan=False 会在存在 NaN/Infinity 时抛错
        json.dumps(payload, allow_nan=False)
        self.assertFalse(payload["actionable"])
        self.assertEqual(payload["signal_status"], SignalStatus.INSUFFICIENT_DATA.value)


if __name__ == "__main__":
    unittest.main()
