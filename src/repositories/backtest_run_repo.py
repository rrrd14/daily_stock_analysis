"""Persistence for immutable backtest evidence, separate from mutable rollups."""

from src.time_utils import beijing_now_naive
import hashlib
import json
import math
from datetime import date, datetime

from sqlalchemy import select

from src.storage import BacktestRun


def json_value(value):
    """Normalize whitelisted evidence to portable, finite JSON values."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


class BacktestRunRepository:
    def __init__(self, db):
        self.db = db

    def create(self, run_id, payload):
        with self.db.get_session() as session:
            session.add(BacktestRun(run_id=run_id, payload=json.dumps(json_value(payload))))
            session.commit()

    def finish(self, run_id, status, payload):
        encoded = json.dumps(json_value(payload), sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False)
        with self.db.get_session() as session:
            row = session.get(BacktestRun, run_id)
            if row is None or row.status != "running":
                raise ValueError("Run is missing or already finalized")
            row.status = status
            row.finished_at = beijing_now_naive()
            row.payload = encoded
            row.sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            session.commit()

    @staticmethod
    def _serialize(row, detail=False):
        result = {"run_id": row.run_id, "status": row.status,
                  "created_at": row.created_at.isoformat(),
                  "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                  "sha256": row.sha256}
        if detail:
            result["evidence"] = json.loads(row.payload)
        return result

    def get(self, run_id):
        with self.db.get_session() as session:
            row = session.get(BacktestRun, run_id)
            return self._serialize(row, detail=True) if row else None

    def recent(self):
        with self.db.get_session() as session:
            rows = session.scalars(select(BacktestRun).order_by(
                BacktestRun.created_at.desc(), BacktestRun.run_id.desc()).limit(20)).all()
            return [self._serialize(row) for row in rows]
