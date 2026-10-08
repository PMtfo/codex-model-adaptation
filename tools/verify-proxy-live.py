#!/usr/bin/env python3
"""验证「重启 WorkBuddy Desktop 后，桌面端请求是否真的走了本地归一化代理」。

背景：Codex Desktop 在进程启动时读取一次 `~/.codex/config.toml`，之后不会热加载。
因此修改 base_url 后，**必须重启桌面应用**，新配置才会生效。

本脚本通过对比「代理日志中的请求来源」与「Desktop 进程启动时间」来判断：
  - 若代理日志出现启动时间晚于 config 修改时间的请求 → 已生效
  - 若只有 codex_exec（CLI）来源 → Desktop 尚未重启

用法：python3 ~/.codex/scripts/verify-proxy-live.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path


CFG = Path(os.path.expanduser("~/.codex/config.toml"))
LOG = Path(os.path.expanduser("~/.codex/proxy/dsml-proxy.log"))
PROXY_PORT = 8899


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""


def proxy_url() -> str:
    try:
        txt = CFG.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    m = re.search(
        r"\[model_providers\.[^\]]+\](.*?)(?=\n\[|\Z)", txt, re.S
    )
    for seg in re.findall(r"\[model_providers\.[^\]]+\](.*?)(?=\n\[|\Z)", txt, re.S):
        mm = re.search(r"base_url\s*=\s*['\"]([^'\"]+)['\"]", seg)
        if mm and "127.0.0.1" in mm.group(1):
            return mm.group(1)
    return ""


def main() -> int:
    print("== 1) 配置指向 ==")
    url = proxy_url()
    if url:
        print("   本地代理已配置：", url)
    else:
        print("   [WARN] 未发现指向 127.0.0.1 的 provider base_url，D1 修复未启用")

    print()
    print("== 2) 代理可达性 ==")
    code = sh(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
               "--noproxy", "*", "-m", "8", f"http://127.0.0.1:{PROXY_PORT}/"]).strip()
    print("   GET / ->", code or "(无响应)")

    print()
    print("== 3) 代理收到的请求来源统计 ==")
    if not LOG.exists():
        print("   日志不存在：", LOG)
        return 1
    txt = LOG.read_text(encoding="utf-8", errors="ignore")
    uas = re.findall(r"ua=([^\s]+)", txt)
    if not uas:
        print("   尚无请求记录")
    else:
        from collections import Counter
        for ua, n in Counter(uas).most_common():
            print("   %5d  %s" % (n, ua))

    print()
    print("== 4) Desktop 进程启动时间（判断是否已重启）==")
    ps = sh(["ps", "-ax", "-o", "pid,lstart,command"])
    desktop = [l for l in ps.splitlines() if "ChatGPT.app/Contents/MacOS/ChatGPT" in l]
    for l in desktop[:2]:
        print("  ", l.strip()[:110])
    cfg_mtime = time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(CFG.stat().st_mtime)) if CFG.exists() else "?"
    print("   config.toml 修改时间:", cfg_mtime)

    print()
    print("== 结论 ==")
    has_desktop_req = any("Codex" in u or "desktop" in u.lower() for u in uas) and \
                      not all(u.startswith("codex_exec") or u.startswith("curl") for u in uas)
    if not url:
        print("   D1 未启用：provider base_url 未指向本地代理（见 D6 的 wrapper / 配置说明）。")
        return 1
    if has_desktop_req:
        print("   已生效：代理日志中出现非 CLI 来源的请求。")
        return 0
    print("   尚未生效：代理日志中只有 CLI（codex_exec）来源。")
    print("   原因：Desktop 在启动时读取一次配置，需**重启 WorkBuddy Desktop** 后才会走代理。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
