# -*- coding: utf-8 -*-
"""
Backtest tools — read-only tools exposing backtest summaries to the agent.

Tools:
- get_skill_backtest_summary: skill-scoped stats when available, otherwise an explicit no-data response
- get_strategy_backtest_summary: legacy alias of the overall summary tool
- get_stock_backtest_summary: backtest results for a specific stock
"""

import logging
from typing import Optional

from src.config import get_config

from src.agent.tools.registry import ToolParameter, ToolDefinition

logger = logging.getLogger(__name__)

_backtest_service = None


def _get_backtest_service():
    """Lazy import + singleton to avoid circular deps and repeated instantiation."""
    global _backtest_service
    if _backtest_service is None:
        from src.services.backtest_service import BacktestService
        _backtest_service = BacktestService()
    return _backtest_service


# ============================================================
# get_skill_backtest_summary / get_strategy_backtest_summary
# ============================================================

def _serialize_overall_backtest_summary(summary: dict, eval_window_days: int) -> dict:
    """Return the public overall-summary payload exposed to the agent."""
    return {
        "scope": summary.get("scope", "overall"),
        "eval_window_days": summary.get("eval_window_days", eval_window_days),
        "total_evaluations": summary.get("total_evaluations", 0),
        "completed_count": summary.get("completed_count", 0),
        "win_rate_pct": summary.get("win_rate_pct"),
        "direction_accuracy_pct": summary.get("direction_accuracy_pct"),
        "avg_stock_return_pct": summary.get("avg_stock_return_pct"),
        "avg_simulated_return_pct": summary.get("avg_simulated_return_pct"),
        "stop_loss_trigger_rate": summary.get("stop_loss_trigger_rate"),
        "take_profit_trigger_rate": summary.get("take_profit_trigger_rate"),
        "advice_breakdown": summary.get("advice_breakdown"),
        "computed_at": summary.get("computed_at"),
    }


def _handle_get_overall_backtest_summary(eval_window_days: Optional[int] = None) -> dict:
    """Get the overall backtest summary for the full analysis corpus."""
    try:
        eval_window_days = eval_window_days if eval_window_days is not None else get_config().backtest_eval_window_days
        svc = _get_backtest_service()
        summary = svc.get_summary(scope="overall", code=None, eval_window_days=eval_window_days)
        if summary is None:
            return {"status": "no_data", "eval_window_days": eval_window_days,
                    "info": "No summary for this evaluation window. Run backtests and check eligible analysis history; this does not prove backtests never ran."}
        return _serialize_overall_backtest_summary(summary, eval_window_days)
    except Exception:
        logger.warning("[backtest_tools] get_overall_backtest_summary error", exc_info=True)
        return {"error": "Failed to retrieve backtest summary."}


def _handle_get_skill_backtest_summary(skill_id: str = "", eval_window_days: Optional[int] = None) -> dict:
    """Get a skill-scoped backtest summary when real per-skill stats exist."""
    if not skill_id:
        return {
            "supported": False,
            "error": "skill_id is required. Use get_strategy_backtest_summary for overall metrics.",
        }

    try:
        eval_window_days = eval_window_days if eval_window_days is not None else get_config().backtest_eval_window_days
        svc = _get_backtest_service()
        summary = svc.get_skill_summary(skill_id, eval_window_days=eval_window_days)
        if summary is None:
            return {
                "skill_id": skill_id,
                "supported": True,
                "status": "no_data",
                "eval_window_days": eval_window_days,
                "info": "No evaluated analyses explicitly attributed to this skill. Untagged and combined-skill analyses are excluded.",
            }
        return {
            "scope": "skill",
            "skill_id": skill_id,
            "supported": True,
            "eval_window_days": summary.get("eval_window_days", eval_window_days),
            "total_evaluations": summary.get("total_evaluations", 0),
            "completed_count": summary.get("completed_count", 0),
            "win_rate": summary.get("win_rate"),
            "direction_accuracy": summary.get("direction_accuracy"),
            "avg_return": summary.get("avg_return"),
            "win_rate_pct": summary.get("win_rate_pct"),
            "direction_accuracy_pct": summary.get("direction_accuracy_pct"),
            "avg_stock_return_pct": summary.get("avg_stock_return_pct"),
            "avg_simulated_return_pct": summary.get("avg_simulated_return_pct"),
            "computed_at": summary.get("computed_at"),
        }
    except Exception:
        logger.warning("[backtest_tools] get_skill_backtest_summary error", exc_info=True)
        return {"error": "Failed to retrieve backtest summary."}


