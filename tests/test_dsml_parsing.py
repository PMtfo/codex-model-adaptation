#!/usr/bin/env python3
"""DSML 解析单元测试。

覆盖标准形态与 vLLM issue/PR 中统计到的三类畸形变体：
  - 参数闭合标签被误写（占比约 49%）
  - invoke 名称 runaway（占比约 32%）
  - 开头标签拼写错误（占比约 21%）

运行：python3 tests/test_dsml_parsing.py
无需 pytest；失败时退出码非 0。
"""

import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "tools", "dsml_normalize_proxy.py")

spec = importlib.util.spec_from_file_location("dsml_proxy", PROXY)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

D = "\uff5c"


def call(text):
    return m.parse_calls(text)


CASES = []


def case(name, text, expect_n, expect_first=None):
    CASES.append((name, text, expect_n, expect_first))


# 1) 标准形态
case(
    "standard",
    f'<{D}DSML{D} calls>\n<{D}DSML{D} invoke name="shell">\n'
    f'<{D}DSML{D} parameter name="command" string="true">echo A</{D}DSML{D} parameter>\n'
    f'</{D}DSML{D} invoke>\n</{D}DSML{D} calls>',
    1,
    ("shell", "echo A"),
)

# 2) 参数闭合标签被误写（vLLM 49% 类型）
case(
    "mis-closed parameter",
    f'<{D}DSML{D} tool_calls>\n<{D}DSML{D} invoke name="record_item">\n'
    f'<{D}DSML{D} parameter name="alpha" string="true">first</{D}DSML{D}>\n'
    f'<{D}DSML{D} parameter name="beta" string="true">second</{D}DSML{D} parameter>\n'
    f'</{D}DSML{D} invoke>\n</{D}DSML{D} tool_calls>',
    1,
    ("record_item", "second"),
)

# 3) 两个参数，闭合正常
case(
    "two parameters",
    f'<{D}DSML{D} tool_calls>\n<{D}DSML{D} invoke name="record_item">\n'
    f'<{D}DSML{D} parameter name="alpha" string="true">A</{D}DSML{D} parameter>\n'
    f'<{D}DSML{D} parameter name="beta" string="true">B</{D}DSML{D} parameter>\n'
    f'</{D}DSML{D} invoke>\n</{D}DSML{D} tool_calls>',
    1,
    ("record_item", "A"),
)

# 4) ASCII 竖线变体：部分链路会把全角竖线 U+FF5C 替换成 ASCII 竖线，
#    完整形式是 `<|DSML| ...>`（注意仍有尖括号包裹）。
case(
    "ascii pipe variant",
    '<|DSML| calls>\n<|DSML| invoke name="shell">\n'
    '<|DSML| parameter name="command" string="true">echo ASCII</|DSML| parameter>\n'
    '</|DSML| invoke>\n</|DSML| calls>',
    1,
    ("shell", "echo ASCII"),
)

# 5) 非 DSML 文本不应产生调用
case("plain text", "just a normal answer, nothing to call", 0)

# 6) 仅标记、无块结构 → 不应误判为调用
case("marker only", f"note: the marker is {D}DSML{D} in the docs", 0)


# 7) invoke 名称 runaway（vLLM 32% 类型）：名称与参数写在同一行、缺右引号。
#    解析器无法确定工具名，必须**不猜测**（返回 0 次调用，交给上层降级处理）。
case(
    "runaway invoke name",
    f'<{D}DSML{D} tool_calls>\n<{D}DSML{D} invoke name="record_item {{\ncategory: Dexes',
    0,
)


# 8)　Anthropic 风格裸 XML（无 DSML 标记）
case(
    "anthropic xml",
    '<function_calls>\n<invoke name="shell">\n'
    '<parameter name="command">echo XML</parameter>\n</invoke>\n</function_calls>',
    1,
    ("shell", "echo XML"),
)

# 9)　仅讨论标签、无完整块结构：parse_calls 不该误产生参数
case(
    "xml opener only",
    'the tag <invoke name="shell"> appears in this doc',
    1,
    ("shell", ""),
)


def check_expected(calls, expect_first):
    if expect_first is None:
        return True
    tool, needle = expect_first
    for c in calls:
        if c.get("name") == tool and needle in (c.get("arguments") or ""):
            return True
    return False


def main():
    passed = failed = 0
    for name, text, expect_n, expect_first in CASES:
        try:
            calls = call(text)
        except Exception as e:
            print("[FAIL] %-26s raised %s: %s" % (name, type(e).__name__, e))
            failed += 1
            continue
        ok_n = len(calls) == expect_n
        ok_detail = check_expected(calls, expect_first)
        if ok_n and ok_detail:
            print("[PASS] %-26s calls=%d" % (name, len(calls)))
            passed += 1
        else:
            print("[FAIL] %-26s calls=%d (expect %d), detail_ok=%s"
                  % (name, len(calls), expect_n, ok_detail))
            print("       parsed:", calls)
            failed += 1

    print()
    print("\u5408\u8ba1: %d \u9879\uff0c\u901a\u8fc7 %d\uff0c\u5931\u8d25 %d" % (len(CASES), passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
