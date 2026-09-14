# -*- coding: utf-8 -*-
"""Backtest API schemas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class BacktestRunRequest(BaseModel):
    code: Optional[str] = Field(None, description="仅回测指定股票")
    force: bool = Field(False, description="强制重新计算")
    eval_window_days: Optional[int] = Field(None, ge=1, le=120, description="评估窗口（交易日数）")
    min_age_days: Optional[int] = Field(None, ge=0, le=365, description="分析记录最小天龄（0=不限）")
    limit: int = Field(200, ge=1, le=2000, description="最多处理的分析记录数")


class BacktestRunResponse(BaseModel):
    run_id: Optional[str] = None
    status: Optional[str] = None
    processed: int = Field(..., description="候选记录数")
    saved: int = Field(..., description="写入回测结果数")
    completed: int = Field(..., description="完成回测数")
    insufficient: int = Field(..., description="数据不足数")
    errors: int = Field(..., description="错误数")


class BacktestRunRecord(BaseModel):
    run_id: str
    status: str
    created_at: str
    finished_at: Optional[str] = None
    sha256: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None

    # 冻结快照引用（WP4）：报告评估可能为空；策略引擎必须指向合格快照
    snapshot_id: Optional[str] = Field(None, description="引用的冻结行情快照 ID")
    data_quality_status: Optional[str] = Field(
        None, description="快照质量等级：verified / partial / unknown"
    )
    input_eligibility: Optional[bool] = Field(
        None, description="该快照是否满足默认策略收益计算的数据资格"
    )
    engine_kind: Optional[str] = Field(
        None, description="引擎类型：ai_report_evaluation / portfolio_daily 等"
    )
    engine_version: Optional[str] = Field(None, description="引擎版本")


class MarketSnapshotRecord(BaseModel):
    """冻结行情快照的只读视图（`bars` 仅在显式请求时返回）。"""

    snapshot_id: str
    schema_version: Optional[str] = None
    instrument: str
    market: Optional[str] = None
    interval: Optional[str] = None
    requested_start: Optional[str] = None
    requested_end: Optional[str] = None
    resolved_start: Optional[str] = None
    resolved_end: Optional[str] = None
    rows: Optional[int] = None
    source: Optional[str] = None
    price_adjustment: Optional[str] = None
    currency: Optional[str] = None
    volume_unit: Optional[str] = None
    coverage_complete: Optional[bool] = None
    data_quality_status: Optional[str] = Field(
        None, description="verified / partial / unknown"
    )
    input_eligibility: Optional[bool] = Field(
        None, description="是否满足默认策略收益计算的数据资格"
    )
    quality: Optional[Dict[str, Any]] = Field(None, description="质量评估明细（含缺失项）")
    payload_hash: Optional[str] = Field(None, description="冻结内容哈希")
    created_at: Optional[str] = None
    bars: Optional[List[Dict[str, Any]]] = Field(
        None, description="冻结行情明细；仅 include_bars=true 时返回"
    )


class BacktestResultItem(BaseModel):
    analysis_history_id: int
    code: str
    stock_name: Optional[str] = None
    analysis_date: Optional[str] = None
    eval_window_days: int
    engine_version: str
    eval_status: str
    evaluated_at: Optional[str] = None
    operation_advice: Optional[str] = None
    trend_prediction: Optional[str] = None
    position_recommendation: Optional[str] = None
    start_price: Optional[float] = None
    end_close: Optional[float] = None
    max_high: Optional[float] = None
    min_low: Optional[float] = None
    stock_return_pct: Optional[float] = None
    actual_return_pct: Optional[float] = None
    actual_movement: Optional[str] = None
    direction_expected: Optional[str] = None
    direction_correct: Optional[bool] = None
    outcome: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    hit_stop_loss: Optional[bool] = None
    hit_take_profit: Optional[bool] = None
    first_hit: Optional[str] = None
    first_hit_date: Optional[str] = None
    first_hit_trading_days: Optional[int] = None
    simulated_entry_price: Optional[float] = None
    simulated_exit_price: Optional[float] = None
    simulated_exit_reason: Optional[str] = None
    simulated_return_pct: Optional[float] = None


class BacktestResultsResponse(BaseModel):
    total: int
    page: int
    limit: int
    items: List[BacktestResultItem] = Field(default_factory=list)


class PerformanceMetrics(BaseModel):
    scope: str
    code: Optional[str] = None
    eval_window_days: int
    engine_version: str
    computed_at: Optional[str] = None

    total_evaluations: int
    completed_count: int
    insufficient_count: int
    long_count: int
    cash_count: int
    win_count: int
    loss_count: int
    neutral_count: int

    direction_accuracy_pct: Optional[float] = None
    win_rate_pct: Optional[float] = None
    neutral_rate_pct: Optional[float] = None
    avg_stock_return_pct: Optional[float] = None
    avg_simulated_return_pct: Optional[float] = None

    stop_loss_trigger_rate: Optional[float] = None
    take_profit_trigger_rate: Optional[float] = None
    ambiguous_rate: Optional[float] = None
    avg_days_to_first_hit: Optional[float] = None

    advice_breakdown: Dict[str, Any] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