get_skill_backtest_summary_tool = ToolDefinition(
    name="get_skill_backtest_summary",
    description=(
        "Inspect backtest data for a specific skill when skill-scoped stats exist. "
        "Provide skill_id for a targeted lookup; use get_strategy_backtest_summary for overall metrics. "
        "When skill-scoped rollups are unavailable, returns an informational response instead of fabricating metrics."
    ),
    parameters=[
        ToolParameter(
            name="skill_id",
            type="string",
            description="Skill identifier, e.g. 'bull_trend'.",
            required=True,
        ),
        ToolParameter(
            name="eval_window_days",
            type="integer",
            description="Evaluation window in trading days; omitted uses BACKTEST_EVAL_WINDOW_DAYS.",
            required=False,
        ),
    ],
    handler=_handle_get_skill_backtest_summary,
    category="data",
)


get_strategy_backtest_summary_tool = ToolDefinition(
    name="get_strategy_backtest_summary",
    description=(
        "Legacy alias returning the overall backtest performance summary without triggering new backtests."
    ),
    parameters=[
        ToolParameter(
            name="eval_window_days",
            type="integer",
            description="Evaluation window in trading days; omitted uses BACKTEST_EVAL_WINDOW_DAYS.",
            required=False,
        ),
    ],
    handler=_handle_get_overall_backtest_summary,
    category="data",
)


# ============================================================
# get_stock_backtest_summary
# ============================================================

def _handle_get_stock_backtest_summary(stock_code: str, eval_window_days: Optional[int] = None, limit: int = 10) -> dict:
    """Get backtest results for a specific stock.

    Returns the summary plus recent evaluation items.
    """
    try:
        eval_window_days = eval_window_days if eval_window_days is not None else get_config().backtest_eval_window_days
        svc = _get_backtest_service()
        result = {}

        # Per-stock summary
        summary = svc.get_summary(scope="stock", code=stock_code, eval_window_days=eval_window_days)
        if summary:
            result["summary"] = {
                "code": summary.get("code", stock_code),
                "total_evaluations": summary.get("total_evaluations", 0),
                "completed_count": summary.get("completed_count", 0),
                "win_rate_pct": summary.get("win_rate_pct"),
                "direction_accuracy_pct": summary.get("direction_accuracy_pct"),
                "avg_stock_return_pct": summary.get("avg_stock_return_pct"),
                "avg_simulated_return_pct": summary.get("avg_simulated_return_pct"),
                "computed_at": summary.get("computed_at"),
            }
        else:
            result["summary"] = None

        # Recent evaluations
        evals = svc.get_recent_evaluations(code=stock_code, eval_window_days=eval_window_days, limit=limit)
        items = evals.get("items", [])
        # Slim down items to essential fields
        result["recent_evaluations"] = [
            {
                "analysis_date": item.get("analysis_date"),
                "operation_advice": item.get("operation_advice"),
                "stock_return_pct": item.get("stock_return_pct"),
                "direction_correct": item.get("direction_correct"),
                "outcome": item.get("outcome"),
                "simulated_return_pct": item.get("simulated_return_pct"),
                "hit_stop_loss": item.get("hit_stop_loss"),
                "hit_take_profit": item.get("hit_take_profit"),
            }
            for item in items
        ]
        result["total"] = evals.get("total", 0)

        if result["summary"] is None and not result["recent_evaluations"]:
            return {"status": "no_data", "eval_window_days": eval_window_days,
                    "info": f"No backtest data for {stock_code} in this evaluation window. Check eligible analysis history and run backtests."}

        return result
    except Exception:
        logger.warning("[backtest_tools] get_stock_backtest_summary error", exc_info=True)
        return {"error": "Failed to retrieve backtest data."}


