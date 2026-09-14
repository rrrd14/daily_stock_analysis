# -*- coding: utf-8 -*-
"""R1 回归：workflow 里 `./path/script` 形式调用的脚本必须带可执行位。

CI 使用 ``./scripts/docker_e2e.sh``，但 ``actions/checkout`` 不会给普通文件补执行位；
mode 为 100644 时 Linux 下退出 **126**（脚本体根本不执行）。Windows 上
``bash scripts/docker_e2e.sh`` 成功不能证明这条 CI 命令可执行，因此这里直接检查
Git 索引里的 mode，而不是文件系统属性。
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# `run: ./scripts/foo.sh`（允许行尾注释/空白）
INVOCATION = re.compile(r"^\s*run:\s*(\./[\w./-]+)", re.MULTILINE)
EXECUTABLE_MODE = "100755"


class CiScriptModeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        if not (REPO_ROOT / ".git").exists():
            self.skipTest("导出的工作区没有 .git，无法读取 Git mode")

    def _workflow_invocations(self):
        paths = set()
        for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
            for match in INVOCATION.finditer(workflow.read_text(encoding="utf-8")):
                paths.add(match.group(1))
        return sorted(paths)

    def _git_modes(self, paths):
        result = subprocess.run(
            ["git", "ls-files", "-s", "--", *paths],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        modes = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                modes[parts[3]] = parts[0]
        return modes

    def test_relative_workflow_invocations_are_tracked_and_executable(self) -> None:
        invocations = self._workflow_invocations()
        # 正则失效时必须显式失败，而不是「没有可疑项」静默通过
        self.assertIn("./scripts/docker_e2e.sh", invocations)

        relative = [path[2:] for path in invocations]
        modes = self._git_modes(relative)

        for path in relative:
            with self.subTest(path=path):
                self.assertIn(path, modes, msg=f"{path} 未被 git 跟踪")
                self.assertEqual(
                    modes[path], EXECUTABLE_MODE,
                    msg=f"{path} 的 Git mode 是 {modes[path]}，CI 直接执行会得到 126",
                )


if __name__ == "__main__":
    unittest.main()
