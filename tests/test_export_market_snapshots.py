# -*- coding: utf-8 -*-
"""WP4 运维工具回归：``scripts/export_market_snapshots.py``。

验收要点：

- 导出内容包含元数据与冻结行情行，manifest 计数与文件哈希可核对；
- 逐份重算内容哈希，被改动的快照必须被点名（不静默）；
- 导出只读：导出前后快照行数/体积不变；
- ``--instrument`` / ``--limit`` 生效，被截断时显式告警。

全部用例离线、确定性，使用内存或临时 SQLite。
"""

import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
SCRIPT_PATH = SCRIPT_DIR / "export_market_snapshots.py"
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

export_tool = importlib.import_module("export_market_snapshots")

from src.config import Config  # noqa: E402
from src.repositories.market_snapshot_repo import (  # noqa: E402
    MarketDataSnapshotRepository,
    SnapshotRequest,
)
from src.storage import DatabaseManager  # noqa: E402

START = date(2023, 9, 13)


def _bars(start: date, days: int, base: float = 10.0):
    return [
        {
            "date": start + timedelta(days=index),
            "open": base,
            "high": base + 1,
            "low": base - 1,
            "close": base + index * 0.01,
            "volume": 1000.0,
            "amount": 10000.0,
            "pct_chg": 0.1,
        }
        for index in range(days)
    ]


class ExportSnapshotsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")
        self.repo = MarketDataSnapshotRepository(self.db)
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "out"

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        self._tmp.cleanup()

    def _request(self, **overrides) -> SnapshotRequest:
        params = dict(
            instrument="588000",
            market="cn",
            bars=_bars(START, 5),
            source="TencentFetcher",
            price_adjustment="provider_default",
            currency="CNY",
            volume_unit="shares",
            requested_start=START,
            requested_end=START + timedelta(days=4),
        )
        params.update(overrides)
        return SnapshotRequest(**params)

    def _read_lines(self, name: str = "snapshots.jsonl"):
        text = (self.out_dir / name).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def test_export_writes_snapshot_lines_and_correlatable_manifest(self) -> None:
        first = self.repo.create(self._request())
        second = self.repo.create(self._request(price_adjustment="qfq"))

        manifest = export_tool.export_snapshots(self.repo, self.out_dir)

        lines = self._read_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(manifest["snapshots"], 2)
        self.assertEqual(manifest["bars"], 10)
        self.assertEqual(manifest["hash_verified"], 2)
        self.assertEqual(manifest["hash_mismatches"], [])
        exported_ids = {line["snapshot_id"] for line in lines}
        self.assertEqual(exported_ids, {first["snapshot_id"], second["snapshot_id"]})
        for line in lines:
            self.assertEqual(len(line["bars"]), 5)
            self.assertEqual(line["schema_version"], "1")

        raw = (self.out_dir / "snapshots.jsonl").read_bytes()
        self.assertEqual(manifest["snapshots_sha256"], hashlib.sha256(raw).hexdigest())
        on_disk = json.loads((self.out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["snapshots_sha256"], manifest["snapshots_sha256"])

    def test_export_does_not_modify_snapshots(self) -> None:
        self.repo.create(self._request())
        before = self.repo.storage_stats()

        export_tool.export_snapshots(self.repo, self.out_dir)

        after = self.repo.storage_stats()
        self.assertEqual(before["rows"], after["rows"])
        self.assertEqual(before["payload_bytes"], after["payload_bytes"])
        self.assertEqual(len(self.repo.list(instrument="588000")), 1)

    def test_export_flags_tampered_snapshot_instead_of_silently_passing(self) -> None:
        created = self.repo.create(self._request())
        original_get = self.repo.get

        def tampered(snapshot_id, *, detail=False):
            record = original_get(snapshot_id, detail=detail)
            if record and detail:
                record = dict(record)
                record["bars"] = _bars(START, 5, base=999.0)
            return record

        with patch.object(self.repo, "get", side_effect=tampered):
            manifest = export_tool.export_snapshots(self.repo, self.out_dir)

        self.assertEqual(manifest["hash_verified"], 0)
        self.assertEqual(manifest["hash_mismatches"], [created["snapshot_id"]])

    def test_export_filters_by_instrument(self) -> None:
        self.repo.create(self._request())
        self.repo.create(self._request(instrument="AAPL", market="us", currency="USD"))

        manifest = export_tool.export_snapshots(self.repo, self.out_dir, instrument="AAPL")

        lines = self._read_lines()
        self.assertEqual(manifest["snapshots"], 1)
        self.assertEqual(lines[0]["instrument"], "AAPL")
        self.assertEqual(manifest["instrument_filter"], "AAPL")

    def test_export_marks_truncated_when_limit_is_reached(self) -> None:
        self.repo.create(self._request())
        self.repo.create(self._request(price_adjustment="qfq"))

        with_limit = export_tool.export_snapshots(self.repo, self.out_dir, limit=2)
        self.assertTrue(with_limit["truncated"])

        roomy = export_tool.export_snapshots(self.repo, self.out_dir, limit=10)
        self.assertFalse(roomy["truncated"])



class ExportCliProcessTestCase(unittest.TestCase):
    """R5：按路径执行脚本必须能启动（不能依赖外部 PYTHONPATH）。

    ``python scripts/export_market_snapshots.py`` 的 sys.path[0] 是 ``scripts/``，
    这组用例在**清空 PYTHONPATH** 的子进程里跑，覆盖真实入口而不是只导入函数。
    """

    def _run(self, args, env_overrides=None, cwd=None):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.update(env_overrides or {})
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    def test_help_runs_without_pythonpath(self) -> None:
        result = self._run(["--help"])

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("--out", result.stdout)

    def test_export_runs_from_repo_root_and_writes_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "snapshots.db"
            out_dir = Path(tmp) / "exported"
            self._seed_database(db_path)

            result = self._run(
                ["--out", str(out_dir), "--instrument", "588000"],
                env_overrides={"DATABASE_PATH": str(db_path)},
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((out_dir / "snapshots.jsonl").is_file())
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["snapshots"], 1)
            self.assertEqual(manifest["hash_mismatches"], [])

    @staticmethod
    def _seed_database(db_path: Path) -> None:
        """用与子进程相同的 DATABASE_PATH 建库并写入一份快照。"""
        original = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        try:
            repo = MarketDataSnapshotRepository(DatabaseManager.get_instance())
            repo.create(SnapshotRequest(
                instrument="588000", market="cn", bars=_bars(START, 5),
                source="TencentFetcher", price_adjustment="provider_default",
                currency="CNY", volume_unit="shares",
                requested_start=START, requested_end=START + timedelta(days=4),
            ))
        finally:
            DatabaseManager.reset_instance()
            Config.reset_instance()
            if original is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = original


class ExportMainCliTestCase(unittest.TestCase):
    """真实 CLI 路径：走配置单例拿到数据库，退出码语义稳定。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "snapshots.db"
        self._env_backup = {"DATABASE_PATH": os.environ.get("DATABASE_PATH")}
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def test_main_exports_from_configured_database(self) -> None:
        repo = MarketDataSnapshotRepository(DatabaseManager.get_instance())
        repo.create(SnapshotRequest(
            instrument="588000", market="cn", bars=_bars(START, 5),
            source="TencentFetcher", price_adjustment="provider_default",
            currency="CNY", volume_unit="shares",
            requested_start=START, requested_end=START + timedelta(days=4),
        ))
        out_dir = Path(self._tmp.name) / "cli-out"

        rc = export_tool.main(["--out", str(out_dir), "--instrument", "588000"])

        self.assertEqual(rc, 0)
        self.assertTrue((out_dir / "snapshots.jsonl").is_file())
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["snapshots"], 1)
        self.assertEqual(manifest["out_dir"], str(out_dir))

    def test_main_reports_failure_without_stack_trace(self) -> None:
        blocked = Path(self._tmp.name) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")

        rc = export_tool.main(["--out", str(blocked)])

        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