get_stock_backtest_summary_tool = ToolDefinition(
    name="get_stock_backtest_summary",
    description=(
        "Get backtest performance data for a specific stock: per-stock summary (win rate, "
        "accuracy, avg return) plus recent evaluation records. Read-only, does not trigger new backtests."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519' (A-share), 'AAPL' (US), 'hk00700' (HK)",
        ),
        ToolParameter(
            name="eval_window_days",
            type="integer",
            description="Evaluation window in trading days; omitted uses BACKTEST_EVAL_WINDOW_DAYS.",
            required=False,
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Max number of recent evaluation records to return (default: 10)",
            required=False,
            default=10,
        ),
    ],
    handler=_handle_get_stock_backtest_summary,
    category="data",
)


# ============================================================
# Exported tool list
# ============================================================

def _handle_get_backtest_run(run_id: str = ""):
    service = _get_backtest_service()
    if not run_id:
        return {"runs": service.get_runs(), "info": "Read-only historical executions; select a run_id to inspect."}
    record = service.get_run(run_id)
    if record is None:
        return {"status": "not_found", "info": "No execution evidence exists for this run ID."}
    evidence = record.pop("evidence")
    return {**record, "kind": evidence["kind"], "parameters": evidence["parameters"],
            "counts": evidence.get("counts"), "limitations": evidence["limitations"],
            "url": f"/backtest?run={record['run_id']}"}


get_backtest_run_tool = ToolDefinition(
    name="get_backtest_run",
    description=("Read program execution evidence. Omit run_id to list recent runs. "
                 "This never runs a backtest. Cite returned url as a Markdown link. "
                 "AI report evaluations are not portfolio returns. Disclose status and limitations; "
                 "a run reference does not verify other claims in your explanation."),
    parameters=[ToolParameter(name="run_id", type="string", required=False,
                              description="Existing run ID; omit to list recent runs.")],
    handler=_handle_get_backtest_run,
    category="data",
)


def _handle_get_market_snapshot(snapshot_id: str = "", instrument: str = "", limit: int = 10):
    """只读查询冻结行情快照；无 snapshot_id 时按标的列出最近快照。"""
    try:
        from src.repositories.market_snapshot_repo import MarketDataSnapshotRepository
        from src.storage import DatabaseManager

        repo = MarketDataSnapshotRepository(DatabaseManager.get_instance())
        if not snapshot_id:
            snapshots = repo.list(instrument=instrument or None,
                                  limit=max(1, min(int(limit or 10), 50)))
            return {
                "snapshots": snapshots,
                "info": ("Read-only frozen market-data snapshots. input_eligibility=false means "
                         "the snapshot must not be used for default strategy return computation. "
                         "No Web page exists for snapshots yet."),
            }
        record = repo.get(snapshot_id, detail=False)
        if record is None:
            return {"status": "not_found", "info": "No frozen snapshot exists for this ID."}
        quality = record.get("quality") or {}
        return {
            **record,
            "missing": quality.get("missing"),
            "info": ("Frozen input identity. data_quality_status=unknown/partial must not be "
                     "reported as verified; a snapshot reference does not prove the data is "
                     "point-in-time accurate."),
            "api_path": f"/api/v1/backtest/snapshots/{record['snapshot_id']}",
        }
    except Exception:
        logger.warning("[backtest_tools] get_market_snapshot error", exc_info=True)
        return {"error": "Failed to retrieve market data snapshot."}


get_market_snapshot_tool = ToolDefinition(
    name="get_market_snapshot",
    description=("Read a frozen market-data snapshot (input identity for research/backtests). "
                 "Omit snapshot_id to list recent snapshots for an instrument. Read-only: it "
                 "never fetches new data and never creates a snapshot. Disclose "
                 "data_quality_status and input_eligibility; unknown/partial snapshots are "
                 "research-only."),
    parameters=[
        ToolParameter(name="snapshot_id", type="string", required=False,
                      description="Existing frozen snapshot ID; omit to list recent snapshots."),
        ToolParameter(name="instrument", type="string", required=False,
                      description="Instrument code used when listing, e.g. '588000' or 'AAPL'."),
        ToolParameter(name="limit", type="integer", required=False, default=10,
                      description="Max snapshots to list (default: 10, max: 50)."),
    ],
    handler=_handle_get_market_snapshot,
    category="data",
)



ALL_BACKTEST_TOOLS = [
    get_backtest_run_tool,
    get_market_snapshot_tool,
    get_skill_backtest_summary_tool,
    get_strategy_backtest_summary_tool,
    get_stock_backtest_summary_tool,
]
