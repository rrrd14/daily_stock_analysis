#!/usr/bin/env python3
"""从单一真源 `.claude/skills/` 生成 `.agents/skills/` 镜像。

治理约定（见 AGENTS.md 第 2 节）：

- 规则真源是仓库根目录 `AGENTS.md`；skills 真源是 `.claude/skills/`。
- `.agents/` 是本地 agent 脚手架，**不入库**（`.gitignore` 已忽略），避免出现
  手工长期维护的同义副本。
- 本脚本负责生成；手工改动会在下一次同步被覆盖，也会被
  `scripts/check_ai_assets.py` 判定为漂移。

用法::

    python scripts/sync_agent_skills.py          # 生成/更新镜像
    python scripts/sync_agent_skills.py --check  # 只校验是否与生成结果一致
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / ".claude" / "skills"
TARGET_DIR = ROOT / ".agents" / "skills"

# 源文档里的路径引用需要映射为 .agents 生态下的等价路径
PATH_REWRITES = (
    (".claude/reviews", ".Codex/reviews"),
)

GENERATED_README = """# Repository agent skills (generated)

本目录由 `scripts/sync_agent_skills.py` 从 `.claude/skills/` 生成，**请勿手工编辑**。

- 规则真源：仓库根目录 `AGENTS.md`
- skills 真源：`.claude/skills/`
- 本目录属于本地 agent 脚手架，已在 `.gitignore` 中忽略
"""


def render(relative_path: Path) -> str:
    text = (SOURCE_DIR / relative_path).read_text(encoding="utf-8")
    for old, new in PATH_REWRITES:
        text = text.replace(old, new)
    return text


def expected_files() -> "dict[Path, str]":
    """返回相对路径 -> 期望内容；README 单独生成，不直接复制源 README。"""
    files: "dict[Path, str]" = {Path("README.md"): GENERATED_README}
    for source in sorted(SOURCE_DIR.rglob("*.md")):
        relative = source.relative_to(SOURCE_DIR)
        if relative.name == "README.md":
            continue
        files[relative] = render(relative)
    return files


def check(expected: "dict[Path, str]") -> int:
    drifted = []
    for relative, content in expected.items():
        target = TARGET_DIR / relative
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            drifted.append(str(relative).replace("\\", "/"))
    if drifted:
        print("[sync-agent-skills] ERROR: mirror drifted: " + ", ".join(drifted),
              file=sys.stderr)
        return 1
    print(f"[sync-agent-skills] OK ({len(expected)} files in sync)")
    return 0


def write(expected: "dict[Path, str]") -> int:
    removed = 0
    if TARGET_DIR.exists():
        for existing in sorted(TARGET_DIR.rglob("*")):
            if existing.is_file() and existing.relative_to(TARGET_DIR) not in expected:
                existing.unlink()
                removed += 1
    for relative, content in expected.items():
        target = TARGET_DIR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    suffix = f", removed {removed} stale" if removed else ""
    print(f"[sync-agent-skills] wrote {len(expected)} files to "
          f"{TARGET_DIR.relative_to(ROOT)}{suffix}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync .agents/skills from .claude/skills")
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    args = parser.parse_args()

    if not SOURCE_DIR.exists():
        print(f"[sync-agent-skills] ERROR: source missing: {SOURCE_DIR}", file=sys.stderr)
        return 1

    expected = expected_files()
    return check(expected) if args.check else write(expected)


if __name__ == "__main__":
    raise SystemExit(main())
