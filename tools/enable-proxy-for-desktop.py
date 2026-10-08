#!/usr/bin/env python3
"""为桌面端启用 DSML/XML 归一化代理（自判断安全性）。

背景：
  Codex Desktop 与 CLI 共用 ~/.codex/config.toml，但桌面端拿不到 wrapper 里的
  NO_PROXY。若在 Desktop 缺少 NO_PROXY 时把 base_url 改为本地代理，
  系统代理会拦截回环请求并返回 502，导致桌面会话与 automation 全部失败。

本脚本先检查当前桌面进程是否已拥有 NO_PROXY，只有确认后才修改配置；
否则只输出提示，不动配置。默认为 dry-run，需 --apply 才写入。

用法：
  python3 enable-proxy-for-desktop.py            # 仅检查（dry-run）
  python3 enable-proxy-for-desktop.py --apply    # 确认安全后写入
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


CFG = Path(os.path.expanduser("~/.codex/config.toml"))
PROXY_URL = "http://127.0.0.1:8899/v1"
DIRECT_URL = "https://your-gateway.example.com/v1"
PROBE = "http://127.0.0.1:8899/"


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""


def desktop_pids() -> list[str]:
    """返回主应用进程（而非 Renderer/Helper）。

    主进程命令行以 .../MacOS/ChatGPT 结尾；Renderer 等在 Frameworks 下，
    它们的环境变量继承自主进程，检测主进程即可代表应用状态。
    """
    out = sh(["ps", "-ax", "-o", "pid=,command="])
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid, cmd = parts
        # 主应用二进制：路径以 /MacOS/ChatGPT 结尾
        if cmd.endswith("Contents/MacOS/ChatGPT") and "Frameworks" not in cmd:
            pids.append(pid)
    return pids


def desktop_has_noproxy(pid: str) -> bool:
    out = sh(["ps", "eww", "-p", pid])
    return "NO_PROXY=" in out


def current_base_url() -> str:
    try:
        txt = CFG.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    m = re.search(r"\[model_providers\.[^\]]+\](.*?)(?=\n\[|\Z)", txt, re.S)
    for seg in re.findall(r"\[model_providers\.[^\]]+\](.*?)(?=\n\[|\Z)", txt, re.S):
        mm = re.search(r"base_url\s*=\s*['\"]([^'\"]+)['\"]", seg)
        if mm:
            return mm.group(1)
    return ""


def main() -> int:
    apply = "--apply" in sys.argv

    print("== 1) 代理可达性 ==")
    code = sh(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
               "--noproxy", "*", "-m", "8", PROBE]).strip()
    print("   GET / ->", code or "(无响应)")
    if code != "200":
        print("   [STOP] 代理不可用，不修改配置。")
        return 1

    print()
    print("== 2) 系统级 NO_PROXY ==")
    np = sh(["launchctl", "getenv", "NO_PROXY"]).strip()
    print("   launchctl NO_PROXY =", np or "(空)")
    if "127.0.0.1" not in np:
        print("   [STOP] 系统级 NO_PROXY 未设置。请先加载 LaunchAgent：")
        print("          launchctl load ~/Library/LaunchAgents/com.user.codex-noproxy.plist")
        return 1

    print()
    print("== 3) 桌面进程是否已继承 NO_PROXY ==")
    pids = desktop_pids()
    if not pids:
        print("   未检测到桌面进程（可能未运行）")
        safe = False
    else:
        states = [(p, desktop_has_noproxy(p)) for p in pids]
        for p, ok in states:
            print("   pid=%-8s NO_PROXY=%s" % (p, "yes" if ok else "NO"))
        safe = all(ok for _, ok in states)

    print()
    print("== 4) 当前 base_url ==")
    cur = current_base_url()
    print("   ", cur)

    print()
    print("== 结论 ==")
    if not safe:
        print("   桌面进程尚未继承 NO_PROXY。请**完全退出并重启 WorkBuddy Desktop**，")
        print("   然后重跑本脚本。当前配置未修改（桌面端直连，安全）。")
        return 2

    if cur and "127.0.0.1" in cur:
        print("   已在代理模式，无需修改。")
        return 0

    if not apply:
        print("   安全条件已满足。加 --apply 即可把 base_url 改为：")
        print("     ", PROXY_URL)
        return 0

    # 备份后写入
    bak = str(CFG) + ".bak-enable-desktop-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CFG, bak)
    txt = CFG.read_text(encoding="utf-8")
    new = re.sub(
        r"(\[model_providers\.[^\]]+\][^\[]*?base_url\s*=\s*['\"])([^'\"]+)(['\"])",
        lambda m: m.group(1) + PROXY_URL + m.group(3) if "my_gateway" in "" else m.group(0),
        txt,
    )
    # 上面的通用替换不可靠，改为定位段内替换
    def repl(m):
        seg = m.group(0)
        if "my_gateway" in seg.split("base_url")[0]:
            return re.sub(r"base_url\s*=\s*['\"][^'\"]+['\"]",
                          "base_url = '%s'" % PROXY_URL, seg, count=1)
        return seg
    new = re.sub(r"\[model_providers\.[^\]]+\](?:(?!\n\[)[\s\S])*", repl, txt)
    if PROXY_URL not in new:
        print("   [FAIL] 定位 my_gateway 失败，未修改。备份：", bak)
        return 1
    CFG.write_text(new, encoding="utf-8")
    print("   已写入。备份：", os.path.basename(bak))
    print("   回读 base_url =", current_base_url())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
