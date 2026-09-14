"""不可变行情冻结快照仓储（WP4 第一阶段）。

设计边界：

- 只提供 ``create`` / ``get`` / ``list``，**没有 update / delete**：快照创建后不得
  原地修改或自动删除，运行记录通过 ``snapshot_id`` 引用它以便重放。
- ``snapshot_id`` 由「口径（复权/币种/单位/来源）+ 区间 + 内容哈希」共同决定，
  因此同源同区间但复权口径不同会得到**不同身份**。
- ``payload_hash`` 使用稳定序列化（键排序、固定分隔符、拒绝 NaN/Infinity）。
- 质量评估对未知口径一律判为不可用于默认策略收益计算，不把 unknown 提升为 verified。
- 覆盖判定分两层：首尾日期（必做）+ 交易日历 session 数（日历可用时）；日历不可用时
  ``quality["coverage"]["basis"]`` 会如实写 ``endpoints_only``，不伪装成已核对完整。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select

from src.core.trading_calendar import count_sessions
from src.repositories.backtest_run_repo import json_value
from src.storage import MarketDataSnapshot

SCHEMA_VERSION = "1"
UNKNOWN = "unknown"
DATA_QUALITY_STATUSES = ("verified", "partial", "unknown")

# 体积预算：单份快照 payload 超过上限直接拒绝写入；总量预算用于运维判断是否需要导出。
SNAPSHOT_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
SNAPSHOT_TOTAL_BUDGET_BYTES = 200 * 1024 * 1024

# 参与内容哈希的列白名单（避免把内部/瞬时字段写进身份）
PAYLOAD_COLUMNS = (
    "date", "open", "high", "low", "close", "volume", "amount", "pct_chg",
)

# 覆盖判定容忍度：请求区间端点与真实数据首尾相差在 10 个自然日内仍视为覆盖
_COVERAGE_TOLERANCE_DAYS = 10

# 会话数赤字容忍度：exchange-calendars 对个别地区节假日历史上并不精确，
# 只有缺口大到不像日历噪声时才判定覆盖不足，避免把正常休市算成缺失数据。
SESSION_DEFICIT_TOLERANCE_RATIO = 0.10


def stable_json(payload: Any) -> str:
    """稳定序列化：键排序、固定分隔符、拒绝 NaN/Infinity。"""
    return json.dumps(
        json_value(payload), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    )


def _is_known(value: Optional[str]) -> bool:
    return bool(value) and str(value).strip().lower() != UNKNOWN


def normalize_bars(bars: Iterable[Any]) -> List[Dict[str, Any]]:
    """把行情行归一为白名单列 + ISO 日期，供稳定哈希使用。

    同时支持 pandas DataFrame 与 dict 行；不做去重/补值，缺失就是缺失。
    """
    rows: List[Dict[str, Any]] = []
    for bar in bars:
        if hasattr(bar, "to_dict") and not isinstance(bar, dict):
            record = dict(bar.to_dict())
        elif isinstance(bar, dict):
            record = dict(bar)
        else:
            continue
        normalized: Dict[str, Any] = {}
        for column in PAYLOAD_COLUMNS:
            if column in record:
                normalized[column] = record[column]
        rows.append(normalized)
    return rows


def compute_coverage(
    actual_start: Optional[date],
    actual_end: Optional[date],
    requested_start: Optional[date],
    requested_end: Optional[date],
    *,
    rows: int = 0,
    required_rows: Optional[int] = None,
    expected_sessions: Optional[int] = None,
) -> bool:
    """基于真实数据首尾判断区间覆盖，绝不用 0 或前值补齐来凑「完整」。

    请求区间未明确、或真实数据首尾明显短于请求区间时一律返回 False，
    这样「缺失一段」的三年请求不会被标成完整三年。

    额外一层会话数校验：当日历可用（``expected_sessions`` 非 None）时，
    真实行数显著少于区间应有 session 数也判为覆盖不足，用于抓「首尾齐全但中间
    缺一大段」这种情况；容差见 ``SESSION_DEFICIT_TOLERANCE_RATIO``。
    """
    if requested_start is None or requested_end is None:
        return False
    if actual_start is None or actual_end is None:
        return False
    if required_rows is not None and rows < required_rows:
        return False
    if expected_sessions is not None and expected_sessions > 0:
        allowed_deficit = int(expected_sessions * SESSION_DEFICIT_TOLERANCE_RATIO)
        if rows < expected_sessions - allowed_deficit:
            return False
    start_ok = actual_start <= (requested_start + timedelta(days=_COVERAGE_TOLERANCE_DAYS))
    end_ok = actual_end >= (requested_end - timedelta(days=_COVERAGE_TOLERANCE_DAYS))
    return bool(start_ok and end_ok)


def describe_coverage(
    *,
    requested_start: Optional[date],
    requested_end: Optional[date],
    rows: int,
    expected_sessions: Optional[int],
) -> Dict[str, Any]:
    """覆盖判定的依据明细（供审计；不参与快照身份哈希）。

    ``basis`` 明确区分「真的按交易日历核对过」与「只做了首尾日期判定」，
    避免读者把缺少日历的情况误认为已核对完整。
    """
    deficit: Optional[int] = None
    if expected_sessions is not None:
        deficit = max(int(expected_sessions) - int(rows), 0)
    return {
        "basis": "trading_calendar" if expected_sessions is not None else "endpoints_only",
        "expected_sessions": expected_sessions,
        "rows": int(rows),
        "deficit": deficit,
        "requested_start": requested_start,
        "requested_end": requested_end,
    }


def assess_data_quality(
    *,
    source: Optional[str],
    price_adjustment: Optional[str],
    currency: Optional[str],
    volume_unit: Optional[str],
    coverage_complete: Optional[bool],
) -> Dict[str, Any]:
    """按「来源 / 复权 / 币种 / 单位 / 覆盖」判定质量与策略资格。

    - 全部已知且覆盖完整 → ``verified``（可用于默认策略收益计算）
    - 来源或复权口径未知 → ``unknown``（不可用于任何收益口径计算）
    - 仅币种/单位未知或覆盖不足 → ``partial``（可研究，需显式标注）
    """
    missing: List[str] = []
    if not _is_known(source):
        missing.append("source")
    if not _is_known(price_adjustment):
        missing.append("price_adjustment")
    if not _is_known(currency):
        missing.append("currency")
    if not _is_known(volume_unit):
        missing.append("volume_unit")
    if coverage_complete is not True:
        missing.append("coverage")

    if not missing:
        status = "verified"
    elif "source" in missing or "price_adjustment" in missing:
        status = "unknown"
    else:
        status = "partial"

    return {
        "data_quality_status": status,
        "input_eligibility": status == "verified",
        "missing": missing,
    }


def build_snapshot_id(
    *,
    instrument: str,
    market: Optional[str],
    interval: str,
    source: Optional[str],
    price_adjustment: Optional[str],
    currency: Optional[str],
    volume_unit: Optional[str],
    requested_start: Optional[date],
    requested_end: Optional[date],
    resolved_start: Optional[date],
    resolved_end: Optional[date],
    rows: int,
    payload_hash: str,
) -> str:
    """由口径 + 区间 + 内容哈希派生的稳定身份（32 位十六进制）。"""
    identity = {
        "schema_version": SCHEMA_VERSION,
        "instrument": instrument,
        "market": market,
        "interval": interval,
        "source": source,
        "price_adjustment": price_adjustment,
        "currency": currency,
        "volume_unit": volume_unit,
        "requested_start": requested_start,
        "requested_end": requested_end,
        "resolved_start": resolved_start,
        "resolved_end": resolved_end,
        "rows": rows,
        "payload_hash": payload_hash,
    }
    return hashlib.sha256(stable_json(identity).encode("utf-8")).hexdigest()[:32]


@dataclass
class SnapshotRequest:
    """构建快照所需的输入（元信息 + 已归一化的行情行）。"""

    instrument: str
    bars: List[Dict[str, Any]] = field(default_factory=list)
    market: Optional[str] = None
    interval: str = "daily"
    source: Optional[str] = None
    price_adjustment: Optional[str] = None
    currency: Optional[str] = None
    volume_unit: Optional[str] = None
    requested_start: Optional[date] = None
    requested_end: Optional[date] = None
    required_rows: Optional[int] = None


def ensure_input_eligible(snapshot: Dict[str, Any]) -> None:
    """默认策略收益计算的前置校验：非 verified 快照必须被拒绝。"""
    if not snapshot.get("input_eligibility"):
        quality = snapshot.get("quality") or {}
        raise ValueError(
            "Snapshot is not eligible for default strategy evaluation: "
            f"snapshot_id={snapshot.get('snapshot_id')}, "
            f"status={snapshot.get('data_quality_status')}, "
            f"missing={quality.get('missing')}"
        )


class MarketDataSnapshotRepository:
    """只增不改的快照仓储（无 update / delete）。"""

    def __init__(self, db, *, max_payload_bytes: int = SNAPSHOT_MAX_PAYLOAD_BYTES):
        self.db = db
        self.max_payload_bytes = int(max_payload_bytes)

    @staticmethod
    def _resolve_range(bars: List[Dict[str, Any]]):
        dates: List[date] = []
        for bar in bars:
            value = bar.get("date")
            if isinstance(value, datetime):
                dates.append(value.date())
            elif isinstance(value, date):
                dates.append(value)
            else:
                try:
                    dates.append(datetime.strptime(str(value)[:10], "%Y-%m-%d").date())
                except (TypeError, ValueError):
                    continue
        return (min(dates), max(dates)) if dates else (None, None)

    def create(self, request: SnapshotRequest) -> Dict[str, Any]:
        bars = normalize_bars(request.bars)
        payload = stable_json(bars)
        payload_bytes = len(payload.encode("utf-8"))
        if payload_bytes > self.max_payload_bytes:
            raise ValueError(
                f"Snapshot payload exceeds the size budget: "
                f"{payload_bytes} > {self.max_payload_bytes} bytes"
            )
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        actual_start, actual_end = self._resolve_range(bars)
        # 会话数只在日线且日历可用时参与判定；拿不到日历就如实退回首尾判定。
        expected_sessions = (
            count_sessions(request.market, request.requested_start, request.requested_end)
            if request.interval == "daily" else None
        )
        coverage = compute_coverage(
            actual_start, actual_end, request.requested_start, request.requested_end,
            rows=len(bars), required_rows=request.required_rows,
            expected_sessions=expected_sessions,
        )
        quality = assess_data_quality(
            source=request.source,
            price_adjustment=request.price_adjustment,
            currency=request.currency,
            volume_unit=request.volume_unit,
            coverage_complete=coverage,
        )
        quality["coverage"] = describe_coverage(
            requested_start=request.requested_start,
            requested_end=request.requested_end,
            rows=len(bars),
            expected_sessions=expected_sessions,
        )
        snapshot_id = build_snapshot_id(
            instrument=request.instrument,
            market=request.market,
            interval=request.interval,
            source=request.source,
            price_adjustment=request.price_adjustment,
            currency=request.currency,
            volume_unit=request.volume_unit,
            requested_start=request.requested_start,
            requested_end=request.requested_end,
            resolved_start=actual_start,
            resolved_end=actual_end,
            rows=len(bars),
            payload_hash=payload_hash,
        )
        with self.db.get_session() as session:
            existing = session.get(MarketDataSnapshot, snapshot_id)
            if existing is not None:
                # 幂等：已存在则原样返回，绝不覆盖已有冻结内容
                return self._serialize(existing)
            row = MarketDataSnapshot(
                snapshot_id=snapshot_id,
                schema_version=SCHEMA_VERSION,
                instrument=request.instrument,
                market=request.market,
                interval=request.interval,
                requested_start=request.requested_start,
                requested_end=request.requested_end,
                resolved_start=actual_start,
                resolved_end=actual_end,
                rows=len(bars),
                price_adjustment=request.price_adjustment,
                currency=request.currency,
                volume_unit=request.volume_unit,
                source=request.source,
                coverage_complete=coverage,
                data_quality_status=quality["data_quality_status"],
                input_eligibility=quality["input_eligibility"],
                quality_json=stable_json(quality),
                payload=payload,
                payload_hash=payload_hash,
            )
            session.add(row)
            session.commit()
            return self._serialize(row)

    @staticmethod
    def _serialize(row: MarketDataSnapshot, detail: bool = False) -> Dict[str, Any]:
        result = {
            "snapshot_id": row.snapshot_id,
            "schema_version": row.schema_version,
            "instrument": row.instrument,
            "market": row.market,
            "interval": row.interval,
            "requested_start": row.requested_start.isoformat() if row.requested_start else None,
            "requested_end": row.requested_end.isoformat() if row.requested_end else None,
            "resolved_start": row.resolved_start.isoformat() if row.resolved_start else None,
            "resolved_end": row.resolved_end.isoformat() if row.resolved_end else None,
            "rows": row.rows,
            "source": row.source,
            "price_adjustment": row.price_adjustment,
            "currency": row.currency,
            "volume_unit": row.volume_unit,
            "coverage_complete": row.coverage_complete,
            "data_quality_status": row.data_quality_status,
            "input_eligibility": bool(row.input_eligibility),
            "quality": json.loads(row.quality_json) if row.quality_json else {},
            "payload_hash": row.payload_hash,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        if detail:
            result["bars"] = json.loads(row.payload)
        return result

    def get(self, snapshot_id: str, *, detail: bool = False) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(MarketDataSnapshot, snapshot_id)
            return self._serialize(row, detail=detail) if row else None

    def list(self, *, instrument: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            statement = select(MarketDataSnapshot)
            if instrument:
                statement = statement.where(MarketDataSnapshot.instrument == instrument)
            statement = statement.order_by(
                MarketDataSnapshot.created_at.desc(),
                MarketDataSnapshot.snapshot_id.desc(),
            ).limit(limit)
            return [self._serialize(row) for row in session.scalars(statement).all()]

    def storage_stats(self) -> Dict[str, Any]:
        """只读体积/规模概览：用于判断快照是否需要导出或人工清理。"""
        with self.db.get_session() as session:
            rows = session.scalars(select(MarketDataSnapshot)).all()
        payload_bytes = sum(len(row.payload.encode("utf-8")) for row in rows)
        return {
            "rows": len(rows),
            "payload_bytes": payload_bytes,
            "max_payload_bytes": self.max_payload_bytes,
            "total_budget_bytes": SNAPSHOT_TOTAL_BUDGET_BYTES,
            "within_budget": payload_bytes <= SNAPSHOT_TOTAL_BUDGET_BYTES,
            "instruments": sorted({row.instrument for row in rows}),
        }
