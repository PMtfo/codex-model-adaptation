#!/usr/bin/env python3
"""DSML leak detector for DeepSeek-family models (Stop hook).

A real leak means the model emitted its tool call as DSML text instead of a
native tool call, so the tool never ran and the turn ended silently.

This guard must NOT fire when DSML is merely being discussed or quoted
(e.g. an agent explaining the issue, or showing markup inside code fences).
Discrimination rules:
  1. code fences and inline code are stripped before matching
  2. a real leak needs an actual DSML BLOCK (invoke / tool_calls / parameter),
     not just the bare marker
  3. markup must dominate the remaining text (ratio threshold)
Safe by construction: every uncertain case is allowed through.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path


AUDIT_PATH = Path(
    os.environ.get(
        "CODEX_DSML_GUARD_AUDIT",
        "~/.codex/hooks/state/dsml-guard-audit.jsonl",
    )
).expanduser()

FW = "\uff5c"
MARK = rf"(?:{FW}|\|)\s*DSML\s*(?:{FW}|\|)?"

# 真实泄漏必须含“块结构”，而不是光秃秃的标记
BLOCK_RE = re.compile(
    rf"<\s*{MARK}\s*(?:invoke|parameter|tool_?calls?|tool-?calls?)\b",
    re.I | re.S,
)

# 用于计算“标记占比”的所有 DSML 片段
SPAN_RE = re.compile(rf"<\s*/?\s*{MARK}[^>]*>", re.I | re.S)

# 讨论/引用场景的强特征：出现解释性措辞时从严
DISCUSS_HINT_RE = re.compile(
    r"(?:检测到|例如|示例|比如|引用|作为|标记|泄漏|归一化|格式|说明|表示|指的是|所谓)",
)

FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_RE = re.compile(r"`[^`\n]*`")

RATIO_THRESHOLD = 0.35

# 注意：Stop hook 的 reason 会作为 hook_prompt 回灌给模型，不是给用户看的。
# 因此写成对模型的明确指令，且不含裸标记字符，避免自我触发。
RECOVERY_HINT = (
    "检测到本轮以未归一化的 DeepSeek DSML 文本块代替了工具调用：该工具实际未执行，本轮属于无效结束。"
    "请立即改用正常的工具调用机制重新执行刚才未完成的动作，不要输出任何 DSML 文本块，"
    "也不要只回复说明性文字。若同一动作连续两次出现该情况，请停止重试，"
    "直接告知用户此会话需切换到其他模型（例如 gpt-5.6-sol）继续。"
)


def _strip_code(text: str) -> str:
    text = FENCE_RE.sub("", text)
    text = INLINE_RE.sub("", text)
    return text


def leak_ratio(text: str) -> float:
    """DSML 片段字符数 / 其余文本字符数。"""
    spans = SPAN_RE.findall(text)
    if not spans:
        return 0.0
    marked = sum(len(s) for s in spans)
    rest = max(1, len(text) - marked)
    return marked / rest


def is_real_leak(text: str) -> bool:
    stripped = _strip_code(text)
    if not BLOCK_RE.search(stripped):
        return False
    ratio = leak_ratio(stripped)
    if ratio < RATIO_THRESHOLD:
        return False
    # 解释性措辞 + 低占比 -> 讨论；此处占比已过阈值，仍要求块结构占主导
    if DISCUSS_HINT_RE.search(stripped) and ratio < RATIO_THRESHOLD * 2:
        return False
    return True


def _message(payload: dict) -> str:
    msg = payload.get("last_assistant_message")
    return msg if isinstance(msg, str) else ""


def _audit(payload: dict, text: str, ratio: float) -> None:
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": int(time.time()),
            "event": "dsml_leak_detected",
            "session_id": str(payload.get("session_id") or payload.get("sessionId") or ""),
            "ratio": round(ratio, 3),
            "message_len": len(text),
        }
        with AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def evaluate(payload) -> dict:
    """Stop entry point. Always allows on any uncertainty or error."""
    if not isinstance(payload, dict):
        return {}
    if payload.get("hook_event_name") != "Stop":
        return {}
    # 防循环：已触发过一次则不再阻断
    if payload.get("stop_hook_active") is True:
        return {}
    text = _message(payload)
    if not text:
        return {}
    if not is_real_leak(text):
        return {}
    _audit(payload, text, leak_ratio(_strip_code(text)))
    return {"decision": "block", "reason": RECOVERY_HINT}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        output = evaluate(payload)
    except Exception:
        output = {}
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
