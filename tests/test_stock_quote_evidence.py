# -*- coding: utf-8 -*-
"""WP3/R3 回归：实时行情证据链（adapter -> service -> schema）。

覆盖 `.claude/reviews/current-review/REVIEW.md` 的 R3：

- Web 行情 API 曾丢掉 quote_time/freshness/source/unit，并把抓取时刻当更新时间；
- 新增字段必须在 response_model 中声明，否则会被静默剥离。

全部用例离线、确定性，不访问网络。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.services.stock_service import StockService


def _payload(quote, code: str = "588000"):
    provider = SimpleNamespace(get_realtime_quote=lambda *args: quote)
    with patch("data_provider.base.DataFetcherManager", return_value=provider):
        return StockService().get_realtime_quote(code)


class QuoteEvidencePayloadTestCase(unittest.TestCase):
    """service 层必须透传来源与时间证据。"""

    def test_stale_quote_keeps_source_time_and_freshness(self) -> None:
        quote = UnifiedRealtimeQuote(
            code="588000",
            name="沪深300ETF",
            source=RealtimeSource.TENCENT,
            price=4.11,
            volume=49549600,
            volume_unit="shares",
            quote_time="2024-01-01T15:00:00+08:00",
        )

        payload = _payload(quote)

        self.assertEqual(payload["quote_time"], "2024-01-01T15:00:00+08:00")
        self.assertEqual(payload["freshness"], "stale")
        self.assertFalse(payload["is_realtime"])
        self.assertEqual(payload["source"], "tencent")
        self.assertEqual(payload["volume_unit"], "shares")
        self.assertIsNotNone(payload["age_seconds"])
        self.assertTrue(payload["field_sources"])
        self.assertEqual(payload["session_date"], "2024-01-01")

    def test_update_time_is_fetch_time_with_beijing_offset(self) -> None:
        quote = UnifiedRealtimeQuote(
            code="588000", price=4.11, quote_time="2024-01-01T15:00:00+08:00"
        )

        payload = _payload(quote)

        # 兼容字段语义为抓取时间，且必须与 fetched_at 一致、带 +08:00 偏移
        self.assertEqual(payload["update_time"], payload["fetched_at"])
        self.assertIn("+08:00", payload["update_time"])
        self.assertIn("+08:00", payload["served_at"])
        # 抓取时间不能等于（被误当成）两年前的行情时间
        self.assertNotEqual(payload["update_time"], payload["quote_time"])

    def test_unknown_source_time_is_not_labelled_realtime(self) -> None:
        quote = UnifiedRealtimeQuote(code="588000", price=4.11)

        payload = _payload(quote)

        self.assertIsNone(payload["quote_time"])
        self.assertIsNone(payload["session_date"])
        self.assertEqual(payload["freshness"], "unknown")
        self.assertFalse(payload["is_realtime"])
        self.assertIsNone(payload["age_seconds"])

    def test_us_session_date_uses_exchange_timezone(self) -> None:
        # 北京时间 2026-09-14 05:00 == 纽约时间 2026-09-13 17:00
        quote = UnifiedRealtimeQuote(
            code="AAPL", price=230.0, quote_time="2026-09-14T05:00:00+08:00"
        )

        payload = _payload(quote, code="AAPL")

        self.assertEqual(payload["session_date"], "2026-09-13")

    def test_secondary_source_field_keeps_its_own_time(self) -> None:
        quote = UnifiedRealtimeQuote(
            code="600519",
            price=1800.0,
            pe_ratio=31.5,
            quote_time="2026-09-14T09:35:00+08:00",
            field_sources={
                "pe_ratio": {
                    "source": "eastmoney",
                    "quote_time": "2026-09-13T15:00:00+08:00",
                    "fetched_at": "2026-09-14T09:53:34+08:00",
                }
            },
        )

        payload = _payload(quote, code="600519")
        fields = payload["field_sources"]

        # 第二来源补充的 PE 不得继承第一来源的价格时间
        self.assertEqual(fields["pe_ratio"]["quote_time"], "2026-09-13T15:00:00+08:00")
        self.assertEqual(fields["price"]["quote_time"], "2026-09-14T09:35:00+08:00")
        self.assertNotEqual(
            fields["pe_ratio"]["quote_time"], fields["price"]["quote_time"]
        )

    def test_placeholder_quote_is_not_realtime(self) -> None:
        payload = StockService()._get_placeholder_quote("600519")

        self.assertEqual(payload["freshness"], "unknown")
        self.assertFalse(payload["is_realtime"])
        self.assertIsNone(payload["quote_time"])
        self.assertIn("+08:00", payload["update_time"])


class StockQuoteSchemaTestCase(unittest.TestCase):
    """response_model 必须声明证据字段，否则 FastAPI 会静默剥离。"""

    EVIDENCE_KEYS = (
        "quote_time", "fetched_at", "served_at", "session_date", "source",
        "freshness", "age_seconds", "volume_unit", "field_sources",
        "is_realtime", "freshness_note",
    )

    def test_schema_declares_and_preserves_evidence_fields(self) -> None:
        from api.v1.schemas.stocks import StockQuote

        payload = _payload(
            UnifiedRealtimeQuote(
                code="588000",
                price=4.11,
                source=RealtimeSource.TENCENT,
                volume_unit="shares",
                quote_time="2024-01-01T15:00:00+08:00",
            )
        )
        dumped = StockQuote.model_validate(payload).model_dump()

        for key in self.EVIDENCE_KEYS:
            self.assertIn(key, StockQuote.model_fields, msg=f"{key} 未在 schema 声明")
            self.assertIsNotNone(dumped[key], msg=f"{key} 被 response_model 剥离")

        self.assertEqual(dumped["freshness"], "stale")
        self.assertFalse(dumped["is_realtime"])
        self.assertEqual(dumped["quote_time"], "2024-01-01T15:00:00+08:00")

    def test_schema_is_backward_compatible_for_legacy_payload(self) -> None:
        from api.v1.schemas.stocks import StockQuote

        legacy = {
            "stock_code": "600519",
            "current_price": 1800.0,
            "update_time": "2024-01-01T15:00:00",
        }

        dumped = StockQuote.model_validate(legacy).model_dump()

        self.assertEqual(dumped["stock_code"], "600519")
        self.assertEqual(dumped["update_time"], "2024-01-01T15:00:00")
        self.assertIsNone(dumped["quote_time"])
        self.assertIsNone(dumped["freshness"])


if __name__ == "__main__":
    unittest.main()
