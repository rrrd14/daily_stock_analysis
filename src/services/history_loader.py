"""DB-first K-line history loader for Agent tools.

Provides:
- ContextVar-based frozen target_date propagation across threads
- ``load_history_df``: read from DB first, DataFetcherManager fallback

Fixes #1066 – eliminates 45+ redundant HTTP requests per stock in Agent mode.
"""
from __future__ import annotations

from src.time_utils import beijing_today
import contextvars
import logging
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)
_CACHE_MIN_RECORDS = 30

# ---------------------------------------------------------------------------
# Frozen target date (ContextVar) – set once per stock in pipeline, read by
# all agent tool threads via copy_context().run().
# ---------------------------------------------------------------------------
_frozen_target_date: contextvars.ContextVar[Optional[date]] = contextvars.ContextVar(
    "_frozen_target_date", default=None,
)


def set_frozen_target_date(d: date) -> contextvars.Token:
    return _frozen_target_date.set(d)


def get_frozen_target_date() -> Optional[date]:
    return _frozen_target_date.get()


def reset_frozen_target_date(token: contextvars.Token) -> None:
    _frozen_target_date.reset(token)


# ---------------------------------------------------------------------------
# Internal DataFetcherManager singleton (fallback only)
# ---------------------------------------------------------------------------
_fetcher_singleton = None
_fetcher_lock = Lock()


def _get_fetcher_manager():
    global _fetcher_singleton
    if _fetcher_singleton is None:
        with _fetcher_lock:
            if _fetcher_singleton is None:
                from data_provider import DataFetcherManager
                _fetcher_singleton = DataFetcherManager()
    return _fetcher_singleton


# ---------------------------------------------------------------------------
# DB-first history loader
# ---------------------------------------------------------------------------
def _history_code_candidates(stock_code: str) -> Tuple[List[str], str]:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    raw_code = str(stock_code or "").strip()
    normalized_code = canonical_stock_code(normalize_stock_code(raw_code))
    candidates: List[str] = []
    for candidate in (canonical_stock_code(raw_code), normalized_code):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates, normalized_code


def _coerce_bar_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return date.min
    if hasattr(value, "date"):
        try:
            coerced = value.date()
            return coerced if isinstance(coerced, date) else date.min
        except Exception:
            return date.min
    return date.min


def _bar_date(bar: Any) -> date:
    row_date = _coerce_bar_date(getattr(bar, "date", None))
    if row_date != date.min:
        return row_date
    if hasattr(bar, "to_dict"):
        try:
            return _coerce_bar_date((bar.to_dict() or {}).get("date"))
        except Exception:
            return date.min
    return date.min


def _select_best_bars(db, stock_code: str, start: date, end: date) -> Tuple[Optional[str], list]:
    candidates, normalized_code = _history_code_candidates(stock_code)
    best_code = None
    best_bars = []
    best_key = None

    for candidate in candidates:
        bars = list(db.get_data_range(candidate, start, end) or [])
        if not bars:
            continue
        latest_date = max(_bar_date(bar) for bar in bars)
        key = (latest_date, len(bars), candidate == normalized_code)
        if best_key is None or key > best_key:
            best_key = key
            best_code = candidate
            best_bars = bars

    return best_code, best_bars


def load_history_df(
    stock_code: str,
    days: int = 60,
    target_date: Optional[date] = None,
    source: Optional[str] = None,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Load K-line history, DB first with DataFetcherManager fallback.

    Returns ``(df, source)`` where *source* is ``"db_cache"`` on DB hit or the
    actual provider name on network fallback.  Returns ``(None, "none")`` when
    both paths fail.
    """
    from src.storage import get_db

    # Resolve effective end date
    if target_date is not None:
        end = target_date
    else:
        frozen = get_frozen_target_date()
        end = frozen if frozen else beijing_today()

    # Calendar-day buffer: ~1.8x trading days + margin for long holidays
    start = end - timedelta(days=int(days * 1.8) + 10)

    # --- 1. DB lookup (canonical code, then prefix-stripped fallback) ------
    try:
        db = get_db() if not source else None
        _code, bars = _select_best_bars(db, stock_code, start, end) if not source else (None, [])
        # Long-window requests must not silently reuse a short indicator cache.
        required_records = days if days > 365 else max(min(days, _CACHE_MIN_RECORDS), 1)
        latest_date = max((_bar_date(bar) for bar in bars), default=date.min)
        if bars and latest_date >= end and len(bars) >= required_records:
            df = pd.DataFrame([b.to_dict() for b in bars])
            from data_provider.base import BaseFetcher
            df = df.sort_values('date').drop_duplicates('date', keep='last')
            df = BaseFetcher._calculate_indicators(df)
            if 'data_source' in df and 'amount' in df:
                df.loc[df['data_source'].eq('YfinanceFetcher'), 'amount'] = float('nan')
            logger.debug(
                "load_history_df(%s): %d bars from DB (requested %d)",
                stock_code, len(df), days,
            )
            return df, "db_cache"
    except Exception as e:
        logger.debug("load_history_df(%s): DB read failed: %s", stock_code, e)

    # --- 2. Network fallback via singleton DataFetcherManager -------------
    # 所有路径（短/长窗口、自动/指定来源）共用同一 resolved 日期边界：
    #   - 目标日之后的数据一律不得返回（由 manager 入参 + 返回后裁剪双重保证）
    #   - 显式 source 只改变来源选择，不改变截止日期语义
    # min_records 仅在长窗口或显式来源时要求，避免短窗口把「返回不足」误判为失败。
    diagnostics = []
    try:
        manager = _get_fetcher_manager()
        df, source = manager.get_daily_data(
            stock_code,
            days=days,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            source=source,
            min_records=days if (days > 365 or source) else None,
            diagnostics=diagnostics,
        )
        if df is not None and not df.empty and "date" in df.columns:
            dates = pd.to_datetime(df["date"], errors="coerce")
            # 强制裁剪到 resolved 区间：不返回目标日之后的行情。
            # 仅用解析结果做过滤，不回写 date 列，避免改变调用方依赖的原始类型
            # （例如 datetime.date 会被 pd.to_datetime 变成 Timestamp）。
            in_range = (
                dates.notna()
                & (dates >= pd.Timestamp(start))
                & (dates <= pd.Timestamp(end))
            )
            df = df.loc[in_range]
            df = df.sort_values("date").drop_duplicates("date", keep="last")
        if df is not None and not df.empty:
            df.attrs["source_attempts"] = diagnostics
            return df, source
        # 取到数据但全部落在目标区间之外时，不返回越界行情
        if df is not None:
            empty = pd.DataFrame()
            empty.attrs["source_attempts"] = diagnostics
            return empty, "none"
    except Exception as e:
        logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)

    if diagnostics or source:
        empty = pd.DataFrame()
        empty.attrs["source_attempts"] = diagnostics
        return empty, "none"
    return None, "none"
