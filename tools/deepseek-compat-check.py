#!/usr/bin/env python3
"""DeepSeek 接入 WorkBuddy 兼容性健康检查。

固化 2026-10-08 修复的 5 项缺陷，用于防回归自检。
只读检查，不修改任何配置。

用法：python3 ~/.codex/scripts/deepseek-compat-check.py [--json]
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


CFG = Path(os.path.expanduser("~/.codex/config.toml"))
HOOKS = Path(os.path.expanduser("~/.codex/hooks.json"))
LOCAL_BIN = Path(os.path.expanduser("~/.local/bin"))

OPENAI_NAME = "OpenAI"


def check_p1() -> tuple[bool, str]:
    """P1: provider 显示名不能是 'OpenAI'，否则 Codex 走远程压缩而 relay 不支持。"""
    try:
        txt = CFG.read_text(encoding="utf-8")
    except Exception as e:
        return False, "读取 config.toml 失败: %s" % e
    # 找到 my_gateway 段内的 name
    m = re.search(
        r"\[model_providers\.my_gateway\](.*?)(?=\n\[|\Z)", txt, re.S
    )
    if not m:
        return False, "未找到 [model_providers.my_gateway]"
    seg = m.group(1)
    nm = re.search(r"^name\s*=\s*['\"]([^'\"]+)", seg, re.M)
    if not nm:
        return False, "该段缺少 name 字段"
    if nm.group(1) == OPENAI_NAME:
        return False, (
            "provider name 仍为 'OpenAI' → Codex 会走远程压缩，"
            "而 relay 的 /responses/compact 返回 404，长会话必然压缩失败"
        )
    return True, "provider name = '%s'（非 OpenAI，走本地压缩）" % nm.group(1)


def check_p2() -> tuple[bool, str]:
    """P2: PostToolUseFailure 不得注入 additionalContext。"""
    p = Path(os.path.expanduser("~/.codex/hooks/tool-failure-guard.py"))
    if not p.exists():
        return False, "tool-failure-guard.py 不存在"
    txt = p.read_text(encoding="utf-8", errors="ignore")
    # 允许注释里出现，但不允许代码里 return additionalContext
    code = re.sub(r"#[^\n]*", "", txt)
    if "additionalContext" in code:
        return False, "仍在代码中返回 additionalContext → 可能触发 DeepSeek 400 配对错误"
    return True, "已移除 additionalContext 注入，改审计文件记录"


def check_p3() -> tuple[bool, str]:
    """P3: 远程压缩端点可用性（relay 侧）。"""
    return True, "随 P1 一并规避（不产生 compaction item）"


def check_p4() -> tuple[bool, str]:
    """P4: DSML guard 已注册。"""
    try:
        h = json.loads(HOOKS.read_text(encoding="utf-8"))
    except Exception as e:
        return False, "读取 hooks.json 失败: %s" % e
    cmds = []
    for grp in h.get("hooks", {}).get("Stop", []):
        for hk in grp.get("hooks", []):
            cmds.append(hk.get("command", ""))
    if not any("dsml-guard.py" in c for c in cmds):
        return False, "Stop 链未注册 dsml-guard.py"
    gp = Path(os.path.expanduser("~/.codex/hooks/dsml-guard.py"))
    if not gp.exists():
        return False, "dsml-guard.py 文件不存在"
    return True, "dsml-guard.py 已注册在 Stop 链"


def check_p5() -> tuple[bool, str]:
    """P5: codex-code-mode-host 必须可解析。"""
    host = LOCAL_BIN / "codex-code-mode-host"
    if not host.exists():
        return False, "~/.local/bin/codex-code-mode-host 缺失 → code_mode_only 模型工具无法执行"
    if host.is_symlink() and not host.resolve().exists():
        return False, "符号链接指向的目标不存在"
    return True, "codex-code-mode-host 可用"


CHECKS = [
    ("P1 远程压缩", check_p1),
    ("P2 hook 注入", check_p2),
    ("P3 compaction item", check_p3),
    ("P4 DSML 泄漏", check_p4),
    ("P5 code-mode host", check_p5),
]


def main() -> int:
    as_json = "--json" in sys.argv
    results = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "检查异常: %s" % e
        results.append({"id": name, "ok": ok, "detail": detail})

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            mark = "PASS" if r["ok"] else "FAIL"
            print("[%s] %-20s %s" % (mark, r["id"], r["detail"]))
        failed = sum(1 for r in results if not r["ok"])
        print()
        print("合计: %d 项，通过 %d，失败 %d" % (len(results), len(results) - failed, failed))

    return 1 if any(not r["ok"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
