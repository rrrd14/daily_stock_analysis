# -*- coding: utf-8 -*-
"""回测引擎注册表（WP4 第二阶段）。

把「engine_kind → 计算函数」的映射集中在这里，避免在 ``run_backtest`` 里硬编码
「换个标签跑旧引擎」。每个策略引擎负责：从**冻结快照**（而非 DB/抓取路径）读取
bars、校验标的与区间口径、产出自己的 ``evidence.items`` / ``metrics``，并把
``snapshot.consumed`` 置为 True。

内置的报告事后评估（``ai_report_evaluation``）不在此注册表内：它走
``BacktestService._run_backtest`` 的旧路径，快照只是**引用**（consumed=false）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# kind -> handler(service, code, snapshot, evidence, params) -> stats dict
_ENGINES: Dict[str, Callable[..., Dict[str, Any]]] = {}


def register_engine(kind: str) -> Callable:
    def decorator(handler: Callable) -> Callable:
        _ENGINES[kind] = handler
        return handler

    return decorator


def get_engine(kind: str) -> Optional[Callable]:
    return _ENGINES.get(kind)


def implemented_kinds() -> List[str]:
    return sorted(_ENGINES)


def _daily_returns_from_bars(bars: List[Dict[str, Any]]):
    """从冻结 bars 的收盘价计算日频简单收益率序列（close-to-close）。

    只读、确定性：不写库、不抓取；按日期排序后逐对计算，前收盘为 0 时记为
    ``simple_return=null``（不伪造除零结果）。返回 ``(observations, closes)``，
    其中 ``closes`` 是 ``(date, close)`` 的升序列表，供首尾收盘统计。
    """
    closes = []
    for bar in bars:
        close = bar.get("close") if isinstance(bar, dict) else None
        if close is None:
            continue
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        closes.append((str(bar.get("date", ""))[:10], value))
    closes.sort(key=lambda pair: pair[0])

    observations = []
    for index in range(1, len(closes)):
        previous_close = closes[index - 1][1]
        current_date, current_close = closes[index]
        if previous_close == 0:
            simple_return = None
        else:
            simple_return = round((current_close / previous_close) - 1.0, 12)
        observations.append({
            "date": current_date,
            "close": current_close,
            "simple_return": simple_return,
        })
    return observations, closes


def _bars_within_requested_range(
    bars: List[Dict[str, Any]],
    requested_start: Optional[str],
    requested_end: Optional[str],
):
    """把收益计算绑定到请求区间（修复 P1：消费区间必须与验证区间一致）。

    快照 payload 可能保留请求区间外的行（作预热/上下文），但 ``daily_return`` 的
    总回报率必须只由「声明区间内」的收盘价计算，避免区间外低价收盘污染结果。
    区间外行不计入观测，只作透明计数；请求区间未声明时不裁剪。
    """
    in_range: List[Dict[str, Any]] = []
    out_of_range = 0
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        day = str(bar.get("date", ""))[:10]
        if not day:
            out_of_range += 1
            continue
        if requested_start and day < requested_start:
            out_of_range += 1
            continue
        if requested_end and day > requested_end:
            out_of_range += 1
            continue
        in_range.append(bar)
    return in_range, out_of_range


@register_engine("daily_return")
def run_daily_return(service, *, code, snapshot, evidence, params) -> Dict[str, Any]:
    """日频回报率引擎：从冻结快照计算日频简单收益率序列与总回报率。

    诚实边界：只消费冻结 bars 的收盘价，不建模资金 / 费用 / 滑点 / 持仓——这是
    「算回报率」的引擎，不是组合 / 账户回测，也不产出可交易净值曲线。
    执行时按**当前** QC 规则复检「实际消费」的区间内数据，不信任旧快照存下来的
    资格布尔值（QC 升级后旧快照可能仍带 eligible=true）。
    """
    from src.repositories.market_snapshot_repo import (
        SNAPSHOT_QC_VERSION,
        MarketDataSnapshotRepository,
        validate_bars,
    )

    if code is not None and str(snapshot["instrument"]) != str(code):
        raise ValueError(
            f"snapshot instrument {snapshot['instrument']!r} does not match "
            f"requested code {code!r}"
        )

    detail = MarketDataSnapshotRepository(service.db).get(
        snapshot["snapshot_id"], detail=True
    )
    bars = detail["bars"] if detail else []
    # 收益必须只由「声明区间」内的收盘价计算：快照 payload 可能保留区间外行作
    # 预热/上下文，区间外行不计入观测，只透明计数（修复 P1：消费区间绑定）。
    bars, bars_out_of_range = _bars_within_requested_range(
        bars,
        requested_start=snapshot.get("requested_start"),
        requested_end=snapshot.get("requested_end"),
    )
    # 复检：按当前 QC 规则重新校验「实际消费」的区间内数据，不信任旧快照在旧版
    # 规则下写入的资格布尔值（QC 升级后旧快照仍可能带 eligible=true）。
    revalidation_problems = validate_bars(bars)["problems"]

    observations, closes = _daily_returns_from_bars(bars)

    first_close = closes[0][1] if closes else None
    last_close = closes[-1][1] if closes else None
    total_return = None
    if first_close is not None and first_close != 0:
        total_return = round((last_close / first_close) - 1.0, 12)

    annualized_return = None
    if total_return is not None and total_return > -1.0 and len(observations) > 0:
        # 几何年化：按 252 个交易日/年，以收盘到收盘观测数折算。
        annualized_return = round(
            ((1.0 + total_return) ** (252.0 / len(observations))) - 1.0, 12,
        )

    evidence["schema_version"] = 2
    evidence["kind"] = "daily_return"
    evidence["snapshot"]["consumed"] = True
    evidence["snapshot"]["bars_consumed"] = len(bars)
    evidence["snapshot"]["bars_out_of_range"] = bars_out_of_range
    evidence["snapshot"]["revalidated"] = True
    evidence["snapshot"]["revalidation_qc_version"] = SNAPSHOT_QC_VERSION
    evidence["snapshot"]["revalidation_problems"] = revalidation_problems
    evidence["items"] = observations
    evidence["metrics"] = {
        "bars": len(bars),
        "bars_out_of_range": bars_out_of_range,
        "observations": len(observations),
        "first_close": first_close,
        "last_close": last_close,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "periods_per_year": 252.0,
        "revalidation_problems": revalidation_problems,
    }
    evidence["limitations"] = [
        "Daily return engine consumes the frozen snapshot, but does not model "
        "capital, fees, slippage or positions; returns are simple close-to-close "
        "observations, not a portfolio/account backtest.",
        "annualized_return is geometric, assumes 252 trading days/year and one "
        "observation per trading day; it does not adjust for gaps in the series.",
    ]
    if revalidation_problems:
        # 复检发现当前规则下的问题（如旧快照含零价格）：不宣称 completed，证据已如实记录。
        return {
            "processed": len(bars),
            "saved": 0,
            "completed": 0,
            "insufficient": 1,
            "errors": 0,
        }
    processed = len(observations)
    return {
        "processed": processed,
        "saved": processed,
        "completed": processed,
        "insufficient": 0,
        "errors": 0,
    }
