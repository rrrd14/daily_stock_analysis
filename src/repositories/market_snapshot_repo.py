"""不可变行情冻结快照仓储（WP4 第一阶段）。

设计边界：

- 只提供 ``create`` / ``get`` / ``list``，**没有 update / delete**：快照创建后不得
  原地修改或自动删除，运行记录通过 ``snapshot_id`` 引用它以便重放。
- ``snapshot_id`` 由「口径（复权/币种/单位/来源）+ 区间 + 内容哈希」共同决定，
  因此同源同区间但复权口径不同会得到**不同身份**。
- ``payload_hash`` 使用稳定序列化（键排序、固定分隔符、拒绝 NaN/Infinity）。
- 质量评估对未知口径一律判为不可用于默认策略收益计算，不把 unknown 提升为 verified；
  冻结前还会逐行校验行情（OHLCV 缺失、NaN/Inf、日期非法或重复、价格关系不成立），
  有问题的行不计入覆盖，且这类快照判为 unknown（价格不可用，不能只降级为 partial）。
- 影响持久化资格的要求（``required_rows``）与质量规则版本（``SNAPSHOT_QC_VERSION``）
  都进快照身份：更高的数据要求或规则升级会得到**新身份**，不会复用旧快照的布尔结论。
- 覆盖判定分两层：首尾日期（必做）+ 交易日历 session 数（日历可用时）；日历不可用时
  ``quality["coverage"]["basis"]`` 会如实写 ``endpoints_only``，不伪装成已核对完整。
"""

from __future__ import annotations

import hashlib
import json
import math
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

