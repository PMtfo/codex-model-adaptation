#!/usr/bin/env python3
"""D8 回归测试：delivery-completion-gate Stop 熔断（自包含，无外部 hook 依赖）。

运行：python3 hooks/tests/test_delivery_gate_circuit_breaker.py
或：  python3 -m unittest hooks.tests.test_delivery_gate_circuit_breaker
无需 pytest；失败退出码非 0。
覆盖：连续阻断达上限后放行、完成态清零、端到端 subprocess 行为。
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
GATE = HERE.parent / "delivery-completion-gate.py"
SPEC = importlib.util.spec_from_file_location("delivery_completion_gate", GATE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def payload(message, *, active=False, session_id="s", turn_id="t"):
    return {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": "/tmp",
        "model": "m",
        "permission_mode": "dontAsk",
        "transcript_path": None,
        "stop_hook_active": active,
        "last_assistant_message": message,
    }


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        # 独立状态文件 + 屏蔽 automation gate（自包含，不依赖其它 hook 文件）。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        state = pathlib.Path(self._tmp.name) / "delivery-completion-gate.json"
        p1 = mock.patch.object(MODULE, "DELIVERY_GATE_STATE_PATH", state)
        p2 = mock.patch.object(MODULE, "_automation_recovery_output", return_value={})
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_blocks_then_circuit_breaks(self):
        msg = "我会继续调研并给出结果。"
        n = MODULE.MAX_CONSECUTIVE_STOP_BLOCKS
        decisions = [
            MODULE.evaluate(payload(msg, active=True)).get("decision")
            for _ in range(n + 2)
        ]
        self.assertEqual(decisions[:n], ["block"] * n)
        self.assertTrue(all(d is None for d in decisions[n:]), decisions)

    def test_completion_resets_counter(self):
        unfinished, completed = "我会继续调研并给出结果。", "已完成交付并回读验证通过。"
        for _ in range(MODULE.MAX_CONSECUTIVE_STOP_BLOCKS):
            MODULE.evaluate(payload(unfinished, active=True))
        self.assertEqual(MODULE.evaluate(payload(completed)), {})
        self.assertEqual(
            MODULE.evaluate(payload(unfinished, active=True)).get("decision"), "block"
        )

    def test_end_to_end_subprocess(self):
        env = {
            **os.environ,
            "CODEX_DELIVERY_COMPLETION_GATE_STATE": str(
                pathlib.Path(self._tmp.name) / "e2e.json"
            ),
        }
        n = MODULE.MAX_CONSECUTIVE_STOP_BLOCKS
        decisions = []
        for i in range(1, n + 3):
            pl = payload("我会继续调研并给出结果。", active=True,
                         session_id="e2e", turn_id="t%d" % i)
            r = subprocess.run(
                [sys.executable, str(GATE)],
                input=json.dumps(pl, ensure_ascii=False),
                text=True, capture_output=True, env=env,
            )
            decisions.append(json.loads(r.stdout or "{}").get("decision"))
        self.assertEqual(decisions[:n], ["block"] * n)
        self.assertTrue(all(d is None for d in decisions[n:]), decisions)


if __name__ == "__main__":
    unittest.main()
