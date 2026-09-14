/**
 * Backtest API type definitions
 * Mirrors api/v1/schemas/backtest.py
 */

// ============ Request / Response ============

export interface BacktestRunRequest {
  code?: string;
  force?: boolean;
  evalWindowDays?: number;
  minAgeDays?: number;
  limit?: number;
}

export interface BacktestRunResponse {
  runId?: string;
  status?: string;
  processed: number;
  saved: number;
  completed: number;
  insufficient: number;
  errors: number;
}

// Preserve server keys in evidence exports so the canonical SHA256 remains reproducible.
export interface BacktestRunRecord {
  run_id: string;
  status: string;
  created_at: string;
  finished_at?: string | null;
  sha256?: string | null;
  /**
   * 冻结快照引用（WP4）。报告评估（ai_report_evaluation）可以没有快照；
   * 其它策略引擎在服务端必须引用合格快照，否则入口会直接拒绝。
   */
  snapshot_id?: string | null;
  /** 快照质量等级：verified / partial / unknown */
  data_quality_status?: string | null;
  /** 该快照是否满足默认策略收益计算的输入资格 */
  input_eligibility?: boolean | null;
  engine_kind?: string | null;
  engine_version?: string | null;
  evidence?: {
    kind: string;
    parameters: Record<string, unknown>;
    counts?: Record<string, number>;
    limitations: string[];
    items: Array<{
      code: string;
      analysis_history_id: number;
      requested_forward_bars: number;
      start_date_matches_request?: boolean;
      forward_bars: Array<{ date: string; data_source: string | null }>;
      start_bar: { date: string; data_source: string | null } | null;
      result: { eval_status: string };
    }>;
  };
}

// ============ Frozen Market Snapshot (WP4) ============

/** 冻结行情快照的只读视图（`bars` 仅在 include_bars=true 时返回）。 */
export interface MarketSnapshotRecord {
  snapshot_id: string;
  schema_version?: string | null;
  instrument: string;
  market?: string | null;
  interval?: string | null;
  requested_start?: string | null;
  requested_end?: string | null;
  resolved_start?: string | null;
  resolved_end?: string | null;
  rows?: number | null;
  source?: string | null;
  price_adjustment?: string | null;
  currency?: string | null;
  volume_unit?: string | null;
  coverage_complete?: boolean | null;
  data_quality_status?: string | null;
  input_eligibility?: boolean | null;
  /**
   * 质量评估明细：`missing` 为缺失的口径/覆盖项，
   * `coverage` 说明覆盖判定依据（是否真的按交易日历核对过）。
   */
  quality?: {
    missing?: string[];
    coverage?: {
      /** trading_calendar = 已按交易日历核对 session 数；endpoints_only = 只比对首尾日期 */
      basis?: string;
      expected_sessions?: number | null;
      rows?: number | null;
      deficit?: number | null;
    } | null;
  } | null;
  payload_hash?: string | null;
  created_at?: string | null;
}

// ============ Result Item ============

export interface BacktestResultItem {
  analysisHistoryId: number;
  code: string;
  stockName?: string;
  analysisDate?: string;
  evalWindowDays: number;
  engineVersion: string;
  evalStatus: string;
  evaluatedAt?: string;
  operationAdvice?: string;
  trendPrediction?: string;
  positionRecommendation?: string;
  startPrice?: number;
  endClose?: number;
  maxHigh?: number;
  minLow?: number;
  stockReturnPct?: number;
  actualReturnPct?: number;
  actualMovement?: string;
  directionExpected?: string;
  directionCorrect?: boolean;
  outcome?: string;
  stopLoss?: number;
  takeProfit?: number;
  hitStopLoss?: boolean;
  hitTakeProfit?: boolean;
  firstHit?: string;
  firstHitDate?: string;
  firstHitTradingDays?: number;
  simulatedEntryPrice?: number;
  simulatedExitPrice?: number;
  simulatedExitReason?: string;
  simulatedReturnPct?: number;
}

export interface BacktestResultsResponse {
  total: number;
  page: number;
  limit: number;
  items: BacktestResultItem[];
}

// ============ Performance Metrics ============

export interface PerformanceMetrics {
  scope: string;
  code?: string;
  evalWindowDays: number;
  engineVersion: string;
  computedAt?: string;

  totalEvaluations: number;
  completedCount: number;
  insufficientCount: number;
  longCount: number;
  cashCount: number;
  winCount: number;
  lossCount: number;
  neutralCount: number;

  directionAccuracyPct?: number;
  winRatePct?: number;
  neutralRatePct?: number;
  avgStockReturnPct?: number;
  avgSimulatedReturnPct?: number;

  stopLossTriggerRate?: number;
  takeProfitTriggerRate?: number;
  ambiguousRate?: number;
  avgDaysToFirstHit?: number;

  adviceBreakdown: Record<string, unknown>;
  diagnostics: Record<string, unknown>;
}
