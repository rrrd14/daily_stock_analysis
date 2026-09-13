# -*- coding: utf-8 -*-
"""Integration tests for backtest service and repository.

These tests run against a temporary SQLite DB (same approach as other tests)
and validate idempotency/force semantics, result field correctness,
summary creation, and query methods.
"""

import os
import json
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from src.config import Config
from src.core.backtest_engine import OVERALL_SENTINEL_CODE
from src.services.backtest_service import BacktestService
from src.storage import AnalysisHistory, BacktestResult, BacktestSummary, DatabaseManager, StockDaily


class BacktestServiceTestCase(unittest.TestCase):
    def test_run_evidence_is_immutable_and_matches_engine_inputs(self):
        import hashlib
        service = BacktestService(self.db)
        first = service.run_backtest(eval_window_days=3, min_age_days=0)
        record = service.get_run(first["run_id"])
        self.assertEqual(record["status"], "completed")
        evidence = record["evidence"]
        item = evidence["items"][0]
        self.assertEqual(item["start_bar"]["close"], 100)
        self.assertEqual(len(item["forward_bars"]), 3)
        self.assertAlmostEqual(item["result"]["stock_return_pct"], 7)
        self.assertEqual(item["adjustment"], "unknown")
        encoded = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.assertEqual(record["sha256"], hashlib.sha256(encoded.encode()).hexdigest())
        with self.db.get_session() as session:
            session.query(StockDaily).filter(StockDaily.date == date(2024, 1, 4)).update({"close": 120})
            session.commit()
        second = service.run_backtest(eval_window_days=3, min_age_days=0, force=True)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(service.get_run(first["run_id"]), record)
        self.assertAlmostEqual(service.get_run(second["run_id"])["evidence"]["items"][0]["result"]["stock_return_pct"], 20)

    def test_empty_and_insufficient_runs_are_not_success(self):
        service = BacktestService(self.db)
        empty = service.run_backtest(code="DOES_NOT_EXIST")
        self.assertEqual(empty["status"], "empty")
        with patch.object(service, "_try_fill_daily_data"):
            partial = service.run_backtest(eval_window_days=10, min_age_days=0)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(service.get_run(partial["run_id"])["evidence"]["counts"]["completed"], 0)

    def test_run_failure_is_recorded_without_exception_secrets(self):
        service = BacktestService(self.db)
        with patch.object(service.repo, "save_results_batch", side_effect=RuntimeError("secret-token")):
            with self.assertRaises(RuntimeError):
                service.run_backtest(eval_window_days=3, min_age_days=0)
        record = service.get_run(service.get_runs()[0]["run_id"])
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("secret-token", json.dumps(record))
        from src.repositories.backtest_run_repo import BacktestRunRepository
        with self.assertRaises(ValueError):
            BacktestRunRepository(self.db).finish(record["run_id"], "completed", {})

    def test_interrupted_run_remains_unfinished(self):
        service = BacktestService(self.db)
        with patch.object(service.repo, "get_candidates", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                service.run_backtest()
        record = service.get_run(service.get_runs()[0]["run_id"])
        self.assertEqual(record["status"], "running")
        self.assertIsNone(record["sha256"])
        self.assertIsNone(record["finished_at"])

    def test_run_api_and_agent_read_authoritative_records(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.deps import get_database_manager
        from api.v1.endpoints.backtest import router
        from src.agent.tools.backtest_tools import _handle_get_backtest_run
        app = FastAPI()
        app.include_router(router, prefix="/backtest")
        app.dependency_overrides[get_database_manager] = lambda: self.db
        with TestClient(app) as client:
            response = client.post("/backtest/run", json={"eval_window_days": 3, "min_age_days": 0})
            self.assertEqual(response.status_code, 200)
            run_id = response.json()["run_id"]
            record = client.get(f"/backtest/runs/{run_id}").json()
            self.assertEqual(record["evidence"]["counts"]["completed"], 1)
            self.assertEqual(client.get("/backtest/runs").json()[0]["run_id"], run_id)
            self.assertEqual(client.get("/backtest/runs/missing").status_code, 404)
        with patch("src.agent.tools.backtest_tools._get_backtest_service", return_value=BacktestService(self.db)):
            self.assertEqual(_handle_get_backtest_run(run_id)["url"], f"/backtest?run={run_id}")
            self.assertEqual(_handle_get_backtest_run("missing")["status"], "not_found")

    def test_skill_attribution_persists_even_without_context_snapshot(self):
        result = SimpleNamespace(
            code="600519", name="test", sentiment_score=70,
            operation_advice="buy", trend_prediction="up", analysis_summary="test",
            analysis_skill_ids=["bull_trend"],
        )
        saved = self.db.save_analysis_history(
            result, "attributed", "simple", None,
            context_snapshot={"secret_context": "excluded"}, save_snapshot=False,
        )
        self.assertEqual(saved, 1)
        with self.db.get_session() as session:
            row = session.query(AnalysisHistory).filter_by(query_id="attributed").one()
            self.assertIsNone(row.context_snapshot)
            self.assertEqual(json.loads(row.raw_result)["analysis_skill_ids"], ["bull_trend"])

    def test_incomplete_result_is_retried_and_replaced(self):
        service = BacktestService(self.db)
        with self.db.get_session() as session:
            session.query(StockDaily).filter(StockDaily.date == date(2024, 1, 4)).delete()
            session.commit()
        with patch.object(service, "_try_fill_daily_data"):
            first = service.run_backtest(eval_window_days=3, min_age_days=0)
        self.assertEqual(first["insufficient"], 1)
        with self.db.get_session() as session:
            session.add(StockDaily(code="600519", date=date(2024, 1, 4), high=109, low=104, close=107))
            session.commit()
        second = service.run_backtest(eval_window_days=3, min_age_days=0)
        self.assertEqual(second["completed"], 1)
        with self.db.get_session() as session:
            self.assertEqual(session.query(BacktestResult).count(), 1)
        self.assertEqual(service.run_backtest(eval_window_days=3, min_age_days=0)["processed"], 0)

    def test_skill_summary_uses_only_explicit_single_skill_attribution(self):
        service = BacktestService(self.db)
        service.run_backtest(eval_window_days=3, min_age_days=0)
        for tags, expected in [([], False), (["bull_trend", "other"], False), (["bull_trend"], True)]:
            with self.db.get_session() as session:
                row = session.query(AnalysisHistory).first()
                row.raw_result = json.dumps({"analysis_skill_ids": tags})
                session.commit()
            summary = service.get_skill_summary("bull_trend", eval_window_days=3)
            self.assertEqual(summary is not None, expected)
            if expected:
                self.assertEqual(summary["completed_count"], 1)
                self.assertEqual(summary["scope"], "skill")
                self.assertEqual(summary["win_rate"], summary["win_rate_pct"] / 100)
        self.assertIsNone(service.get_skill_summary("bull_trend", eval_window_days=30))
        self.assertIsNone(service.get_skill_summary("other", eval_window_days=3))

    def test_summary_recovers_from_results_without_rollup(self):
        service = BacktestService(self.db)
        service.run_backtest(eval_window_days=3, min_age_days=0)
        with self.db.get_session() as session:
            session.query(BacktestSummary).delete()
            session.commit()
        self.assertEqual(service.get_summary(scope="overall", code=None, eval_window_days=3)["completed_count"], 1)

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_backtest_service.db")
        os.environ["DATABASE_PATH"] = self._db_path
        os.environ["BACKTEST_EVAL_WINDOW_DAYS"] = "3"

        Config._instance = None
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

        # Ensure analysis is old enough for default min_age_days=14
        old_created_at = datetime(2024, 1, 1, 0, 0, 0)

        with self.db.get_session() as session:
            session.add(
                AnalysisHistory(
                    query_id="q1",
                    code="600519",
                    name="贵州茅台",
                    report_type="simple",
                    sentiment_score=80,
                    operation_advice="买入",
                    trend_prediction="看多",
                    analysis_summary="test",
                    stop_loss=95.0,
                    take_profit=110.0,
                    created_at=old_created_at,
                    context_snapshot='{"enhanced_context": {"date": "2024-01-01"}}',
                )
            )

            # Analysis day close
            session.add(
                StockDaily(
                    code="600519",
                    date=date(2024, 1, 1),
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.0,
                )
            )

            # Forward bars (3 days) that hit take-profit on day1
            session.add_all(
                [
                    StockDaily(code="600519", date=date(2024, 1, 2), high=111.0, low=100.0, close=105.0),
                    StockDaily(code="600519", date=date(2024, 1, 3), high=108.0, low=103.0, close=106.0),
                    StockDaily(code="600519", date=date(2024, 1, 4), high=109.0, low=104.0, close=107.0),
                ]
            )
            session.commit()

    def _seed_analysis(
        self,
        *,
        query_id: str,
        analysis_date: date,
        created_at: datetime,
        operation_advice: str,
        trend_prediction: str,
        start_close: float,
        forward_bars: list[StockDaily],
    ) -> None:
        with self.db.get_session() as session:
            session.add(
                AnalysisHistory(
                    query_id=query_id,
                    code="600519",
                    name="贵州茅台",
                    report_type="simple",
                    sentiment_score=60,
                    operation_advice=operation_advice,
                    trend_prediction=trend_prediction,
                    analysis_summary="extra-test",
                    stop_loss=None,
                    take_profit=None,
                    created_at=created_at,
                    context_snapshot=f'{{"enhanced_context": {{"date": "{analysis_date.isoformat()}"}}}}',
                )
            )
            session.add(
                StockDaily(
                    code="600519",
                    date=analysis_date,
                    open=start_close,
                    high=start_close,
                    low=start_close,
                    close=start_close,
                )
            )
            session.add_all(forward_bars)
            session.commit()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        self._temp_dir.cleanup()

    def _count_results(self) -> int:
        with self.db.get_session() as session:
            return session.query(BacktestResult).count()

    def test_force_semantics(self) -> None:
        service = BacktestService(self.db)

        stats1 = service.run_backtest(code="600519", force=False, eval_window_days=3, min_age_days=0, limit=10)
        self.assertEqual(stats1["saved"], 1)
        self.assertEqual(self._count_results(), 1)

        # Non-force should be idempotent
        stats2 = service.run_backtest(code="600519", force=False, eval_window_days=3, min_age_days=0, limit=10)
        self.assertEqual(stats2["saved"], 0)
        self.assertEqual(self._count_results(), 1)

        # Force should replace existing result without unique constraint errors
        stats3 = service.run_backtest(code="600519", force=True, eval_window_days=3, min_age_days=0, limit=10)
        self.assertEqual(stats3["saved"], 1)
        self.assertEqual(self._count_results(), 1)

    def _run_and_get_result(self) -> BacktestResult:
        """Helper: run backtest and return the single BacktestResult row."""
        service = BacktestService(self.db)
        service.run_backtest(code="600519", force=False, eval_window_days=3, min_age_days=0, limit=10)
        with self.db.get_session() as session:
            return session.query(BacktestResult).one()

    def test_result_fields_correct(self) -> None:
        """Verify BacktestResult row contains correct evaluation values."""
        result = self._run_and_get_result()

        self.assertEqual(result.eval_status, "completed")
        self.assertEqual(result.code, "600519")
        self.assertEqual(result.analysis_date, date(2024, 1, 1))
        self.assertEqual(result.operation_advice, "买入")
        self.assertEqual(result.position_recommendation, "long")
        self.assertEqual(result.direction_expected, "up")

        # Prices
        self.assertAlmostEqual(result.start_price, 100.0)
        self.assertAlmostEqual(result.end_close, 107.0)
        self.assertAlmostEqual(result.stock_return_pct, 7.0)

        # Direction & outcome
        self.assertEqual(result.outcome, "win")
        self.assertTrue(result.direction_correct)

        # Target hits -- day2 high=111 >= take_profit=110
        self.assertTrue(result.hit_take_profit)
        self.assertFalse(result.hit_stop_loss)
        self.assertEqual(result.first_hit, "take_profit")
        self.assertEqual(result.first_hit_trading_days, 1)
        self.assertEqual(result.first_hit_date, date(2024, 1, 2))

        # Simulated execution
        self.assertAlmostEqual(result.simulated_entry_price, 100.0)
        self.assertAlmostEqual(result.simulated_exit_price, 110.0)
        self.assertEqual(result.simulated_exit_reason, "take_profit")
        self.assertAlmostEqual(result.simulated_return_pct, 10.0)

    def test_summaries_created_after_run(self) -> None:
        """Verify both overall and per-stock BacktestSummary rows are created."""
        service = BacktestService(self.db)
        service.run_backtest(code="600519", force=False, eval_window_days=3, min_age_days=0, limit=10)

        with self.db.get_session() as session:
            # Overall summary uses sentinel code
            overall = session.query(BacktestSummary).filter(
                BacktestSummary.scope == "overall",
                BacktestSummary.code == OVERALL_SENTINEL_CODE,
            ).first()
            self.assertIsNotNone(overall)
            self.assertEqual(overall.total_evaluations, 1)
            self.assertEqual(overall.completed_count, 1)
            self.assertEqual(overall.win_count, 1)
            self.assertEqual(overall.loss_count, 0)
            self.assertAlmostEqual(overall.win_rate_pct, 100.0)

            # Stock-level summary
            stock = session.query(BacktestSummary).filter(
                BacktestSummary.scope == "stock",
                BacktestSummary.code == "600519",
            ).first()
            self.assertIsNotNone(stock)
            self.assertEqual(stock.total_evaluations, 1)
            self.assertEqual(stock.completed_count, 1)
            self.assertEqual(stock.win_count, 1)

    def test_get_summary_overall_returns_sentinel_as_none(self) -> None:
        """Verify get_summary translates __overall__ sentinel back to None."""
        service = BacktestService(self.db)
        service.run_backtest(code="600519", force=False, eval_window_days=3, min_age_days=0, limit=10)

        summary = service.get_summary(scope="overall", code=None)
        self.assertIsNotNone(summary)
        self.assertIsNone(summary["code"])
        self.assertEqual(summary["scope"], "overall")
        self.assertEqual(summary["win_count"], 1)

    def test_agent_learning_summary_helpers_keep_skill_rollups_neutral_until_supported(self) -> None:
        service = BacktestService(self.db)
        service.run_backtest(code="600519", force=False, eval_window_days=3, min_age_days=0, limit=10)

        global_summary = service.get_global_summary(eval_window_days=3)
        stock_summary = service.get_stock_summary("600519", eval_window_days=3)
        skill_summary = service.get_skill_summary("bull_trend", eval_window_days=3)
        strategy_summary = service.get_strategy_summary("bull_trend", eval_window_days=3)

        self.assertIsNotNone(global_summary)
        self.assertEqual(global_summary["total_evaluations"], 1)
        self.assertAlmostEqual(global_summary["win_rate"], 1.0)
        self.assertAlmostEqual(global_summary["direction_accuracy"], 1.0)
        self.assertAlmostEqual(global_summary["avg_return"], 0.10)

        self.assertIsNotNone(stock_summary)
        self.assertEqual(stock_summary["code"], "600519")
        self.assertAlmostEqual(stock_summary["win_rate"], 1.0)

        self.assertIsNone(skill_summary)
        self.assertIsNone(strategy_summary)

    def test_get_recent_evaluations(self) -> None:
        """Verify get_recent_evaluations returns correct paginated results."""
        service = BacktestService(self.db)
        service.run_backtest(code="600519", force=False, eval_window_days=3, min_age_days=0, limit=10)

        data = service.get_recent_evaluations(code="600519", limit=10, page=1)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["limit"], 10)
        self.assertEqual(len(data["items"]), 1)

        item = data["items"][0]
        self.assertEqual(item["code"], "600519")
        self.assertEqual(item["outcome"], "win")
        self.assertEqual(item["direction_expected"], "up")
        self.assertTrue(item["direction_correct"])

    def test_get_recent_evaluations_supports_tracking_fields_and_analysis_date_filters(self) -> None:
        self._seed_analysis(
            query_id="q2",
            analysis_date=date(2024, 1, 10),
            created_at=datetime(2024, 1, 10, 0, 0, 0),
            operation_advice="买入",
            trend_prediction="看多",
            start_close=100.0,
            forward_bars=[
                StockDaily(code="600519", date=date(2024, 1, 11), high=101.0, low=95.0, close=96.0),
            ],
        )

        service = BacktestService(self.db)
        service.run_backtest(code="600519", force=False, eval_window_days=1, min_age_days=0, limit=20)

        data = service.get_recent_evaluations(
            code="600519",
            eval_window_days=1,
            limit=10,
            page=1,
            analysis_date_from=date(2024, 1, 10),
            analysis_date_to=date(2024, 1, 10),
        )
        self.assertEqual(data["total"], 1)
        item = data["items"][0]
        self.assertEqual(item["stock_name"], "贵州茅台")
        self.assertEqual(item["trend_prediction"], "看多")
        self.assertEqual(item["actual_movement"], "down")
        self.assertAlmostEqual(item["actual_return_pct"], -4.0)
        self.assertFalse(item["direction_correct"])

    def test_get_summary_supports_analysis_date_range(self) -> None:
        self._seed_analysis(
            query_id="q2",
            analysis_date=date(2024, 1, 10),
            created_at=datetime(2024, 1, 10, 0, 0, 0),
            operation_advice="买入",
            trend_prediction="看多",
            start_close=100.0,
            forward_bars=[
                StockDaily(code="600519", date=date(2024, 1, 11), high=101.0, low=95.0, close=96.0),
            ],
        )

        service = BacktestService(self.db)
        service.run_backtest(code="600519", force=False, eval_window_days=1, min_age_days=0, limit=20)

        summary = service.get_summary(
            scope="stock",
            code="600519",
            eval_window_days=1,
            analysis_date_from=date(2024, 1, 10),
            analysis_date_to=date(2024, 1, 10),
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary["total_evaluations"], 1)
        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["win_count"], 0)
        self.assertEqual(summary["loss_count"], 1)
        self.assertAlmostEqual(summary["direction_accuracy_pct"], 0.0)

    def test_get_summary_date_range_filters_to_single_window_and_engine(self) -> None:
        service = BacktestService(self.db)
        service.run_backtest(code="600519", force=False, eval_window_days=3, min_age_days=0, limit=10)

        with self.db.get_session() as session:
            base_result = session.query(BacktestResult).filter(
                BacktestResult.code == "600519",
                BacktestResult.eval_window_days == 3,
                BacktestResult.engine_version == "v1",
            ).one()
            session.add_all([
                BacktestResult(
                    analysis_history_id=base_result.analysis_history_id,
                    code=base_result.code,
                    analysis_date=base_result.analysis_date,
                    eval_window_days=1,
                    engine_version="v1",
                    eval_status="completed",
                    evaluated_at=datetime(2024, 1, 5, 0, 0, 0),
                    operation_advice="买入",
                    position_recommendation="long",
                    start_price=100.0,
                    end_close=96.0,
                    stock_return_pct=-4.0,
                    direction_expected="up",
                    direction_correct=False,
                    outcome="loss",
                    simulated_return_pct=-4.0,
                ),
                BacktestResult(
                    analysis_history_id=base_result.analysis_history_id,
                    code=base_result.code,
                    analysis_date=base_result.analysis_date,
                    eval_window_days=3,
                    engine_version="v2",
                    eval_status="completed",
                    evaluated_at=datetime(2024, 1, 6, 0, 0, 0),
                    operation_advice="买入",
                    position_recommendation="long",
                    start_price=100.0,
                    end_close=96.0,
                    stock_return_pct=-4.0,
                    direction_expected="up",
                    direction_correct=False,
                    outcome="loss",
                    simulated_return_pct=-4.0,
                ),
            ])
            session.commit()

        rows = service.repo.list_results(
            code="600519",
            eval_window_days=3,
            engine_version="v1",
            analysis_date_from=date(2024, 1, 1),
            analysis_date_to=date(2024, 1, 1),
        )
        self.assertEqual(len(rows), 1)

        evaluations = service.get_recent_evaluations(
            code="600519",
            eval_window_days=3,
            limit=10,
            page=1,
            analysis_date_from=date(2024, 1, 1),
            analysis_date_to=date(2024, 1, 1),
        )
        self.assertEqual(evaluations["total"], 1)
        self.assertEqual(len(evaluations["items"]), 1)
        self.assertEqual(evaluations["items"][0]["engine_version"], "v1")

        # Without explicit eval_window_days, summary infers the smallest
        # window from matched rows (window=1 in this dataset) instead of
        # falling back to the config default.
        summary_inferred = service.get_summary(
            scope="stock",
            code="600519",
            analysis_date_from=date(2024, 1, 1),
            analysis_date_to=date(2024, 1, 1),
        )
        self.assertIsNotNone(summary_inferred)
        assert summary_inferred is not None
        self.assertEqual(summary_inferred["eval_window_days"], 1)
        self.assertEqual(summary_inferred["engine_version"], "v1")
        self.assertEqual(summary_inferred["total_evaluations"], 1)
        self.assertEqual(summary_inferred["completed_count"], 1)
        self.assertEqual(summary_inferred["win_count"], 0)
        self.assertEqual(summary_inferred["loss_count"], 1)
        self.assertAlmostEqual(summary_inferred["direction_accuracy_pct"], 0.0)

        # With explicit eval_window_days=3, summary filters to that window only.
        summary_explicit = service.get_summary(
            scope="stock",
            code="600519",
            eval_window_days=3,
            analysis_date_from=date(2024, 1, 1),
            analysis_date_to=date(2024, 1, 1),
        )
        self.assertIsNotNone(summary_explicit)
        assert summary_explicit is not None
        self.assertEqual(summary_explicit["eval_window_days"], 3)
        self.assertEqual(summary_explicit["engine_version"], "v1")
        self.assertEqual(summary_explicit["total_evaluations"], 1)
        self.assertEqual(summary_explicit["completed_count"], 1)
        self.assertEqual(summary_explicit["win_count"], 1)
        self.assertEqual(summary_explicit["loss_count"], 0)
        self.assertAlmostEqual(summary_explicit["direction_accuracy_pct"], 100.0)

    def test_get_summary_date_range_rejects_excessive_row_counts(self) -> None:
        service = BacktestService(self.db)
        service.run_backtest(code="600519", force=False, eval_window_days=3, min_age_days=0, limit=10)

        with patch.object(BacktestService, "MAX_DYNAMIC_SUMMARY_ROWS", 0):
            with self.assertRaisesRegex(ValueError, "Date-filtered summary matches too many rows"):
                service.get_summary(
                    scope="stock",
                    code="600519",
                    analysis_date_from=date(2024, 1, 1),
                    analysis_date_to=date(2024, 1, 1),
                )

    def test_multi_stock_summaries(self) -> None:
        """Verify separate summaries for multiple stocks + correct overall aggregate."""
        old_created_at = datetime(2024, 1, 1, 0, 0, 0)

        with self.db.get_session() as session:
            # Second stock with sell advice -- price drops (win for cash/down)
            session.add(
                AnalysisHistory(
                    query_id="q2",
                    code="000001",
                    name="平安银行",
                    report_type="simple",
                    sentiment_score=30,
                    operation_advice="卖出",
                    trend_prediction="看空",
                    analysis_summary="test2",
                    stop_loss=None,
                    take_profit=None,
                    created_at=old_created_at,
                    context_snapshot='{"enhanced_context": {"date": "2024-01-01"}}',
                )
            )
            session.add(
                StockDaily(code="000001", date=date(2024, 1, 1), open=10.0, high=10.2, low=9.8, close=10.0)
            )
            session.add_all([
                StockDaily(code="000001", date=date(2024, 1, 2), high=10.0, low=9.5, close=9.6),
                StockDaily(code="000001", date=date(2024, 1, 3), high=9.7, low=9.3, close=9.4),
                StockDaily(code="000001", date=date(2024, 1, 4), high=9.5, low=9.0, close=9.1),
            ])
            session.commit()

        service = BacktestService(self.db)
        stats = service.run_backtest(code=None, force=False, eval_window_days=3, min_age_days=0, limit=10)
        self.assertEqual(stats["saved"], 2)
        self.assertEqual(stats["completed"], 2)

        with self.db.get_session() as session:
            # Each stock has its own summary
            s1 = session.query(BacktestSummary).filter(
                BacktestSummary.scope == "stock", BacktestSummary.code == "600519"
            ).first()
            s2 = session.query(BacktestSummary).filter(
                BacktestSummary.scope == "stock", BacktestSummary.code == "000001"
            ).first()
            self.assertIsNotNone(s1)
            self.assertIsNotNone(s2)
            self.assertEqual(s1.win_count, 1)
            self.assertEqual(s2.win_count, 1)

            # Overall aggregates both
            overall = session.query(BacktestSummary).filter(
                BacktestSummary.scope == "overall",
                BacktestSummary.code == OVERALL_SENTINEL_CODE,
            ).first()
            self.assertIsNotNone(overall)
            self.assertEqual(overall.total_evaluations, 2)
            self.assertEqual(overall.completed_count, 2)
            self.assertEqual(overall.win_count, 2)


if __name__ == "__main__":
    unittest.main()
