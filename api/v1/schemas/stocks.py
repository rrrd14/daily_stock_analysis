# -*- coding: utf-8 -*-
"""
===================================
股票数据相关模型
===================================

职责：
1. 定义股票实时行情模型
2. 定义历史 K 线数据模型
"""

from typing import Any, Dict, Optional, List

from pydantic import BaseModel, Field


class StockQuote(BaseModel):
    """股票实时行情"""

    stock_code: str = Field(..., description="股票代码")
    stock_name: Optional[str] = Field(None, description="股票名称")
    current_price: float = Field(..., description="当前价格")
    change: Optional[float] = Field(None, description="涨跌额")
    change_percent: Optional[float] = Field(None, description="涨跌幅 (%)")
    open: Optional[float] = Field(None, description="开盘价")
    high: Optional[float] = Field(None, description="最高价")
    low: Optional[float] = Field(None, description="最低价")
    prev_close: Optional[float] = Field(None, description="昨收价")
    volume: Optional[float] = Field(None, description="成交量（单位见 volume_unit）")
    amount: Optional[float] = Field(None, description="成交额（元）")
    # 兼容字段：明确语义为「本次抓取时间」，不再代表行情发生时间。
    update_time: Optional[str] = Field(
        None, description="本次抓取时间（北京时间，含 +08:00 偏移）；兼容字段，等价于 fetched_at"
    )

    # === 行情证据（追加字段，旧客户端可忽略；未提供时为 null）===
    quote_time: Optional[str] = Field(
        None, description="行情发生/发布时刻（来源提供，北京时间 +08:00）；来源未提供时为 null"
    )
    fetched_at: Optional[str] = Field(
        None, description="本次成功收到该行情的时刻（北京时间 +08:00）"
    )
    served_at: Optional[str] = Field(
        None, description="本次 API 返回时刻（北京时间 +08:00）；仅表示服务时间，不能用于判断行情新鲜度"
    )
    session_date: Optional[str] = Field(
        None, description="所属交易所的交易日（按市场时区；美股可能为北京时间前一日）"
    )
    source: Optional[str] = Field(None, description="行情数据来源标识")
    freshness: Optional[str] = Field(
        None, description="时效：recent / stale / unknown / future_timestamp"
    )
    age_seconds: Optional[float] = Field(
        None, description="行情时间距抓取时刻的秒数；来源未提供行情时间时为 null"
    )
    volume_unit: Optional[str] = Field(
        None, description="成交量单位：shares / lots / unknown（unknown 时不应据此比较绝对量）"
    )
    field_sources: Optional[Dict[str, Any]] = Field(
        None, description="逐字段来源与时间，用于识别第二来源补充字段（如 PE 与价格不同时间）"
    )
    is_realtime: Optional[bool] = Field(
        None, description="是否可视为当前实时行情（freshness == 'recent'）"
    )
    freshness_note: Optional[str] = Field(None, description="时效语义说明")

    class Config:
        json_schema_extra = {
            "example": {
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "current_price": 1800.00,
                "change": 15.00,
                "change_percent": 0.84,
                "open": 1785.00,
                "high": 1810.00,
                "low": 1780.00,
                "prev_close": 1785.00,
                "volume": 10000000,
                "amount": 18000000000,
                "update_time": "2026-09-14T09:53:34+08:00",
                "quote_time": "2026-09-14T09:35:00+08:00",
                "fetched_at": "2026-09-14T09:53:34+08:00",
                "served_at": "2026-09-14T09:53:35+08:00",
                "session_date": "2026-09-14",
                "source": "tencent",
                "freshness": "recent",
                "age_seconds": 1114.0,
                "volume_unit": "shares",
                "field_sources": {
                    "price": {
                        "source": "tencent",
                        "quote_time": "2026-09-14T09:35:00+08:00",
                        "fetched_at": "2026-09-14T09:53:34+08:00",
                    }
                },
                "is_realtime": True,
                "freshness_note": "recent仅表示源时间戳在5分钟内；抓取时间不等于行情时间。",
            }
        }


class KLineData(BaseModel):
    """K 线数据点"""
    
    date: str = Field(..., description="日期")
    open: float = Field(..., description="开盘价")
    high: float = Field(..., description="最高价")
    low: float = Field(..., description="最低价")
    close: float = Field(..., description="收盘价")
    volume: Optional[float] = Field(None, description="成交量")
    amount: Optional[float] = Field(None, description="成交额")
    change_percent: Optional[float] = Field(None, description="涨跌幅 (%)")
    
    class Config:
        json_schema_extra = {
            "example": {
                "date": "2024-01-01",
                "open": 1785.00,
                "high": 1810.00,
                "low": 1780.00,
                "close": 1800.00,
                "volume": 10000000,
                "amount": 18000000000,
                "change_percent": 0.84
            }
        }


class ExtractItem(BaseModel):
    """单条提取结果（代码、名称、置信度）"""

    code: Optional[str] = Field(None, description="股票代码，None 表示解析失败")
    name: Optional[str] = Field(None, description="股票名称（如有）")
    confidence: str = Field("medium", description="置信度：high/medium/low")


class ExtractFromImageResponse(BaseModel):
    """图片股票代码提取响应"""

    codes: List[str] = Field(..., description="提取的股票代码（已去重，向后兼容）")
    items: List[ExtractItem] = Field(default_factory=list, description="提取结果明细（代码+名称+置信度）")
    raw_text: Optional[str] = Field(None, description="原始 LLM 响应（调试用）")


class StockHistoryResponse(BaseModel):
    """股票历史行情响应"""
    
    stock_code: str = Field(..., description="股票代码")
    stock_name: Optional[str] = Field(None, description="股票名称")
    period: str = Field(..., description="K 线周期")
    data: List[KLineData] = Field(default_factory=list, description="K 线数据列表")
    
    class Config:
        json_schema_extra = {
            "example": {
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "period": "daily",
                "data": []
            }
        }
