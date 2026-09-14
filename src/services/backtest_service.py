# -*- coding: utf-8 -*-
"""Backtest orchestration service."""

from __future__ import annotations

from src.time_utils import beijing_now_naive
import json
import logging
import hashlib
from pathlib import Path
from uuid import uuid4
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, select

from src.config import get_config
from src.core.backtest_engine import OVERALL_SENTINEL_CODE, BacktestEngine, EvaluationConfig
from src.repositories.backtest_repo import BacktestRepository
from src.repositories.backtest_run_repo import BacktestRunRepository, json_value
from src.repositories.market_snapshot_repo import MarketDataSnapshotRepository
from src.repositories.stock_repo import StockRepository
from src.storage import AnalysisHistory, BacktestResult, BacktestSummary, DatabaseManager

logger = logging.getLogger(__name__)


class BacktestService:
    """Service layer to run and query backtests."""

    MAX_DYNAMIC_SUMMARY_ROWS = 2000

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()
        self.repo = BacktestRepository(self.db)
        self.stock_repo = StockRepository(self.db)

    ENGINE_KIND_REPORT_EVALUATION = "ai_report_evaluation"
    # 只有报告事后评估真正实现；其它引擎名一律拒绝，避免把旧引擎的运行结果
    # 标成组合/策略引擎，也避免「只记录 snapshot_id」被读成「消费了冻结行情」。
    IMPLEMENTED_ENGINE_KINDS = (ENGINE_KIND_REPORT_EVALUATION,)

    def run_backtest(
        self, *, code=None, force=False, eval_window_days=None, min_age_days=None, limit=200,
        snapshot_id: Optional[str] = None,
        engine_kind: str = ENGINE_KIND_REPORT_EVALUATION,
    ) -> Dict[str, Any]:
        """Record the inputs actually evaluated; never infer execution from a rollup.

        引擎门槛（WP4 修订）：

        - 只实现 ``ai_report_evaluation``（报告事后评估）。传入其它 ``engine_kind``
          一律直接拒绝：旧引擎没有消费冻结行情，把它标成组合/策略引擎会让运行记录
          与真实计算不符。
        - 报告评估仍可附加 ``snapshot_id`` 作为**引用**（用于核对当时的口径与质量），
          但证据里会写明 ``consumed=false``：报告引擎读取的是数据库/抓取路径，
          不是该快照的 bars。
        - 未知名/不存在的 ``snapshot_id`` 仍会被拒绝。
        """
        if engine_kind not in self.IMPLEMENTED_ENGINE_KINDS:
            raise ValueError(
                f"engine_kind={engine_kind!r} is not implemented; refusing to label the "
                "report evaluation engine as another engine. Implemented: "
                f"{', '.join(self.IMPLEMENTED_ENGINE_KINDS)}"
            )
        config = get_config()
        params = {
            "code": code, "force": force, "limit": int(limit),
            "eval_window_days": int(eval_window_days if eval_window_days is not None
                                    else getattr(config, "backtest_eval_window_days", 10)),
            "min_age_days": int(min_age_days if min_age_days is not None
                               else getattr(config, "backtest_min_age_days", 14)),
        }
        engine_settings = {
            "engine_version": str(getattr(config, "backtest_engine_version", "v1")),
            "neutral_band_pct": float(getattr(config, "backtest_neutral_band_pct", 2.0)),
        }

        snapshot_ref = self._resolve_snapshot_reference(snapshot_id=snapshot_id)
        evidence = {
            "schema_version": 1, "kind": engine_kind,
            "parameters": {**params, **engine_settings},
            "code_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (Path(__file__), Path(__file__).parents[1] / "core/backtest_engine.py")},
            "limitations": ["Not a portfolio backtest; capital, fees and slippage are not modeled.",
                            "Adjustment is unknown; bar count does not validate trading-calendar continuity.",
                            "Current stored/fetched data does not prove historical point-in-time availability."],
            "items": [],
        }
        if snapshot_ref is not None:
            evidence["snapshot"] = {
                "snapshot_id": snapshot_ref["snapshot_id"],
                "data_quality_status": snapshot_ref["data_quality_status"],
                "input_eligibility": snapshot_ref["input_eligibility"],
                "coverage_complete": snapshot_ref["coverage_complete"],
                # 引用只用于核对当时的口径与质量；报告评估引擎不读取冻结 bars。
                "consumed": False,
            }
            evidence["limitations"].append(
                "The referenced snapshot was not consumed by this engine; it records the "
                "input quality of the run, it does not prove the frozen bars were used."
            )
        run_id = uuid4().hex
        runs = BacktestRunRepository(self.db)
        runs.create(
            run_id, evidence,
            snapshot_id=snapshot_ref["snapshot_id"] if snapshot_ref else None,
            data_quality_status=snapshot_ref["data_quality_status"] if snapshot_ref else None,
            input_eligibility=snapshot_ref["input_eligibility"] if snapshot_ref else None,
            engine_kind=engine_kind,
            engine_version=engine_settings["engine_version"],
        )
        try:
            stats = self._run_backtest(**params, audit_items=evidence["items"], engine_settings=engine_settings)
            status = ("empty" if not stats["processed"] else
                      "partial" if stats["insufficient"] or stats["errors"] else "completed")
            evidence["counts"] = stats
            runs.finish(run_id, status, evidence)
        except Exception:
            evidence["failure"] = "Execution or persistence failed; some result rows may already be saved."
            try:
                runs.finish(run_id, "failed", evidence)
            except Exception:
                logger.exception("Unable to finalize backtest run %s", run_id)
            raise
        return {**stats, "run_id": run_id, "status": status}

    def get_run(self, run_id):
        return BacktestRunRepository(self.db).get(run_id)

    def get_runs(self):
        return BacktestRunRepository(self.db).recent()

    def _resolve_snapshot_reference(self, *, snapshot_id):
        """解析并校验快照引用。

        只校验存在性：引用（用于核对当时口径与质量）**不等于消费**。真正消费冻结
        行情需要引擎从 snapshot 的 bars 计算，目前只有报告评估引擎，且它不消费。
        """
        if snapshot_id is None:
            return None
        snapshot = MarketDataSnapshotRepository(self.db).get(snapshot_id)
        if snapshot is None:
            raise ValueError(f"Unknown snapshot_id: {snapshot_id}")
        return snapshot

    @staticmethod
    def _snapshot_bar(bar):
        return json_value({key: getattr(bar, key, None)
                           for key in ("date", "open", "high", "low", "close", "volume", "data_source")})

    def _run_backtest(
        self,
        *,
        code: Optional[str] = None,
        force: bool = False,
        eval_window_days: Optional[int] = None,
        min_age_days: Optional[int] = None,
        limit: int = 200,
        audit_items=None,
        engine_settings=None,
    ) -> Dict[str, Any]:
        config = get_config()

        if eval_window_days is None:
            eval_window_days = getattr(config, "backtest_eval_window_days", 10)
        if min_age_days is None:
            min_age_days = getattr(config, "backtest_min_age_days", 14)

        engine_version = engine_settings["engine_version"]
        neutral_band_pct = engine_settings["neutral_band_pct"]

        eval_config = EvaluationConfig(
            eval_window_days=int(eval_window_days),
            neutral_band_pct=neutral_band_pct,
            engine_version=str(engine_version),
        )

        candidates = self.repo.get_candidates(
            code=code,
            min_age_days=int(min_age_days),
            limit=int(limit),
            eval_window_days=int(eval_window_days),
            engine_version=str(engine_version),
            force=force,
        )

        processed = 0
        completed = 0
        insufficient = 0
        errors = 0
        touched_codes: set[str] = set()

        results_to_save: List[BacktestResult] = []

        for analysis in candidates:
            processed += 1
            touched_codes.add(analysis.code)
            item = {"analysis_history_id": analysis.id, "code": analysis.code,
                    "operation_advice": analysis.operation_advice,
                    "stop_loss": analysis.stop_loss, "take_profit": analysis.take_profit,
                    "adjustment": "unknown", "coverage_validation": "bar_count_only",
                    "requested_forward_bars": int(eval_window_days), "start_bar": None,
                    "forward_bars": []}

            try:
                analysis_date = self._resolve_analysis_date(analysis)
                item["requested_analysis_date"] = json_value(analysis_date)
                if analysis_date is None:
                    item["reason"] = "missing_analysis_date"
                    errors += 1
                    results_to_save.append(
                        BacktestResult(
                            analysis_history_id=analysis.id,
                            code=analysis.code,
                            eval_window_days=int(eval_window_days),
                            engine_version=str(engine_version),
                            eval_status="error",
                            evaluated_at=beijing_now_naive(),
                            operation_advice=analysis.operation_advice,
                        )
                    )
                    continue
                start_daily = self.stock_repo.get_start_daily(code=analysis.code, analysis_date=analysis_date)

                if start_daily is None or start_daily.close is None:
                    self._try_fill_daily_data(code=analysis.code, analysis_date=analysis_date, eval_window_days=eval_window_days)
                    start_daily = self.stock_repo.get_start_daily(code=analysis.code, analysis_date=analysis_date)

                if start_daily is None or start_daily.close is None:
                    insufficient += 1
                    item["reason"] = "missing_start_price"
                    results_to_save.append(
                        BacktestResult(
                            analysis_history_id=analysis.id,
                            code=analysis.code,
                            analysis_date=analysis_date,
                            eval_window_days=int(eval_window_days),
                            engine_version=str(engine_version),
                            eval_status="insufficient_data",
                            evaluated_at=beijing_now_naive(),
                            operation_advice=analysis.operation_advice,
                        )
                    )
                    continue

                forward_bars = self.stock_repo.get_forward_bars(
                    code=analysis.code,
                    analysis_date=start_daily.date,
                    eval_window_days=int(eval_window_days),
                )

                if len(forward_bars) < int(eval_window_days):
                    self._try_fill_daily_data(code=analysis.code, analysis_date=start_daily.date, eval_window_days=eval_window_days)
                    forward_bars = self.stock_repo.get_forward_bars(
                        code=analysis.code,
                        analysis_date=start_daily.date,
                        eval_window_days=int(eval_window_days),
                    )

                item["start_bar"] = self._snapshot_bar(start_daily)
                item["forward_bars"] = [self._snapshot_bar(bar) for bar in forward_bars]
                item["start_date_matches_request"] = start_daily.date == analysis_date
                evaluation = BacktestEngine.evaluate_single(
                    operation_advice=analysis.operation_advice,
                    analysis_date=start_daily.date,
                    start_price=float(start_daily.close),
                    forward_bars=forward_bars,
                    stop_loss=analysis.stop_loss,
                    take_profit=analysis.take_profit,
                    config=eval_config,
                )

                status = evaluation.get("eval_status")
                if status == "insufficient_data":
                    item["reason"] = "insufficient_forward_data"
                    insufficient += 1
                elif status == "completed":
                    completed += 1
                else:
                    errors += 1

                results_to_save.append(
                    BacktestResult(
                        analysis_history_id=analysis.id,
                        code=analysis.code,
                        analysis_date=evaluation.get("analysis_date"),
                        eval_window_days=int(evaluation.get("eval_window_days") or eval_window_days),
                        engine_version=str(evaluation.get("engine_version") or engine_version),
                        eval_status=str(evaluation.get("eval_status") or "error"),
                        evaluated_at=beijing_now_naive(),
                        operation_advice=evaluation.get("operation_advice"),
                        position_recommendation=evaluation.get("position_recommendation"),
                        start_price=evaluation.get("start_price"),
                        end_close=evaluation.get("end_close"),
                        max_high=evaluation.get("max_high"),
                        min_low=evaluation.get("min_low"),
                        stock_return_pct=evaluation.get("stock_return_pct"),
                        direction_expected=evaluation.get("direction_expected"),
                        direction_correct=evaluation.get("direction_correct"),
                        outcome=evaluation.get("outcome"),
                        stop_loss=evaluation.get("stop_loss"),
                        take_profit=evaluation.get("take_profit"),
                        hit_stop_loss=evaluation.get("hit_stop_loss"),
                        hit_take_profit=evaluation.get("hit_take_profit"),
                        first_hit=evaluation.get("first_hit"),
                        first_hit_date=evaluation.get("first_hit_date"),
                        first_hit_trading_days=evaluation.get("first_hit_trading_days"),
                        simulated_entry_price=evaluation.get("simulated_entry_price"),
                        simulated_exit_price=evaluation.get("simulated_exit_price"),
                        simulated_exit_reason=evaluation.get("simulated_exit_reason"),
                        simulated_return_pct=evaluation.get("simulated_return_pct"),
                    )
                )

            except Exception as exc:
                errors += 1
                item["reason"] = "evaluation_exception"
                logger.error(f"回测失败: {analysis.code}#{analysis.id}: {exc}")
                results_to_save.append(
                    BacktestResult(
                        analysis_history_id=analysis.id,
                        code=analysis.code,
                        analysis_date=self._resolve_analysis_date(analysis),
                        eval_window_days=int(eval_window_days),
                        engine_version=str(engine_version),
                        eval_status="error",
                        evaluated_at=beijing_now_naive(),
                        operation_advice=analysis.operation_advice,
                    )
                )

            finally:
                # Copy before save_results_batch expires ORM attributes on commit.
                result = results_to_save[-1]
                item["result"] = json_value({column.name: getattr(result, column.name)
                                              for column in BacktestResult.__table__.columns
                                              if column.name != "id"})
                audit_items.append(item)

        saved = 0
        if results_to_save:
            # Retry incomplete/error evaluations without duplicating their unique keys.
            saved = self.repo.save_results_batch(results_to_save, replace_existing=True)

        if saved:
            self._recompute_summaries(
                touched_codes=sorted(touched_codes),
                eval_window_days=int(eval_window_days),
                engine_version=str(engine_version),
            )

        return {
            "processed": processed,
            "saved": saved,
            "completed": completed,
            "insufficient": insufficient,
            "errors": errors,
        }

    def get_recent_evaluations(
        self,
        *,
        code: Optional[str],
        eval_window_days: Optional[int] = None,
        limit: int = 50,
        page: int = 1,
        analysis_date_from: Optional[date] = None,
        analysis_date_to: Optional[date] = None,
    ) -> Dict[str, Any]:
        config = get_config()
        engine_version = str(getattr(config, "backtest_engine_version", "v1"))

        # When date filters are active and no explicit window is requested,
        # infer the smallest available window to stay aligned with summary metrics.
        if eval_window_days is None and (analysis_date_from is not None or analysis_date_to is not None):
            windows = self.repo.get_distinct_eval_windows(
                code=code,
                engine_version=engine_version,
                analysis_date_from=analysis_date_from,
                analysis_date_to=analysis_date_to,
            )
            if windows:
                eval_window_days = windows[0]

        offset = max(page - 1, 0) * limit
        rows, total = self.repo.get_results_paginated(
            code=code,
            eval_window_days=eval_window_days,
            engine_version=engine_version,
            analysis_date_from=analysis_date_from,
            analysis_date_to=analysis_date_to,
            days=None,
            offset=offset,
            limit=limit,
        )
        items = [self._result_to_dict(result, stock_name, trend_prediction) for result, stock_name, trend_prediction, _ in rows]
        return {"total": total, "page": page, "limit": limit, "items": items}

    def get_summary(
        self,
        *,
        scope: str,
        code: Optional[str],
        eval_window_days: Optional[int] = None,
        analysis_date_from: Optional[date] = None,
        analysis_date_to: Optional[date] = None,
    ) -> Optional[Dict[str, Any]]:
        config = get_config()
        engine_version = str(getattr(config, "backtest_engine_version", "v1"))
        lookup_code = OVERALL_SENTINEL_CODE if scope == "overall" else code

        if analysis_date_from is not None or analysis_date_to is not None:
            ew = int(eval_window_days) if eval_window_days is not None else None
            count = self.repo.count_results(
                code=code,
                eval_window_days=ew,
                engine_version=engine_version,
                analysis_date_from=analysis_date_from,
                analysis_date_to=analysis_date_to,
            )
            if count > self.MAX_DYNAMIC_SUMMARY_ROWS:
                raise ValueError(
                    "Date-filtered summary matches too many rows; narrow the analysis date range or stock code."
                )
            rows = self.repo.list_results(
                code=code,
                eval_window_days=ew,
                engine_version=engine_version,
                analysis_date_from=analysis_date_from,
                analysis_date_to=analysis_date_to,
            )
            return self._build_dynamic_summary(
                rows=rows,
                scope=scope,
                code=lookup_code,
                eval_window_days=int(eval_window_days) if eval_window_days is not None else None,
                engine_version=engine_version,
                max_rows=self.MAX_DYNAMIC_SUMMARY_ROWS,
            )

        summary = self.repo.get_summary(
            scope=scope,
            code=lookup_code,
            eval_window_days=eval_window_days,
            engine_version=engine_version,
        )
        if summary is None:
            # Results may have committed before summary generation failed.
            window = eval_window_days if eval_window_days is not None else config.backtest_eval_window_days
            rows = self.repo.list_results(code=code, eval_window_days=window, engine_version=engine_version)
            if not rows:
                return None
            return self._build_dynamic_summary(
                rows=rows, scope=scope, code=lookup_code,
                eval_window_days=window, engine_version=engine_version,
            )
        return self._summary_to_dict(summary)

    def get_global_summary(self, *, eval_window_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Return overall backtest metrics normalized for Agent memory consumers."""
        return self._normalize_learning_summary(
            self.get_summary(scope="overall", code=None, eval_window_days=eval_window_days)
        )

    def get_stock_summary(self, code: str, *, eval_window_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Return per-stock backtest metrics normalized for Agent memory consumers."""
        return self._normalize_learning_summary(
            self.get_summary(scope="stock", code=code, eval_window_days=eval_window_days)
        )

    def get_skill_summary(self, skill_id: str, *, eval_window_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Aggregate explicitly single-skill analyses; never infer legacy attribution.

        Combined-skill and multi-agent decisions are excluded because their final
        return cannot be attributed to an individual skill for auto-weighting.
        """
        if not skill_id or not skill_id.strip():
            return None
        skill_id = skill_id.strip()
        config = get_config()
        window = eval_window_days if eval_window_days is not None else config.backtest_eval_window_days
        version = config.backtest_engine_version
        results = []
        with self.db.get_session() as session:
            query = (
                select(BacktestResult, AnalysisHistory.raw_result)
                .join(AnalysisHistory, AnalysisHistory.id == BacktestResult.analysis_history_id)
                .where(BacktestResult.eval_window_days == window, BacktestResult.engine_version == version)
            )
            for result, raw in session.execute(query).yield_per(500):
                try:
                    payload = json.loads(raw or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and payload.get("analysis_skill_ids") == [skill_id]:
                    results.append(result)
        if not results:
            return None
        summary = self._build_dynamic_summary(
            rows=results, scope="skill", code=skill_id,
            eval_window_days=window, engine_version=version,
        )
        summary["skill_id"] = skill_id
        return self._normalize_learning_summary(summary)

    def get_strategy_summary(self, strategy_id: str, *, eval_window_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Compatibility wrapper for legacy strategy-based callers."""
        summary = self.get_skill_summary(strategy_id, eval_window_days=eval_window_days)
        if summary is None:
            return None
        normalized = dict(summary)
        normalized["strategy_id"] = strategy_id
        return normalized

    def _resolve_analysis_date(self, analysis) -> Optional[date]:
        parsed = self.repo.parse_analysis_date_from_snapshot(analysis.context_snapshot)
        if parsed:
            return parsed
        if getattr(analysis, "created_at", None):
            return analysis.created_at.date()
        logger.warning(f"无法确定分析日期，跳过记录: {analysis.code}#{getattr(analysis, 'id', '?')}")
        return None

    def _try_fill_daily_data(self, *, code: str, analysis_date: date, eval_window_days: int) -> None:
        try:
            from data_provider.base import DataFetcherManager

            # fetch a window that covers start + forward bars
            end_date = analysis_date + timedelta(days=max(eval_window_days * 2, 30))
            manager = DataFetcherManager()
            df, source = manager.get_daily_data(
                stock_code=code,
                start_date=analysis_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                days=eval_window_days * 2,
            )
            if df is None or df.empty:
                return
            self.db.save_daily_data(df, code=code, data_source=source)
        except Exception as exc:
            logger.warning(f"补全日线数据失败({code}): {exc}")

    def _recompute_summaries(self, *, touched_codes: List[str], eval_window_days: int, engine_version: str) -> None:
        with self.db.get_session() as session:
            # overall
            overall_rows = session.execute(
                select(BacktestResult).where(
                    and_(
                        BacktestResult.eval_window_days == eval_window_days,
                        BacktestResult.engine_version == engine_version,
                    )
                )
            ).scalars().all()
            overall_data = BacktestEngine.compute_summary(
                results=overall_rows,
                scope="overall",
                code=OVERALL_SENTINEL_CODE,
                eval_window_days=eval_window_days,
                engine_version=engine_version,
            )
            overall_summary = self._build_summary_model(overall_data)
            self.repo.upsert_summary(overall_summary)

            for code in touched_codes:
                rows = session.execute(
                    select(BacktestResult).where(
                        and_(
                            BacktestResult.code == code,
                            BacktestResult.eval_window_days == eval_window_days,
                            BacktestResult.engine_version == engine_version,
                        )
                    )
                ).scalars().all()
                data = BacktestEngine.compute_summary(
                    results=rows,
                    scope="stock",
                    code=code,
                    eval_window_days=eval_window_days,
                    engine_version=engine_version,
                )
                summary = self._build_summary_model(data)
                self.repo.upsert_summary(summary)

    @staticmethod
    def _build_summary_model(summary_data: Dict[str, Any]) -> BacktestSummary:
        return BacktestSummary(
            scope=summary_data.get("scope"),
            code=summary_data.get("code"),
            eval_window_days=summary_data.get("eval_window_days"),
            engine_version=summary_data.get("engine_version"),
            computed_at=beijing_now_naive(),
            total_evaluations=summary_data.get("total_evaluations") or 0,
            completed_count=summary_data.get("completed_count") or 0,
            insufficient_count=summary_data.get("insufficient_count") or 0,
            long_count=summary_data.get("long_count") or 0,
            cash_count=summary_data.get("cash_count") or 0,
            win_count=summary_data.get("win_count") or 0,
            loss_count=summary_data.get("loss_count") or 0,
            neutral_count=summary_data.get("neutral_count") or 0,
            direction_accuracy_pct=summary_data.get("direction_accuracy_pct"),
            win_rate_pct=summary_data.get("win_rate_pct"),
            neutral_rate_pct=summary_data.get("neutral_rate_pct"),
            avg_stock_return_pct=summary_data.get("avg_stock_return_pct"),
            avg_simulated_return_pct=summary_data.get("avg_simulated_return_pct"),
            stop_loss_trigger_rate=summary_data.get("stop_loss_trigger_rate"),
            take_profit_trigger_rate=summary_data.get("take_profit_trigger_rate"),
            ambiguous_rate=summary_data.get("ambiguous_rate"),
            avg_days_to_first_hit=summary_data.get("avg_days_to_first_hit"),
            advice_breakdown_json=json.dumps(summary_data.get("advice_breakdown") or {}, ensure_ascii=False),
            diagnostics_json=json.dumps(summary_data.get("diagnostics") or {}, ensure_ascii=False),
        )

    @staticmethod
    def _result_to_dict(
        row: BacktestResult,
        stock_name: Optional[str] = None,
        trend_prediction: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "analysis_history_id": row.analysis_history_id,
            "code": row.code,
            "stock_name": stock_name,
            "analysis_date": row.analysis_date.isoformat() if row.analysis_date else None,
            "eval_window_days": row.eval_window_days,
            "engine_version": row.engine_version,
            "eval_status": row.eval_status,
            "evaluated_at": row.evaluated_at.isoformat() if row.evaluated_at else None,
            "operation_advice": row.operation_advice,
            "trend_prediction": trend_prediction,
            "position_recommendation": row.position_recommendation,
            "start_price": row.start_price,
            "end_close": row.end_close,
            "max_high": row.max_high,
            "min_low": row.min_low,
            "stock_return_pct": row.stock_return_pct,
            "actual_return_pct": row.stock_return_pct,
            "actual_movement": BacktestService._actual_movement_from_return(row.stock_return_pct),
            "direction_expected": row.direction_expected,
            "direction_correct": row.direction_correct,
            "outcome": row.outcome,
            "stop_loss": row.stop_loss,
            "take_profit": row.take_profit,
            "hit_stop_loss": row.hit_stop_loss,
            "hit_take_profit": row.hit_take_profit,
            "first_hit": row.first_hit,
            "first_hit_date": row.first_hit_date.isoformat() if row.first_hit_date else None,
            "first_hit_trading_days": row.first_hit_trading_days,
            "simulated_entry_price": row.simulated_entry_price,
            "simulated_exit_price": row.simulated_exit_price,
            "simulated_exit_reason": row.simulated_exit_reason,
            "simulated_return_pct": row.simulated_return_pct,
        }

    @staticmethod
    def _summary_to_dict(row: BacktestSummary) -> Dict[str, Any]:
        return {
            "scope": row.scope,
            "code": None if row.code == OVERALL_SENTINEL_CODE else row.code,
            "eval_window_days": row.eval_window_days,
            "engine_version": row.engine_version,
            "computed_at": row.computed_at.isoformat() if row.computed_at else None,
            "total_evaluations": row.total_evaluations,
            "completed_count": row.completed_count,
            "insufficient_count": row.insufficient_count,
            "long_count": row.long_count,
            "cash_count": row.cash_count,
            "win_count": row.win_count,
            "loss_count": row.loss_count,
            "neutral_count": row.neutral_count,
            "direction_accuracy_pct": row.direction_accuracy_pct,
            "win_rate_pct": row.win_rate_pct,
            "neutral_rate_pct": row.neutral_rate_pct,
            "avg_stock_return_pct": row.avg_stock_return_pct,
            "avg_simulated_return_pct": row.avg_simulated_return_pct,
            "stop_loss_trigger_rate": row.stop_loss_trigger_rate,
            "take_profit_trigger_rate": row.take_profit_trigger_rate,
            "ambiguous_rate": row.ambiguous_rate,
            "avg_days_to_first_hit": row.avg_days_to_first_hit,
            "advice_breakdown": json.loads(row.advice_breakdown_json) if row.advice_breakdown_json else {},
            "diagnostics": json.loads(row.diagnostics_json) if row.diagnostics_json else {},
        }

    @staticmethod
    def _normalize_learning_summary(summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Normalize summary metrics to the ratio-based shape expected by Agent memory."""
        if summary is None:
            return None

        normalized = dict(summary)
        normalized["win_rate"] = BacktestService._pct_to_ratio(summary.get("win_rate_pct"), default=0.5)
        normalized["direction_accuracy"] = BacktestService._pct_to_ratio(
            summary.get("direction_accuracy_pct"),
            default=0.5,
        )

        avg_return_pct = summary.get("avg_simulated_return_pct")
        if avg_return_pct is None:
            avg_return_pct = summary.get("avg_stock_return_pct")
        normalized["avg_return"] = BacktestService._pct_to_ratio(avg_return_pct, default=0.0)
        return normalized

    @staticmethod
    def _pct_to_ratio(value: Optional[float], default: float = 0.0) -> float:
        try:
            return float(value) / 100.0
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _actual_movement_from_return(value: Optional[float]) -> Optional[str]:
        if value is None:
            return None
        try:
            actual_return = float(value)
        except (TypeError, ValueError):
            return None
        if actual_return > 0:
            return "up"
        if actual_return < 0:
            return "down"
        return "flat"

    @staticmethod
    def _build_dynamic_summary(
        *,
        rows: List[BacktestResult],
        scope: str,
        code: Optional[str],
        eval_window_days: Optional[int],
        engine_version: str,
        max_rows: Optional[int] = None,
    ) -> Dict[str, Any]:
        filtered_rows = [row for row in rows if getattr(row, "engine_version", None) == engine_version]
        if eval_window_days is not None:
            summary_window_days = int(eval_window_days)
        else:
            window_values = sorted({
                int(row.eval_window_days)
                for row in filtered_rows
                if getattr(row, "eval_window_days", None) is not None
            })
            if len(window_values) > 1:
                logger.warning(
                    "Multiple eval_window_days values found for dynamic summary; using %s for engine_version=%s, scope=%s, code=%s",
                    window_values[0],
                    engine_version,
                    scope,
                    code,
                )
            if window_values:
                summary_window_days = window_values[0]
            else:
                summary_window_days = int(getattr(get_config(), "backtest_eval_window_days", 10))

        filtered_rows = [
            row for row in filtered_rows if getattr(row, "eval_window_days", None) == summary_window_days
        ]

        if max_rows is not None and len(filtered_rows) > max_rows:
            raise ValueError(
                "Date-filtered summary matches too many rows; narrow the analysis date range or stock code."
            )

        summary = BacktestEngine.compute_summary(
            results=filtered_rows,
            scope=scope,
            code=code,
            eval_window_days=summary_window_days,
            engine_version=engine_version,
        )
        summary["code"] = None if summary.get("code") == OVERALL_SENTINEL_CODE else summary.get("code")
        summary["computed_at"] = beijing_now_naive().isoformat()
        return summary
