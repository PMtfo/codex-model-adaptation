#!/usr/bin/env python3
"""在 Codex Stop 时拦截承诺、阶段结果或工具失败导致的提前结束。"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


FOLLOW_UP = (
    "检测到本轮以尚未执行的后续承诺结束。立即继续完成刚才承诺的动作，"
    "不要重复计划或只发进度；完成交付、报告真实阻塞，或确实需要用户输入/授权后再结束。"
)
REPEATED_FOLLOW_UP = (
    "上一次 Stop 已被阻断，但本轮仍以未完成状态结束。不要重复阶段性结论；"
    "立即继续执行和验证。只有完成交付，或明确报告真实阻塞、所需用户输入/授权后才能结束。"
)
STAGE_FOLLOW_UP = (
    "检测到回复仍是阶段性结论或明确包含未完成项，不能作为最终交付。"
    "立即继续剩余动作、测试和回读；只有完成交付或报告真实阻塞后才能结束。"
)
TOOL_FAILURE_FOLLOW_UP = (
    "检测到本会话仍有未恢复的工具失败，或最后回复停在工具报错。"
    "不要把报错或部分结果当成最终交付；沿原方法的正式恢复路径继续，"
    "成功后完成验证和回读，或明确报告真实阻塞后再结束。"
)

TOOL_FAILURE_STATE_PATH = Path(
    os.environ.get(
        "CODEX_TOOL_FAILURE_GUARD_STATE",
        "~/.codex/hooks/state/tool-failure-guard.json",
    )
).expanduser()
AUTOMATION_GATE_PATH = Path.home() / ".codex/hooks/automation-recovery-gate.py"

DELIVERY_GATE_STATE_PATH = Path(
    os.environ.get(
        "CODEX_DELIVERY_COMPLETION_GATE_STATE",
        "~/.codex/hooks/state/delivery-completion-gate.json",
    )
).expanduser()
# 同一 turn 内 Stop 连续被本门禁阻断的上限；超过即熔断放行。
# 用户级 hooks.json 与 dynamic-workflow 插件 hooks.json 会在同一次 Stop 各调用
# 本脚本一次，两处共用同一状态文件；没有熔断时"阻断→回灌→再阻断"会无限持续
# （详见本仓 README D8「Stop 交付门禁无熔断导致会话持续中止」）。
MAX_CONSECUTIVE_STOP_BLOCKS = 6

# 只匹配明确的第一人称即时执行承诺，避免把“建议下一步”当成未完成任务。
COMMITMENT_PATTERNS = (
    re.compile(
        r"(?:^|[。！？；;\n])\s*"
        r"(?:我(?:会|将|先|现在|接下来)|接下来(?:我)?(?:会|将|先)|"
        r"现在(?:我)?(?:会|将|先|开始))"
        r".{0,80}(?:读取|检查|核验|调研|搜索|安装|连接|操作|调用|执行|"
        r"实现|修改|修复|恢复|纠偏|重算|替换|删除|写入|测试|验证|校验|"
        r"回读|继续|完成|处理|给出|回传|汇报)",
        re.S,
    ),
    re.compile(
        r"(?:^|[。！？；;\n])\s*"
        r"(?:我继续|继续)"
        r".{0,60}(?:读取|检查|核验|调研|搜索|安装|连接|操作|调用|执行|"
        r"实现|修改|修复|恢复|纠偏|重算|替换|删除|写入|测试|验证|校验|"
        r"回读|完成|处理)",
        re.S,
    ),
    re.compile(
        r"(?:确认|检查|读取|完成|安装|连接|测试|验证|修复)(?:后|完后)，?"
        r"\s*(?:我)?(?:会|将)?(?:直接|立即|再|继续)?"
        r"(?:执行|实现|修改|修复|恢复|纠偏|重算|替换|删除|写入|测试|验证|"
        r"校验|回读|完成|处理|给出|回传|汇报)",
        re.S,
    ),
    re.compile(
        r"(?:^|[。！？；;，,\n])\s*"
        r"(?:(?:我)?(?:目前|当前|现在)?(?:仍|还)?正在|(?:我)?(?:仍|还)在)"
        r".{0,80}(?:读取|检查|核验|调研|搜索|安装|连接|操作|调用|执行|"
        r"实现|修改|修复|恢复|纠偏|重算|替换|删除|写入|测试|验证|校验|"
        r"回读|继续|完成|处理|回传|汇报)",
        re.S,
    ),
)

# 明确边界始终放行。门禁宁可漏判，也不打断授权、安全或真实阻塞边界。
BOUNDARY_PATTERNS = (
    re.compile(
        r"(?:需要你|请你|等你|等待你|需由你|需要用户|用户输入|用户确认|"
        r"明确授权|登录后|重新登录|权限被拒绝|没有权限|当前阻塞|唯一阻塞|"
        r"无法继续|不能继续|暂时无法|入口未验证通过)"
    ),
    re.compile(
        r"(?:我不能协助|我无法协助|不能帮助|无法帮助|出于安全原因|"
        r"不能提供|无法提供)"
    ),
    re.compile(
        r"(?:后台任务|正在后台|仍在运行|已经启动).{0,100}"
        r"(?:session(?:[_ -]?id)?|进程|PID|可轮询|轮询句柄|任务 ID)",
        re.I | re.S,
    ),
)

# 完成信号只有出现在最后一项执行承诺之后才算本轮终态，避免“已完成读取，接下来实现”漏拦截。
COMPLETION_PATTERN = re.compile(
    r"(?:已完成|已经完成|已完成交付|交付完成|已交付|已经交付|已处理|"
    r"已修复|已实现|已写入|已更新|已安装|已连接|已调用|已回传|"
    r"验证通过|测试通过|回读通过)"
)

# 这些表达只覆盖明确的阶段态或尚未完成态；普通建议和纯知识回答不命中。
INCOMPLETE_PATTERNS = (
    re.compile(r"(?:阶段性|初步|暂时)(?:结论|结果|进展|发现|判断)"),
    re.compile(
        r"(?:尚未|还未|仍未|没有|未)(?:完成|执行|测试|验证|回读|交付|处理|"
        r"修复|实现|写入|获取|拿到|形成|结束)"
    ),
    re.compile(r"(?:剩余|后续|下一阶段).{0,40}(?:待|需要|尚未|还未|仍未|未完成)"),
)

TOOL_ERROR_PATTERNS = (
    re.compile(
        r"(?:工具|命令|调用|接口|runner|脚本).{0,40}"
        r"(?:报错|失败|异常|超时|连接被重置|未返回|不可用)",
        re.I | re.S,
    ),
    re.compile(
        r"(?:报错|错误|异常|失败|超时).{0,60}"
        r"(?:部分|阶段性|初步|尚未|还未|仍未|未完成)",
        re.S,
    ),
)


def _allow() -> dict[str, Any]:
    return {}


def _stop_block_key(payload: dict[str, Any]) -> str:
    # 仅用 session_id：死循环中 turn_id 是否稳定未知，而 session_id 已被
    # automation-recovery-audit 证明在同一中止会话内恒定，用它计数才能可靠熔断。
    return str(payload.get("session_id") or payload.get("sessionId") or "")


def _read_delivery_gate_state() -> dict[str, Any]:
    try:
        raw = json.loads(DELIVERY_GATE_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {"blocks": {}}
    if not isinstance(raw, dict):
        return {"blocks": {}}
    blocks = raw.get("blocks")
    return {"blocks": blocks if isinstance(blocks, dict) else {}}


def _write_delivery_gate_state(state: dict[str, Any]) -> None:
    blocks = state.get("blocks")
    if not isinstance(blocks, dict):
        blocks = {}
    if len(blocks) > 500:
        blocks = dict(list(blocks.items())[-250:])
    try:
        DELIVERY_GATE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DELIVERY_GATE_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"blocks": blocks}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, DELIVERY_GATE_STATE_PATH)
    except OSError:
        pass


def _stop_blocks_exceeded(payload: dict[str, Any]) -> bool:
    state = _read_delivery_gate_state()
    count = state.get("blocks", {}).get(_stop_block_key(payload))
    try:
        return int(count or 0) >= MAX_CONSECUTIVE_STOP_BLOCKS
    except (TypeError, ValueError):
        return False


def _increment_stop_block_count(payload: dict[str, Any]) -> None:
    state = _read_delivery_gate_state()
    blocks = state.get("blocks", {})
    key = _stop_block_key(payload)
    try:
        current = int(blocks.get(key) or 0)
    except (TypeError, ValueError):
        current = 0
    blocks[key] = current + 1
    state["blocks"] = blocks
    _write_delivery_gate_state(state)


def _reset_stop_block_count(payload: dict[str, Any]) -> None:
    state = _read_delivery_gate_state()
    blocks = state.get("blocks", {})
    key = _stop_block_key(payload)
    if key in blocks:
        blocks.pop(key, None)
        state["blocks"] = blocks
        _write_delivery_gate_state(state)


def _automation_recovery_output(payload: Any) -> dict[str, Any]:
    """复用已加载的 Stop 入口热桥接 automation 恢复门禁。"""
    if not isinstance(payload, dict):
        return {}
    try:
        spec = importlib.util.spec_from_file_location(
            "automation_recovery_gate", AUTOMATION_GATE_PATH
        )
        if not spec or not spec.loader:
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = module.evaluate(payload)
        return output if isinstance(output, dict) else {}
    except Exception:
        return {}


def _message(payload: Any) -> str:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return ""
    message = payload.get("last_assistant_message")
    if not isinstance(message, str):
        return ""
    message = message.strip()
    return message if message and len(message) <= 2_000 else ""


def _has_boundary(message: str) -> bool:
    return any(pattern.search(message) for pattern in BOUNDARY_PATTERNS)


def _last_match_start(patterns: tuple[re.Pattern[str], ...], message: str) -> int:
    starts = [match.start() for pattern in patterns for match in pattern.finditer(message)]
    return max(starts, default=-1)


def _completion_after(message: str, position: int) -> bool:
    matches = list(COMPLETION_PATTERN.finditer(message))
    return bool(matches and matches[-1].start() > position)


def has_recorded_tool_failure(payload: Any) -> bool:
    """检查工具失败门禁记录中是否仍有当前 session 的未恢复失败。"""
    if not isinstance(payload, dict):
        return False
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    if not session_id:
        return False
    try:
        state = json.loads(TOOL_FAILURE_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    if not isinstance(state, dict):
        return False
    return any(
        isinstance(entry, dict)
        and str(entry.get("session_id", "")) == session_id
        and int(entry.get("count", 0)) > 0
        for entry in state.values()
    )


def has_unfinished_commitment(payload: Any) -> bool:
    """判断最后回复是否以尚未执行的即时承诺结束。"""
    message = _message(payload)
    if not message:
        return False

    if _has_boundary(message):
        return False
    last_commitment_start = _last_match_start(COMMITMENT_PATTERNS, message)
    if last_commitment_start < 0:
        return False
    return not _completion_after(message, last_commitment_start)


def unfinished_reason(payload: Any) -> str | None:
    """返回本轮仍未完成的高置信原因；明确完成或边界优先放行。"""
    message = _message(payload)
    if not message or _has_boundary(message):
        return None

    # 失败记录只能由同目标成功调用清零，不能用自然语言完成声明覆盖。
    if has_recorded_tool_failure(payload):
        return TOOL_FAILURE_FOLLOW_UP

    last_incomplete = _last_match_start(INCOMPLETE_PATTERNS, message)
    last_tool_error = _last_match_start(TOOL_ERROR_PATTERNS, message)
    last_commitment = _last_match_start(COMMITMENT_PATTERNS, message)
    last_pending = max(last_incomplete, last_tool_error, last_commitment)

    if last_pending >= 0 and not _completion_after(message, last_pending):
        if payload.get("stop_hook_active") is True:
            return REPEATED_FOLLOW_UP
        if last_tool_error >= 0:
            return TOOL_FAILURE_FOLLOW_UP
        if last_incomplete == last_pending:
            return STAGE_FOLLOW_UP
        return FOLLOW_UP

    return None


def should_block_stop(payload: Any) -> bool:
    return unfinished_reason(payload) is not None


def evaluate(payload: Any) -> dict[str, Any]:
    """返回 Codex Stop Hook wire output；任何不确定或异常输入均放行。"""
    if not isinstance(payload, dict):
        return _allow()

    recovery = _automation_recovery_output(payload)
    if recovery.get("decision") == "block":
        return recovery

    reason = unfinished_reason(payload)
    if reason is None:
        _reset_stop_block_count(payload)
        return _allow()

    # 熔断：同一 turn 内本门禁连续阻断达到上限后放行，打破
    # “阻断→回灌→再阻断”死循环。用户级 hooks.json 与 dynamic-workflow
    # 插件 hooks.json 会对同一次 Stop 各调用本脚本一次，共用同一状态文件。
    if _stop_blocks_exceeded(payload):
        return _allow()
    _increment_stop_block_count(payload)
    return {"decision": "block", "reason": reason}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        output = evaluate(payload)
    except Exception:
        # Stop 门禁必须 fail-open，不能因脚本自身故障锁死对话。
        output = _allow()
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