# 逐行校验（WP4 修订）：缺 OHLCV、NaN/Inf、日期非法或重复的行**不算有效交易日**，
# 因而既不能填满覆盖判定，也不能取得默认策略资格。
REQUIRED_BAR_COLUMNS = ("open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("open", "high", "low", "close")
PAYLOAD_PROBLEMS = (
    "invalid_date", "duplicate_date", "missing_field", "non_finite", "price_relation",
)

# 质量规则版本：判定规则变化时必须让身份随之变化，否则旧快照的布尔结论会被新请求复用。
SNAPSHOT_QC_VERSION = "2"

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


def validate_bars(
    bars: Iterable[Any],
    *,
    requested_start: Optional[date] = None,
    requested_end: Optional[date] = None,
) -> Dict[str, Any]:
    """冻结前逐行校验行情本身是否可用，并给出请求区间内的有效交易日集合。

    问题分类：

    - 日期无法解析 → ``invalid_date``；同一天出现多次 → ``duplicate_date``；
    - 缺 OHLCV 任一字段 → ``missing_field``；NaN/Infinity/非数值 → ``non_finite``；
    - 价格关系不成立（`high < low`，或 `open`/`close` 落在 `[low, high]` 之外）→ ``price_relation``。

    只有**没有任何问题**的行才会进入 ``sessions``：坏行与重复行不能填满覆盖判定，
    也不能让快照取得默认策略资格。``stable_json`` 把 NaN 转成 null 只是存储策略，
    不代表数据通过校验。
    """
    problems: set = set()
    sessions: set = set()
    for bar in normalize_bars(bars):
        try:
            day = date.fromisoformat(str(bar.get("date"))[:10])
        except (TypeError, ValueError):
            problems.add("invalid_date")
            continue
        if requested_start is not None and day < requested_start:
            continue
        if requested_end is not None and day > requested_end:
            continue

        row_problems: set = set()
        if day in sessions:
            row_problems.add("duplicate_date")
        prices: Dict[str, float] = {}
        for column in REQUIRED_BAR_COLUMNS:
            value = bar.get(column)
            if value is None or value == "":
                row_problems.add("missing_field")
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                row_problems.add("non_finite")
                continue
            if not math.isfinite(number):
                row_problems.add("non_finite")
                continue
            prices[column] = number
        if len(prices) == len(REQUIRED_BAR_COLUMNS):
            low, high = prices["low"], prices["high"]
            inside = low <= prices["open"] <= high and low <= prices["close"] <= high
            if high < low or not inside:
                row_problems.add("price_relation")

        if row_problems:
            problems |= row_problems
        else:
            sessions.add(day)

    return {
        "problems": [key for key in PAYLOAD_PROBLEMS if key in problems],
        "sessions": sorted(sessions),
    }


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
    有效交易日数显著少于区间应有 session 数也判为覆盖不足，用于抓「首尾齐全但中间
    缺一大段」这种情况；容差见 ``SESSION_DEFICIT_TOLERANCE_RATIO``。

    ``rows`` 由调用方传入**有效唯一交易日数**（见 ``validate_bars``），不是存储行数：
    坏行与重复行不能用来凑满覆盖。
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
    counted_sessions: Optional[int] = None,
) -> Dict[str, Any]:
    """覆盖判定的依据明细（供审计；不参与快照身份哈希）。

    ``rows`` 是存储行数，``counted_sessions`` 是**真正参与判定**的有效唯一交易日数
    （坏行与重复行不计入）。``basis`` 明确区分「真的按交易日历核对过」与「只做了
    首尾日期判定」，避免读者把缺少日历的情况误认为已核对完整。
    """
    counted = int(rows) if counted_sessions is None else int(counted_sessions)
    deficit: Optional[int] = None
    if expected_sessions is not None:
        deficit = max(int(expected_sessions) - counted, 0)
    return {
        "basis": "trading_calendar" if expected_sessions is not None else "endpoints_only",
        "expected_sessions": expected_sessions,
        "rows": int(rows),
        "counted_sessions": counted,
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
    payload_problems: Iterable[str] = (),
) -> Dict[str, Any]:
    """按「来源 / 复权 / 币种 / 单位 / 覆盖 / 行情本身」判定质量与策略资格。

    - 全部已知且覆盖完整、且行情行未发现缺陷 → ``verified``（可用于默认策略收益计算）
    - 来源或复权口径未知 → ``unknown``（不可用于任何收益口径计算）
    - 行情行缺失字段、含 NaN/Inf、日期非法/重复或价格关系不成立 → ``unknown``
      （这类数据连价格都不可用，不能只降级为「可研究」）
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
    for problem in payload_problems:
        if problem not in missing:
            missing.append(problem)

    if not missing:
        status = "verified"
    elif "source" in missing or "price_adjustment" in missing or any(
        problem in missing for problem in PAYLOAD_PROBLEMS
    ):
        status = "unknown"
    else:
        status = "partial"

    return {
        "data_quality_status": status,
        "input_eligibility": status == "verified",
        "missing": missing,
        "qc_version": SNAPSHOT_QC_VERSION,
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
    required_rows: Optional[int] = None,
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
        # 影响持久化资格的要求与规则版本必须进身份：否则更高的 required_rows、
        # 或规则升级后的新判定，都会复用旧快照上的布尔结论。
        "required_rows": required_rows,
        "qc_version": SNAPSHOT_QC_VERSION,
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
        payload_check = validate_bars(
            bars,
            requested_start=request.requested_start,
            requested_end=request.requested_end,
        )
        sessions = payload_check["sessions"]
        if sessions:
            # 首尾也只取有效交易日：坏行不能决定「数据覆盖到哪里」。
            actual_start, actual_end = sessions[0], sessions[-1]
        # 会话数只在日线且日历可用时参与判定；拿不到日历就如实退回首尾判定。
        expected_sessions = (
            count_sessions(request.market, request.requested_start, request.requested_end)
            if request.interval == "daily" else None
        )
        coverage = compute_coverage(
            actual_start, actual_end, request.requested_start, request.requested_end,
            rows=len(sessions), required_rows=request.required_rows,
            expected_sessions=expected_sessions,
        )
        quality = assess_data_quality(
            source=request.source,
            price_adjustment=request.price_adjustment,
            currency=request.currency,
            volume_unit=request.volume_unit,
            coverage_complete=coverage,
            payload_problems=payload_check["problems"],
        )
        quality["payload"] = {
            "rows": len(bars),
            "valid_sessions": len(sessions),
            "problems": payload_check["problems"],
            "qc_version": SNAPSHOT_QC_VERSION,
        }
        quality["coverage"] = describe_coverage(
            requested_start=request.requested_start,
            requested_end=request.requested_end,
            rows=len(bars),
            counted_sessions=len(sessions),
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
            required_rows=request.required_rows,
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
