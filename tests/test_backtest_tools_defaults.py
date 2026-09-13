"""Regression tests for Agent/runner evaluation-window consistency."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.agent.tools import backtest_tools as tools


def test_tools_default_to_configured_window():
    svc = MagicMock()
    svc.get_summary.return_value = None
    svc.get_skill_summary.return_value = None
    svc.get_recent_evaluations.return_value = {"items": [], "total": 0}
    with patch.object(tools, "get_config", return_value=SimpleNamespace(backtest_eval_window_days=10)), patch.object(tools, "_get_backtest_service", return_value=svc):
        overall = tools._handle_get_overall_backtest_summary()
        skill = tools._handle_get_skill_backtest_summary("bull_trend")
        stock = tools._handle_get_stock_backtest_summary("588000")
    assert overall["eval_window_days"] == skill["eval_window_days"] == stock["eval_window_days"] == 10
    assert skill["supported"] is True
    assert skill["status"] == "no_data"
    svc.get_skill_summary.assert_called_once_with("bull_trend", eval_window_days=10)
    svc.get_recent_evaluations.assert_called_once_with(code="588000", eval_window_days=10, limit=10)
    svc.run_backtest.assert_not_called()
