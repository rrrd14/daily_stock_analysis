# -*- coding: utf-8 -*-
"""
===================================
股票数据服务层
===================================

职责：
1. 封装股票数据获取逻辑
2. 提供实时行情和历史数据接口
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any

from src.repositories.stock_repo import StockRepository
from src.time_utils import beijing_now

logger = logging.getLogger(__name__)


def _session_date_for(stock_code: str, quote_time: Optional[str]) -> Optional[str]:
    """行情所属交易所的交易日（按市场时区），而不是北京时间日期。

    美股收盘时刻在北京时间次日，交易所交易日仍按 America/New_York 计算。
    无法解析或不认识的市场返回 None，不做猜测。
    """
    if not quote_time:
        return None
    try:
        from zoneinfo import ZoneInfo

        from src.core.trading_calendar import MARKET_TIMEZONE, get_market_for_stock

        moment = datetime.fromisoformat(quote_time)
        tz_name = MARKET_TIMEZONE.get(get_market_for_stock(stock_code) or "")
        if tz_name:
            moment = moment.astimezone(ZoneInfo(tz_name))
        return moment.date().isoformat()
    except (ValueError, TypeError, KeyError, OSError):
        return None


class StockService:
    """
    股票数据服务
    
    封装股票数据获取的业务逻辑
    """
    
    def __init__(self):
        """初始化股票数据服务"""
        self.repo = StockRepository()
    
    def get_realtime_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取股票实时行情
        
        Args:
            stock_code: 股票代码
            
        Returns:
            实时行情数据字典
        """
        try:
            # 调用数据获取器获取实时行情
            from data_provider.base import DataFetcherManager
            
            manager = DataFetcherManager()
            quote = manager.get_realtime_quote(stock_code)
            
            if quote is None:
                logger.warning(f"获取 {stock_code} 实时行情失败")
                return None
            
            # UnifiedRealtimeQuote 是 dataclass，使用 getattr 安全访问字段
            # 字段映射: UnifiedRealtimeQuote -> API 响应
            # - code -> stock_code
            # - name -> stock_name
            # - price -> current_price
            # - change_amount -> change
            # - change_pct -> change_percent
            # - open_price -> open
            # - high -> high
            # - low -> low
            # - pre_close -> prev_close
            # - volume -> volume
            # - amount -> amount
            #
            # 证据字段必须与本响应一同返回：行情时间(quote_time) 表示来源提供的
            # 行情发生时刻，抓取时间(fetched_at) 表示本次收到数据的时刻，
            # 返回时刻(served_at) 只表示服务时间。时效未知/过期时不得表述为当前实时。
            provenance = quote.provenance() if hasattr(quote, "provenance") else {}
            fetched_at = provenance.get("fetched_at") or beijing_now().isoformat(timespec="seconds")
            quote_time = provenance.get("quote_time")
            source = getattr(quote, "source", None)
            source_name = (
                source.value if hasattr(source, "value")
                else (str(source) if source is not None else None)
            )
            freshness = provenance.get("freshness") or "unknown"

            return {
                "stock_code": getattr(quote, "code", stock_code),
                "stock_name": getattr(quote, "name", None),
                "current_price": getattr(quote, "price", 0.0) or 0.0,
                "change": getattr(quote, "change_amount", None),
                "change_percent": getattr(quote, "change_pct", None),
                "open": getattr(quote, "open_price", None),
                "high": getattr(quote, "high", None),
                "low": getattr(quote, "low", None),
                "prev_close": getattr(quote, "pre_close", None),
                "volume": getattr(quote, "volume", None),
                "amount": getattr(quote, "amount", None),
                # 兼容字段：语义明确为本次抓取时间（北京时间，含 +08:00）
                "update_time": fetched_at,
                # === 追加证据字段 ===
                "quote_time": quote_time,
                "fetched_at": fetched_at,
                "served_at": beijing_now().isoformat(timespec="seconds"),
                "session_date": _session_date_for(stock_code, quote_time),
                "source": source_name,
                "freshness": freshness,
                "age_seconds": provenance.get("age_seconds"),
                "volume_unit": provenance.get("volume_unit") or getattr(quote, "volume_unit", None),
                "field_sources": provenance.get("field_sources"),
                "is_realtime": freshness == "recent",
                "freshness_note": provenance.get("freshness_note"),
            }
            
        except ImportError:
            logger.warning("DataFetcherManager 未找到，使用占位数据")
            return self._get_placeholder_quote(stock_code)
        except Exception as e:
            logger.error(f"获取实时行情失败: {e}", exc_info=True)
            return None
    
    def get_history_data(
        self,
        stock_code: str,
        period: str = "daily",
        days: int = 30
    ) -> Dict[str, Any]:
        """
        获取股票历史行情
        
        Args:
            stock_code: 股票代码
            period: K 线周期 (daily/weekly/monthly)
            days: 获取天数
            
        Returns:
            历史行情数据字典
            
        Raises:
            ValueError: 当 period 不是 daily 时抛出（weekly/monthly 暂未实现）
        """
        # 验证 period 参数，只支持 daily
        if period != "daily":
            raise ValueError(
                f"暂不支持 '{period}' 周期，目前仅支持 'daily'。"
                "weekly/monthly 聚合功能将在后续版本实现。"
            )
        
        try:
            # 调用数据获取器获取历史数据
            from data_provider.base import DataFetcherManager
            
            manager = DataFetcherManager()
            df, source = manager.get_daily_data(stock_code, days=days)
            
            if df is None or df.empty:
                logger.warning(f"获取 {stock_code} 历史数据失败")
                return {"stock_code": stock_code, "period": period, "data": []}
            
            # 获取股票名称
            stock_name = manager.get_stock_name(stock_code)
            
            # 转换为响应格式
            data = []
            for _, row in df.iterrows():
                date_val = row.get("date")
                if hasattr(date_val, "strftime"):
                    date_str = date_val.strftime("%Y-%m-%d")
                else:
                    date_str = str(date_val)
                
                data.append({
                    "date": date_str,
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": float(row.get("volume", 0)) if row.get("volume") else None,
                    "amount": float(row.get("amount", 0)) if row.get("amount") else None,
                    "change_percent": float(row.get("pct_chg", 0)) if row.get("pct_chg") else None,
                })
            
            return {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "period": period,
                "data": data,
            }
            
        except ImportError:
            logger.warning("DataFetcherManager 未找到，返回空数据")
            return {"stock_code": stock_code, "period": period, "data": []}
        except Exception as e:
            logger.error(f"获取历史数据失败: {e}", exc_info=True)
            return {"stock_code": stock_code, "period": period, "data": []}
    
    def _get_placeholder_quote(self, stock_code: str) -> Dict[str, Any]:
        """
        获取占位行情数据（用于测试）
        
        Args:
            stock_code: 股票代码
            
        Returns:
            占位行情数据
        """
        now_iso = beijing_now().isoformat(timespec="seconds")
        return {
            "stock_code": stock_code,
            "stock_name": f"股票{stock_code}",
            "current_price": 0.0,
            "change": None,
            "change_percent": None,
            "open": None,
            "high": None,
            "low": None,
            "prev_close": None,
            "volume": None,
            "amount": None,
            "update_time": now_iso,
            "quote_time": None,
            "fetched_at": now_iso,
            "served_at": now_iso,
            "session_date": None,
            "source": "placeholder",
            "freshness": "unknown",
            "age_seconds": None,
            "volume_unit": "unknown",
            "field_sources": {},
            "is_realtime": False,
            "freshness_note": "占位数据：未取得真实行情，不能作为实时行情使用。",
        }
