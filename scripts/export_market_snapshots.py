#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Export frozen market-data snapshots (WP4 operations tooling).

为什么需要它：冻结快照是回测可复现的证据来源，但 ``storage_stats()`` 只能告诉你
「有多少、多大」。换机器、换库里重放旧运行、或做离线审计时，需要一个能把快照
完整搬走并逐份核对内容哈希的出口。

产物：

- ``<out>/snapshots.jsonl``：每行一个快照对象（元数据 + ``bars`` 冻结行情行）
- ``<out>/manifest.json``：导出时间、条数、文件 SHA256、逐份 payload 哈希核对结果、
  ``storage_stats()`` 体积概览

只读保证：本工具只调用 ``list()`` / ``get()`` / ``storage_stats()``，不写入、
不修改、不删除任何快照，也不会新增快照行。

Usage:
    python scripts/export_market_snapshots.py --out exports/snapshots
    python scripts/export_market_snapshots.py --out exports/snapshots --instrument 588000 --limit 50

Exits 0 on success, non-zero with a human-readable message otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.repositories.backtest_run_repo import json_value
from src.repositories.market_snapshot_repo import (
    SCHEMA_VERSION,
    MarketDataSnapshotRepository,
    normalize_bars,
    stable_json,
)
from src.time_utils import beijing_now

DEFAULT_LIMIT = 100
EXPORT_SCHEMA_VERSION = "1"


def _hash_payload(bars: List[Dict[str, Any]]) -> str:
    """用与写入时完全相同的方式重算内容哈希，用于核对是否被改动。"""
    return hashlib.sha256(
        stable_json(normalize_bars(bars)).encode("utf-8")
    ).hexdigest()


def export_snapshots(
    repo: MarketDataSnapshotRepository,
    out_dir: Path,
    *,
    instrument: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """把快照导出到 ``out_dir``，返回 manifest 内容（同时已落盘）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    listed = repo.list(instrument=instrument, limit=limit)
    snapshots_path = out_dir / "snapshots.jsonl"
    digest = hashlib.sha256()
    mismatches: List[str] = []
    total_bars = 0

    with snapshots_path.open("w", encoding="utf-8", newline="\n") as handle:
        for meta in listed:
            snapshot_id = meta["snapshot_id"]
            detail = repo.get(snapshot_id, detail=True)
            if detail is None:
                # 列表与详情之间被并发删除（本仓库无 delete API，属异常情况）
                mismatches.append(snapshot_id)
                continue
            bars = detail.get("bars") or []
            total_bars += len(bars)
            if _hash_payload(bars) != detail.get("payload_hash"):
                mismatches.append(snapshot_id)
            # 用与快照存储一致的稳定序列化：日期归一为 ISO、拒绝 NaN/Infinity，
            # 避免单份异常载荷让整次导出中途抛错。
            line = stable_json(detail) + "\n"
            handle.write(line)
            digest.update(line.encode("utf-8"))

    manifest = {
        "tool": "export_market_snapshots",
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "snapshot_schema_version": SCHEMA_VERSION,
        "exported_at": beijing_now().isoformat(timespec="seconds"),
        "out_dir": str(out_dir),
        "instrument_filter": instrument,
        "limit": int(limit),
        "truncated": len(listed) >= int(limit),
        "snapshots": len(listed),
        "bars": total_bars,
        "snapshots_file": snapshots_path.name,
        "snapshots_sha256": digest.hexdigest(),
        "hash_verified": len(listed) - len(mismatches),
        "hash_mismatches": mismatches,
        "storage_stats": repo.storage_stats(),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(json_value(manifest), sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="导出冻结行情快照为 JSONL + manifest（只读，不修改数据库）。",
    )
    parser.add_argument("--out", required=True, help="导出目录（不存在时创建）")
    parser.add_argument("--instrument", default=None, help="只导出指定标的（如 588000 / AAPL）")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help=f"最多导出条数（默认 {DEFAULT_LIMIT}）",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    from src.storage import DatabaseManager

    try:
        repo = MarketDataSnapshotRepository(DatabaseManager.get_instance())
        manifest = export_snapshots(
            repo, Path(args.out), instrument=args.instrument, limit=args.limit,
        )
    except Exception as exc:  # pragma: no cover - 运维路径，异常需可见但不抛栈
        print(f"ERROR: 导出失败: {exc}", file=sys.stderr)
        return 1

    print(
        f"已导出 {manifest['snapshots']} 份快照（{manifest['bars']} 行行情）"
        f"到 {manifest['out_dir']}"
    )
    print(f"内容哈希核对通过 {manifest['hash_verified']} 份")
    if manifest["hash_mismatches"]:
        print(f"WARNING: 以下快照内容哈希不匹配: {manifest['hash_mismatches']}", file=sys.stderr)
        return 2
    if manifest["truncated"]:
        print(
            f"WARNING: 结果被 --limit={manifest['limit']} 截断，可能还有更多快照未导出",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
