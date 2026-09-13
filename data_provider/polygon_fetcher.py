# -*- coding: utf-8 -*-
"""
===================================
PolygonFetcher - Polygon.io 美股数据源 (Priority 6)
===================================

数据来源：Polygon.io (https://polygon.io)
特点：覆盖美股日线历史数据，免费版每天 5 次 API 调用
定位：美股第三兜底数据源（排在 YFinance > Longbridge 之后）

凭证：`POLYGON_API_KEY` 环境变量
免费注册：https://polygon.io/dashboard/signup
"""

import logging
import os
import time
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

from .base import BaseFetcher, STANDARD_COLUMNS, DataFetchError
from .us_index_mapping import is_us_stock_code

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
RETRY_DELAY = 1.0  # seconds


def _to_polygon_symbol(stock_code: str) -> Optional[str]:
    """
    将内部股票代码转换为 Polygon 格式（纯大写，无后缀）
    例如：AAPL.US → AAPL,  AAPL → AAPL
    """
    code = stock_code.strip().upper()
    if code.endswith(".US"):
        return code[:-3]
    if is_us_stock_code(code):
        return code
    return None


class PolygonFetcher(BaseFetcher):
    """
    Polygon.io 数据源实现

    优先级: 6（在 YFinance(4) 和 Longbridge(5) 之后）
    数据来源: Polygon.io REST API

    免费版限制：每分钟 5 次请求，仅支持美股日线（延迟 15 分钟）
    """

    name = "PolygonFetcher"
    priority = int(os.getenv("POLYGON_PRIORITY", "6"))

    def __init__(self):
        self._api_key = os.getenv("POLYGON_API_KEY", "").strip()
        self._available = None

    def _is_available(self) -> bool:
        """检查是否配置了 API Key"""
        if self._available is not None:
            return self._available
        self._available = bool(self._api_key)
        if not self._available:
            logger.debug("[Polygon] 未配置 POLYGON_API_KEY，跳过")
        return self._available

    def _fetch_raw_data(
        self, stock_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """
        从 Polygon.io 获取历史日线数据

        API: GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}
        """
        symbol = _to_polygon_symbol(stock_code)
        if symbol is None:
            raise ValueError(f"无法将 {stock_code} 转换为 Polygon 符号")

        if not self._is_available():
            raise RuntimeError("Polygon API key 未配置")

        # 转换日期格式为 Polygon 需要的 yyyy-MM-dd
        start = datetime.strptime(start_date, "%Y-%m-%d").strftime("%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d").strftime("%Y-%m-%d")

        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}"
            f"/range/1/day/{start}/{end}"
        )
        params = {
            "adjusted": "true",
            "sort": "asc",
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        logger.debug(f"[Polygon] 请求: {url}?{params}")

        last_exception = None
        for attempt in range(1, MAX_RETRIES + 2):  # 1 initial + retries
            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=15,
                )
                # Handle rate limiting
                if resp.status_code == 429:
                    logger.warning(
                        "[Polygon] 触发速率限制，等待后重试 (%d/%d)",
                        attempt,
                        MAX_RETRIES + 1,
                    )
                    time.sleep(RETRY_DELAY * attempt)
                    continue

                resp.raise_for_status()
                data = resp.json()

                # Delayed subscriptions still return valid historical aggregates.
                if data.get("status") not in {"OK", "DELAYED"}:
                    raise DataFetchError(
                        f"Polygon API 返回异常状态: {data.get('status')} "
                        f"({data.get('error', '')})"
                    )

                results = data.get("results")
                if not results:
                    logger.info("[Polygon] %s 返回空数据", symbol)
                    return pd.DataFrame()

                # 解析结果
                rows = []
                for bar in results:
                    ts = bar.get("t")  # Unix 毫秒时间戳
                    if ts is None:
                        continue
                    dt = datetime.fromtimestamp(ts / 1000.0).strftime("%Y-%m-%d")
                    rows.append({
                        "date": dt,
                        "open": bar.get("o"),
                        "high": bar.get("h"),
                        "low": bar.get("l"),
                        "close": bar.get("c"),
                        "volume": bar.get("v"),
                        "amount": None,  # Polygon 不提供成交额
                    })

                logger.info("[Polygon] %s 获取 %d 条日线数据", symbol, len(rows))
                return pd.DataFrame(rows)

            except requests.exceptions.RequestException as e:
                last_exception = e
                logger.debug(f"[Polygon] 请求失败 (尝试 {attempt}): {e}")
                if attempt <= MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                else:
                    raise DataFetchError(
                        f"Polygon 获取 {symbol} 数据失败: {e}"
                    ) from e

        if last_exception:
            raise DataFetchError(
                f"Polygon 获取 {symbol} 数据失败: {last_exception}"
            ) from last_exception

        return pd.DataFrame()

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        保留 date 列供 BaseFetcher 清洗和排序，返回标准列。
        """
        if df.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        df = df.copy()
        # BaseFetcher expects date as a column, not a same-named index.
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.sort_values("date").reset_index(drop=True)

        if "close" in df.columns and "pct_chg" not in df.columns:
            df["pct_chg"] = df["close"].pct_change() * 100

        # 确保列名统一（原始列已经是小写，但为了健壮性显式重命名）
        df = df.rename(columns={
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        })

        # 补充缺失的标准列
        for col in STANDARD_COLUMNS:
            if col not in df.columns:
                df[col] = None

        return df[STANDARD_COLUMNS]
