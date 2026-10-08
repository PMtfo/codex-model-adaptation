# codex-third-party-model-adaptation

把第三方大模型（DeepSeek / GLM / MiniMax / Qwen / Step）接入 Codex 时的兼容性问题、
复现方法、修复方案与可直接落地的补丁，集中归档在这里。

所有结论都来自**本机实测**（macOS arm64 + Codex Desktop 0.162.0-alpha.2），
并尽量附上对应的上游 issue / PR 作为交叉验证。每条问题都标注了：
**现象 → 根因 → 复现 → 修复 → 验证 → 上游佐证**。

> 适用范围：任何「OpenAI Responses API 兼容」的第三方网关或自建代理，
> 不限于某个特定厂商。

---

## 目录

- [一、问题总览](#一问题总览)
- [二、DeepSeek 适配方案（完整）](#二deepseek-适配方案完整)
  - [D1 DSML 工具调用未被归一化](#d1-dsml-工具调用未被归一化阻断级)
  - [D2 远程压缩端点缺失](#d2-远程压缩端点缺失阻断级)
  - [D3 reasoning effort 档位无区分](#d3-reasoning-effort-档位无区分重要)
  - [D4 hook 上下文插入导致会话永久 400](#d4-hook-上下文插入导致会话永久-400重要)
  - [D5 code-mode host 缺失](#d5-code-mode-host-缺失)
  - [D6 系统代理拦截回环请求](#d6系统代理拦截回环请求统一报-502阻断级易漏诊)
  - [D7 CLI 与 host 版本匹配](#d7cli-与-code-mode-host-版本必须匹配阻断级)
- [三、其他模型适配问题](#三其他模型适配问题)
  - [GLM](#glm)
  - [MiniMax](#minimax)
  - [Qwen](#qwen)
  - [Step](#step)
- [四、通用适配清单](#四通用适配清单)
- [五、工具箱](#五工具箱)
- [六、快速自检](#六快速自检)
- [七、参考](#七参考)

---

## 一、问题总览

| 编号 | 问题 | 影响面 | 严重度 | 本机证据 | 本仓修复 |
|---|---|---|---|---|---|
| D1 | DSML / Anthropic XML 文本协议未被归一化 | **所有第三方** | 阻断 | 已复现（代理修复后转换正常） | 归一化代理 |
| D2 | provider 名 `OpenAI` 触发远程压缩，但网关无 `/responses/compact` | 所有第三方 | 阻断 | 已复现（404 + 400） | 配置修复 |
| D3 | `xhigh` 对 `high` 的提升幅度远小于名称暗示 | 所有第三方 | 提示 | 已实测（6 采样：+14%） | 需上游说明 |
| D4 | hook 插入 `function_call` 与 output 之间 → 会话永久 400 | 所有严格校验网关 | 重要 | 已复现 | hook 修复 |
| D5 | `codex-code-mode-host` 位置错误 → 工具无法执行 | code_mode_only 模型 | 阻断 | 已复现并修复 | 放置到二进制同目录 |
| D6 | 系统代理拦截回环请求，统一报 502 | 任何本地代理方案 | 阻断 | 已复现（无 NO_PROXY 必 502） | 分层部署 + NO_PROXY |
| D7 | CLI 与 host 版本不同源 | code_mode_only 模型 | 提示 | 实测可工作（下调为潜在风险） | 建议同源 |
| G1 | `apply_patch` 返回嵌套对象，与 Codex freeform 契约不符 | **所有第三方** | 重要 | 已复现（**8/8 模型**） | 适配层 |
| G2 | 单 chunk 上游导致 tool_call arguments 翻倍 | GLM 系 | 重要 | 本网关未复现 | 适配层 |
| M1 | 消息顺序校验严格 | MiniMax 系 | 重要 | 已复现（与 D4 同根因） | 勿注入 hook 上下文 |
| Q1 | `<tool_call>` 内 Python 风格调用不被识别 | Qwen 系 | 重要 | 已复现 | 解析器 |
| Q2 | `<function=NAME>` 隐式开头不被识别 | Qwen3-Coder | 重要 | 已复现并修复 | 解析器 |
| Q3 | 连字符 MCP 工具名被截断 | Qwen / MCP | 提示 | 本网关未复现 | 解析器 |

> **证据列说明**：`已复现` = 本机实测确认；`未复现` = 本次测试未触发，不代表上游无此问题；
> `提示` = 严重度经实测下调，不构成阻断。

---

## 二、DeepSeek 适配方案（完整）

### D1：DSML 工具调用未被归一化（阻断级）

#### 现象

模型偶尔不通过原生 `tool_calls` 字段返回工具调用，而是把调用写成 **DSML 文本**混在
assistant `content` 里：

```text
<｜DSML｜ calls>
<｜DSML｜ invoke name="shell">
<｜DSML｜ parameter name="command" string="true">ls -la</｜DSML｜ parameter>
</｜DSML｜ invoke>
</｜DSML｜ calls>
```

注意分隔符是 **`U+FF5C` FULLWIDTH VERTICAL LINE**（俗称全角竖线），不是 ASCII `|`；
而且**开标签和闭标签都被包裹**，所以正则匹配 `<invoke>` / `</invoke>` 的解析器会全部失效。

后果：

1. 网关不认识这段文本 → 原样作为普通消息返回 → **工具根本没被执行**；
2. 模型发完这条就无内容可继续 → **turn 静默结束**；
3. 用户观感就是「任务干到一半停住了」，而且日志里**没有任何报错**。

#### 根因

模型用自家原生文本协议表达工具调用，网关缺少对应的归一化层。
该文本协议不在常见解析器的 tag 集合里（`<tool_call>` / `<invoke>` / `<minimax:tool_call>` 等都不含 DSML）。

#### 复现

强制模型以文本形式输出工具调用，然后检查返回的 `output` 里是 `function_call` 还是 `message`：

```bash
curl -s -X POST "$BASE/v1/responses" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -d '{
    "model": "deepseek-v4.1-flash",
    "input": "Output exactly this text and nothing else:\n\n<｜DSML｜ calls>\n<｜DSML｜ invoke name=\"shell\">\n<｜DSML｜ parameter name=\"command\" string=\"true\">echo X</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n</｜DSML｜ calls>",
    "tools": [{"type":"function","name":"shell","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}]
  }'
```

实测（未修复）：`output` 为 `['message']`，DSML 原文完整泄漏，无 `function_call`。

#### 修复

在本地插入一层**归一化代理**：`tools/dsml_normalize_proxy.py`。

它位于 Codex 与真实网关之间，把 DSML 文本块转成标准 Responses `function_call` 输出项：

```
Codex  →  http://127.0.0.1:8899/v1  →  dsml_normalize_proxy  →  真实网关
```

关键实现要点：

1. **流式与非流式都要处理**。非流式直接改写最终 `output` 数组；
   流式需缓冲 `message` 的 `output_text.delta`，等 `output_item.done` 时一次性判定，
   再补发 `function_call` 系列的四个事件
   （`output_item.added` → `function_call_arguments.delta` → `.done` → `output_item.done`）。
2. **必须同步改写 `response.completed` 里的 `output`**。
   否则客户端以 `completed` 为准，会忽略前面发出的事件，修复看起来「没生效」。
3. **只转换、不伪造**：解析不出工具调用时，退化为「剔除标记后的纯文本」，不猜测参数。
4. 只监听 `127.0.0.1`，不记录任何凭证。

启用方式（把 provider 的 base_url 指向本地代理）：

```toml
[model_providers.my_gateway]
base_url = 'http://127.0.0.1:8899/v1'
```

#### ⚠️ 修改 `base_url` 后必须重启桌面应用

Codex Desktop **在进程启动时读取一次** `~/.codex/config.toml`，之后不会热加载。实测证据：

- Desktop 主进程与其 codex core 进程均启动于 18:05，而配置修改于 22:37；
- `lsof` 查该进程句柄，**并未打开 config.toml**；
- 代理日志中只有 `codex_exec`（CLI）来源，无桌面请求。

因此：**修改后需要完全退出并重启桌面应用**，新建对话不会重新加载配置。

重启后可用**一条命令**完成确认与激活：

```bash
python3 tools/enable-proxy-for-desktop.py          # 检查（不修改）
python3 tools/enable-proxy-for-desktop.py --apply  # 安全时才写入
```

脚本会先确认：代理可达 → 系统级 NO_PROXY 已设 → 桌面进程已继承；任一不满足就拒绝修改配置，避免把桌面端推入 502。

重启后用以下脚本确认：

```bash
python3 tools/verify-proxy-live.py
```

它会对比代理日志的请求来源与进程启动时间，给出「已生效 / 尚未生效 / 未启用」三种结论。

#### 验证

| 路径 | 用例 | 修复前 | 修复后 |
|---|---|---|---|
| 非流式 | DSML 文本 | `message`，标记泄漏 | ✅ `function_call`，参数正确 |
| 流式 | DSML 文本 | `message`，标记泄漏 | ✅ `function_call`，`completed.output` 同步 |
| 非流式 | 正常调用 | `function_call` | ✅ 不变 |
| 流式 | 正常调用 | `function_call` | ✅ 不变 |

#### 上游佐证

- **vLLM PR #54686**《Fix DSML leaking for DeepSeek-v4 models》：
  9500 条采样中 76 条泄漏（**0.80%**），并点名
  "the agent (in our case, **claude code/codex**) would probably loop itself"。
  修复思路即「提取 DSML 块 → 剥掉标记前缀 → 交给已有解析器」。
- **vLLM issue #53831**：`tool_choice=auto` 在**深 reasoning 档位（xhigh/max）**下静默丢弃工具调用。
- **ZeroClaw issue #11130**（S1）：复现模型同为 `deepseek-v4.1-flash`，
  且明确指出失败是静默的——runtime 仍记录 `success`。

---

### D2：远程压缩端点缺失（阻断级）

#### 现象

长会话触发上下文压缩时，整轮失败：

```text
Error running remote compact task: 不支持的 Responses input item type: compaction_trigger
```

#### 根因

Codex 依据 provider 的**显示名**决定是否走远程压缩（源码里是纯字符串比较：
`self.name == "OpenAI"`）。当第三方 provider 的 `name` 被写成 `OpenAI` 时，
压缩会走远程端点，而第三方网关普遍没有实现它。

实测端点行为：

| 端点 | 结果 |
|---|---|
| `POST /v1/responses` | 200 |
| `POST /v1/responses/compact` | **404** |
| `POST /v1/responses/input_tokens` | **404** |
| `POST /v1/responses` + `compaction_trigger` | **400 不支持的 input item type** |

#### 修复

**把 provider 显示名改成非 `OpenAI`**，让 Codex 回落到本地压缩：

```diff
 [model_providers.<name>]
 base_url = 'https://your-gateway/v1'
-name = 'OpenAI'
+name = 'My Gateway'
```

本地压缩走普通 `/responses` 请求，第三方网关都支持。

#### 上游佐证

- **openai/codex issue #42313**：Custom provider display name silently controls remote compaction capability
- **openai/codex issue #45393**（open）：第三方 Responses provider 不支持 `compaction` item，
  导致 resume/handoff 永久失败；建议对非 OpenAI provider 剥离该 item
- cockpit-tools issue #2435：完整归因链与实测数据

---

### D3：reasoning effort 档位无区分（重要）

#### 现象

同一问题指定不同 effort，观察 `reasoning_tokens`：

| effort | reasoning_tokens |
|---|---|
| low | 21 |
| high | 54 |
| **xhigh** | **51** |

`low → high` 有明显区分，但 **`high` 与 `xhigh` 基本无差异**，
即界面上选择的「极高」在链路上**没有真正生效**。

**后续多轮采样（每档 6 次，更可信）**：

| 档位 | n | median | mean | 值域 |
|---|---|---|---|---|
| low | 6 | 39.5 | 38.7 | 24-53 |
| high | 6 | 45.0 | 47.0 | 37-60 |
| **xhigh** | 6 | **51.5** | 49.7 | 35-59 |

**修正后的结论**：档位确实有效，但档位间差异远小于名称暗示：
xhigh 相对 low 约 +30%，相对 high 仅 +14%（median 45.0 -> 51.5），
且方差大（xhigh 值域 35-59），日常感知区别不明显。
早期「完全无区别」基于单次采样，已在此修正。

#### 影响

用户以为已启用最高推理强度，实际与 `high` 相同；
配合 [D1](#d1-dsml-工具调用未被归一化阻断级) 的 #53831，深档位反而更容易丢工具调用。

此外还有一类**更彻底**的失效：某些路由层**完全没有把 effort 传到上游**。
此时界面显示 `Max`，但日志里 `Effort: -`，请求体中既无 `requestedEffort`
也无 `reasoningWireField`，在 DeepSeek 与 GLM 上都能复现，主对话与子 agent 路径都受影响。

#### 修复

属于网关/上游映射问题，本地无法根治。建议按顺序排查：

1. **先确认 effort 是否被传递**——对比不同档位的 `reasoning_tokens`，
   若各档位数值几乎相同，说明映射缺失或档位未生效；
2. 网关侧正确转发并映射 `xhigh`（含路由层、子 agent 路径）；
3. 若上游确实没有更高档位，请在模型目录中如实标注可选档位，避免误导用户。

#### 佐证

- opencodex issue #1100：Reasoning effort selected in Codex Desktop is not propagated
  to routed DeepSeek and GLM models

---

### D4：hook 上下文插入导致会话永久 400（重要）

#### 现象

Codex 会把工具类 hook 的输出作为 `role: developer` 消息注入会话。
如果这条消息落在 `function_call` 与其配对的 `function_call_output` **之间**，
严格校验的网关会直接拒绝：

```json
{"error":{"code":400,"message":"function_call 后必须先提供全部 function_call_output"}}
```

更糟的是：一旦出现，该会话**后续所有请求都会失败**，发新消息或 resume 都无法恢复。

#### 最小复现

```json
{"input": [
  {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run echo HI"}]},
  {"type": "function_call", "call_id": "call_TEST", "name": "shell", "arguments": "{\"command\":\"echo HI\"}"},
  {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "hook context"}]},
  {"type": "function_call_output", "call_id": "call_TEST", "output": "HI"}
]}
```

对照组：去掉中间那条 `developer` 消息，返回 200。

#### 修复

**让工具类 hook 不要再向对话注入 `additionalContext`**，改为写入本地审计文件。

以「连续失败三次后阻止重试」的门禁为例——安全强度依赖的是 `PreToolUse` 阶段的
`permission: deny`（在工具发起前就拦截），而不是事后注入的提示文字。
因此移除注入不会削弱门禁：

```diff
 if count >= LIMIT:
-    return {
-        "hookSpecificOutput": {
-            "hookEventName": "PostToolUseFailure",
-            "additionalContext": "同一目标工具已连续失败 N 次……",
-        }
-    }
+    # 不再注入对话，避免破坏 function_call / function_call_output 配对
+    _write_audit(payload, count)
 return _allow()
```

#### 上游佐证

- DSCodex issue #9：DeepSeek Responses API rejects requests when hook context is inserted
  between a function_call and its output（明确记录「一旦出现，该会话所有后续请求都失败」）

---

### D5：code-mode host 缺失或位置错误（阻断级）

#### 现象

`tool_mode = code_mode_only` 的模型依赖 `codex-code-mode-host`，
缺失时报错，**所有工具调用都无法执行**：

```text
ERROR codex_core::tools::router: error=failed to spawn code-mode host   <DIR>/codex-code-mode-host: No such file or directory (os error 2)
```

#### 根因：查找位置是「codex 二进制所在目录」，不是 PATH

**这是最容易踩错的一点。** 报错里的 `<DIR>` 并非 `~/.local/bin`，
而是**实际被执行的 codex 二进制所在目录**。

典型陷阱：

- 把 host 软链到 `~/.local/bin/codex-code-mode-host` → **无效**，仍报同样的错；
- 只有当 host 与真实二进制同目录时才会被找到。

如果 `~/.local/bin/codex` 是 wrapper 脚本（例如为了注入 `NO_PROXY`），
那么真正执行的是 wrapper 里 `exec` 的目标，host 必须放在**那个目录**。

#### 诊断

先看清报错里的路径，再验证它是否存在：

```bash
# 找出真实二进制（若是 wrapper，看它的 exec 目标）
file -L "$(which codex)"
grep -n 'exec ' "$(which codex)" 2>/dev/null

# 确认 host 是否在二进制同目录
ls -la "<DIR>/codex-code-mode-host"
```

#### 修复

把 host 放到**真实二进制同目录**：

```bash
ln -sfn "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex-code-mode-host"         "$HOME/.local/codex-0.153.0/codex-code-mode-host"   # 换成你的真实目录
```

#### 验证

跑一次带工具调用的任务，确认出现 `command_execution` 且 `exit_code=0`：

```text
{"type":"item.completed","item":{"type":"command_execution",
  "command":"/bin/zsh -lc 'echo P5-FIXED'","aggregated_output":"P5-FIXED
","exit_code":0}}
```

#### 注意：版本也要匹配

host 与 CLI **大版本不一致时同样会失败**（host 能启动但协议不兼容）。
优先使用同一发行来源的同版本二进制，参见 [D7](#d7cli-与-code-mode-host-版本必须匹配阻断级)。

---

### D6：系统代理拦截回环请求，统一报 502（阻断级，易漏诊）

#### 现象

把 provider `base_url` 指向本地归一化代理（`http://127.0.0.1:8899/v1`）后，
客户端每次请求都失败：

```text
unexpected status 502 Bad Gateway: Unknown error, url: http://127.0.0.1:8899/v1/responses
```

**最容易误判的一点**：代理自身的日志里**没有任何请求记录**，
而用 `curl` 直接访问同一个地址却完全正常（200）。
于是很容易得出「代理写错了 / 端口没通」的错误结论。

#### 根因

macOS 系统代理（Clash / Surge 等）会拦截**回环地址**请求。
即使系统代理的例外列表里已经写了 `127.0.0.1`，部分程序化 HTTP 客户端仍会走代理，
而代理本身拒绝转发到本地端口，于是返回 502。

踩坑点：**指向一个完全不存在的端口（如 9999）时，报错完全相同**（都是 502），
因此无法靠错误码区分「代理没通」和「请求被系统代理吃掉了」。

#### 诊断

对照实验即可定位（关键：一个走系统代理，一个不走）：

```bash
# A. 经系统代理访问本地服务
curl -x http://127.0.0.1:7897 -m 12 -o /dev/null -w 'via-proxy  HTTP=%{http_code}
' http://127.0.0.1:8899/

# B. 绕过系统代理直连
curl --noproxy '*' -m 8 -o /dev/null -w 'direct     HTTP=%{http_code}
' http://127.0.0.1:8899/
```

如果 A 返回 502、B 返回 200，即可确认根因。

先查系统代理配置：

```bash
scutil --proxy | grep -E 'HTTP|HTTPS|SOCKS|Port|Enable'
```

#### 修复

**方案一（推荐）：为 CLI 加 wrapper，强制回环直连。**

```bash
#!/bin/zsh
# ~/.local/bin/codex
export NO_PROXY="127.0.0.1,localhost,::1,0.0.0.0${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
exec "/path/to/real/codex" "$@"
```

注意：把原来的 `codex` 软链换成 wrapper 前，先记录原目标路径以便回滚。

**方案二：把本地端口加入系统代理的绕过列表**（在代理软件里设置，含端口）。

**已验证但无效的做法**（避免浪费时间）：

- `launchctl setenv NO_PROXY ...` —— 对**已启动**的进程无效；
- 配置项 `respect_system_proxy=false` —— 实测仍走系统代理，未解决问题。

#### 验证

修复后的决定性证据是**代理日志里出现来自客户端的真实请求**：

```text
POST /v1/responses len=193991 ua=codex_exec/0.153.0 (Mac OS ...; arm64)
```

在此之前日志里只有你自己 curl 的记录。

---


#### 严重度校正（实测）

早期把本项标为「阻断级」，**实测后下调**：

| 项 | 实测数据 |
|---|---|
| CLI 版本 | 0.153.0 |
| host 启动 | **ALIVE**（stdio 模式正常等待输入） |
| CLI 二进制 | 220MB / 2026-09-03 |
| host 二进制 | 65MB / 2026-10-07（**不同源**） |

不同源的组合**能正常工作**：本会话多次工具调用成功
（`command_execution` + `exit_code=0`）即为证据。

**修正后的定位**：版本不一致是**潜在风险**（升级后可能不兼容），
但不构成当前阻断。建议同源获取，不必因此阻止使用。
### D7：CLI 与 code-mode host 版本必须匹配（阻断级）

#### 现象

工具链整体不可用，模型报告 code-mode host 缺失或无法通信：

```text
failed to spawn code-mode host /Users/<user>/.local/bin/codex-code-mode-host:
  No such file or directory (os error 2)
```

#### 根因

`tool_mode = code_mode_only` 的模型依赖 `codex-code-mode-host`，
而该二进制**必须与 CLI 版本匹配**。常见组合错误：

- CLI 是 0.153.0，却把 0.162 的 host 软链过来 —— host 能启动，但协议不兼容；
- 旧版本目录里根本没有 host 二进制。

#### 修复

让 CLI 与 host 同版本。例如使用 Desktop 自带的同版本组合：

```bash
/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex --version
# codex-cli 0.162.0-alpha.2
```

#### 验证

用同版本组合跑一次带工具调用的任务，确认出现 `command_execution` 且 `exit_code=0`。

---

## 三、其他模型适配问题

### GLM

#### G1：`apply_patch` freeform 契约不匹配（重要）

**现象**：Codex 把 `apply_patch` 声明为 **freeform 自定义工具**（原始文本入参），
而 GLM 按常规 function call 返回：

```json
{"type": "function_call", "name": "apply_patch",
 "arguments": {"patch": "*** Begin Patch\n..."}}
```

**后果**：中间层不认这个形状 → 不产生 tool output → 模型反复重试同一补丁直到被手动停止。

**修复**：在适配层加一个窄而 fail-closed 的转换——把
`function_call(name=apply_patch)` 的 `arguments.patch` 取出，
重写成 freeform 调用的原始入参。无法确定形状时保持原样（fail-closed），不猜测。

**本机实测（已复现）**：对同一网关的多个模型发 `apply_patch` 工具声明，
它们**全部**返回嵌套对象形状，而非 Codex 期望的原始文本：

| 模型 | `apply_patch` 参数形状 |
|---|---|
| GLM 5.3 | `{"patch": "*** Begin Patch..."}` |
| GLM 5.3 Flash | `{"patch": "..."}` |
| DeepSeek V4.1 Flash | `{"patch": "..."}` |
| MiniMax M3 | `{"patch": "..."}` |
| Qwen3.8 Flash / Max | `{"patch": "..."}` |
| Step 5 Preview | `{"patch": "..."}` |

**全部 8 个受测模型 8/8 均返回嵌套形状**。

**端到端实测（已跑完，结论与初期推断不同）**：

| 场景 | 结果 |
|---|---|
| 不涉及 apply_patch（只跑命令） | ✅ 正常（`echo` 成功） |
| 要求用 apply_patch 创建文件 | ⚠️ 日志出现 `incompatible payload` 并反复重试，**但最终仍成功创建** |

实测文件：`/tmp/g1test/hello.txt` 内容 `G1TEST`；`/tmp/g1ctrl/ctrl.txt` 内容 `CTRL-B`，**两者都已写入**。

日志中的典型报错（可复现）：

```text
ERROR codex_core::tools::router: error=Fatal error: tool exec invoked with incompatible payload
```

**修正后的结论**：

- 嵌套形状确实会引发中间层报错与重试，但**当前网关/客户端组合下最终仍能完成写入**；
- 因此定位为**性能/稳定性隐患**（多次无效重试、耗时），而非硬性阻断；
- 上游 issue #105 描述的「反复重试到被停止」未在本次测试中复现到那个程度；
- 仍建议适配层做形状转换，以消除重试开销。

这说明该问题**不限于 GLM**，而是「三方模型按常规 function call 返回」
与「Codex 把 apply_patch 声明为 freeform」之间的通用契约差异。

**佐证**：CodexHub issue #105

#### G2：单 chunk 上游导致 tool_call arguments 翻倍（重要）

**现象**：GLM/Zhipu 经 OpenAI 兼容层接入 Codex CLI 时，
若上游把 `tool_calls` 的 arguments 在**单个 chunk** 里一次性发完，
而适配层同时走了「累加 delta」和「直接赋值」两条路径，参数会被拼接两次。

**修复**：对单 chunk 上游去重——只在增量路径累加，或在整包路径整体赋值，二者取一。

**佐证**：sub2api PR #3481

---

### MiniMax

#### M1：消息顺序校验严格（阻断级）

**现象**：MiniMax 对消息顺序校验很严，要求 **tool result 必须紧跟对应的 tool call**。
Codex 在某些模式下（尤其 hook 注入、并发工具调用、或子 agent 消息穿插时）
会打破这个顺序，触发上游 `2013` 报错，任务中断。

这与 DeepSeek 的 [D4](#d4-hook-上下文插入导致会话永久-400重要) 是**同类问题的不同表现**：
都源于「严格校验的网关 + 会插入消息的客户端」。

**修复**：

1. 通用做法：禁用工具类 hook 的对话注入（见 D4）；
2. 适配层做法：发送前重排 `input`，保证每个 `function_call_output`
   紧邻其配对的 `function_call`；
3. 若上游支持，改用并行工具调用关闭（`parallel_tool_calls = false`）以减少交错。

**佐证**：MiniMax-M2 issue #69（open）

**本机实测（已复现）**：构造「function_call 与 output 之间插入 developer 消息」后发给 MiniMax M3，返回 `400 function_call 后必须先提供全部 function_call_output`。这与 D4 **同根因** —— 严格校验的网关 + 会插入消息的客户端。因此不是两个独立问题，而是一条通用规则：不要向对话注入 hook 上下文。

---

### Qwen

#### Q1：`<tool_call>` 内 Python 风格调用不被识别（重要）

**现象**：Qwen 会把调用写成 Python 表达式塞进 `<tool_call>` 标签里：

```text
<tool_call>
find_definition(symbol="ToolCallParser")
</tool_call>
```

某些解析器只接受标签内的 **JSON 体**，遇到 Python 风格就返回
`finish_reason=stop` 而不是 OpenAI 的 `tool_calls[]`，工具不执行。

**修复**：解析器在 JSON 之外补一条「Python 调用」分支；
畸形、位置参数、未声明工具等无法确定的情况继续走纯文本兜底（fail-closed）。

**佐证**：macprovider PR #160

**本机实测（已复现）**：强制 Qwen3.8 Max 输出 Python 风格调用后，返回 `output` 类型为 `['message']`，未转为 function_call，与上游描述一致。

#### Q2：`<function=NAME>` 隐式开头不被识别（重要，高频）

**现象**：Qwen3-Coder 的系统提示允许模型在工具调用前先说一句推理。
一旦模型说了这句，它**常常省略 `<tool_call>` 开头**，只留下：

```text
我先看一下这个符号的定义。
<function=find_definition>
<parameter=symbol>ToolCallParser</parameter>
</function>
</tool_call>
```

如果解析器只认字面量 `<tool_call>` 作为触发点，整个块就被当作**普通文本**放行：
**没有报错、没有日志，工具调用直接消失**。

值得注意的是：**工具越多、提示越长，模型越倾向于加这句前言**，因此该问题在大规模工具集下更容易出现。

**修复**：把裸的 `<function=` 也当作合法的（隐式）起始标记，与 `<tool_call>` 等价处理。

**佐证**：

- ollama PR #18538：`recognize <function= as an implicit qwen3-coder tool-call opener`
- llama.cpp issue #26987：延迟触发条件在同时缺失 `<tool_call>` 与 `<function=` 时永不触发
- odysseus issue #6412（open）：`<function=NAME>` 标记未被解析，文本模式工具调用永不执行
- sglang PR #42579：同类解析器的正则扫描性能修复

#### Q3：连字符 MCP 工具名被截断（重要）

**现象**：MCP 工具名常带连字符（如 `mcp-server__tool-name`）。
按 `_` 分词或只匹配 `\w+` 的解析器会把名称截断，导致调用不存在或名称错误的工具。

**修复**：工具名允许的字符集需覆盖 `-`；解析后再与实际注册的工具名做校验。

**佐证**：macprovider PR #844

---

### Step

#### S1：未见公开的 Codex 专用适配问题

本机实测：`step-5-preview` 的基础工具调用**正常**
（返回标准 `function_call`，无文本协议泄漏）。

同样地，`qwen3.8-max`、`glm-5.3-flashx` 在基础用例上正常，
但会额外返回一个 `message` 项（并非错误，客户端需能容忍「message + function_call」并存）。

**待补**：需要更长时间的高并发/长上下文实测才能确认边界问题；
欢迎提交 issue 补充。

---

## 四、CLI 与桌面端的环境差异（关键）

**两者共用同一份 `~/.codex/config.toml`，但环境变量不共享。**
这是搭本地代理时最容易踩坑的地方。

### 差异对照

| 维度 | CLI（`~/.local/bin/codex`） | 桌面应用（Desktop / Automation） |
|---|---|---|
| 读取 `config.toml` | ✅ 每次 | ✅ **仅启动时读一次** |
| 能否拿到 wrapper 里的 `NO_PROXY` | ✅ 能 | ❌ **不能** |
| 遵守系统代理例外列表 | — | ❌ **不遵守**（只认 `NO_PROXY` 环境变量） |

### 实测证据

同一个代理地址，两种调用方式结果相反：

```text
curl 直连          -> HTTP 200   # curl 遵守系统代理的例外列表
原生 Codex 二进制   -> HTTP 502   # 不遵守，必须有 NO_PROXY
加上 NO_PROXY   -> 正常（turn.started）
```

### 推荐部署方式：分层，不要全局改 `base_url`

**不要直接把 `config.toml` 的 `base_url` 指向本地代理**，否则：
桌面端会连代理但拿不到 `NO_PROXY` → 被系统代理拦截 → **502，且会话与
automation 全部失败**（实测确证）。

正确做法：

```text
config.toml            → 直连网关（桌面端 / automation 用，永远安全）
~/.local/bin/codex     → wrapper 注入 -c base_url=代理（仅 CLI 用，自带 NO_PROXY）
LaunchAgent            → launchctl setenv NO_PROXY（持久化，重启后 GUI 继承）
```

模板见 `tools/codex-wrapper.zsh.template`。

### 其他环境差异

- **`hooks.json` 是热加载的**：修改 hook 后无需重启应用（实测：启动于 18:05 的进程调用了 21:18 才加入的 hook）；
- **`config.toml` 不热加载**：改 `base_url` / `model` / `model_provider` 后必须重启应用；
- 可用 `tools/verify-proxy-live.py` 判定改动是否已对桌面端生效。

---

## 五、通用适配清单

接入任何「Responses API 兼容」的第三方模型时，逐项核对：

### 配置层

- [ ] provider `name` **不要**写成 `OpenAI`（否则触发远程压缩，见 D2）
- [ ] `wire_api` 与网关实际实现一致（`responses` 或 `chat`）
- [ ] `model_catalog_json` 与 `model` 属于**同一组**，不要跨组混配
      （跨组调用常见结果是 `422 MODEL_NOT_ALLOWED`）
- [ ] `model_reasoning_effort` 使用该模型目录声明支持的档位

### 工具调用层

- [ ] 确认模型返回的是原生 `function_call`，而非文本协议
      （DSML / `<tool_call>` / `<function_calls>` / `<invoke>`）
- [ ] 文本协议要做归一化，且**流式与非流式都要覆盖**
- [ ] 归一化后同步改写 `response.completed` 的 `output`
- [ ] `apply_patch` 这类 freeform 工具的契约要单独适配（见 G1）

### 消息顺序层

- [ ] 不向对话注入 `additionalContext`（会让 `function_call` 与 output 失配，见 D4）
- [ ] 发送前确保 `function_call_output` 紧邻其配对的 `function_call`
- [ ] 关注并发工具调用是否造成交错（必要时关闭 parallel tool calls）

### 运行时层

- [ ] `codex-code-mode-host` 等随版本发布的辅助二进制可解析（见 D5）
- [ ] CLI 与 Desktop 版本一致或至少互相兼容
- [ ] 压缩路径可用：要么网关支持 `/responses/compact`，要么回落本地压缩

---

## 六、工具箱

| 文件 | 用途 |
|---|---|
| `tools/dsml_normalize_proxy.py` | DSML 归一化本地代理（流式 + 非流式） |
| `tools/dsml-guard.py` | DSML 泄漏检测（Stop hook），含误报抑制与防循环 |
| `tools/deepseek-compat-check.py` | 5 项兼容性自检，防回归 |
| `tools/com.user.dsml-normalize-proxy.plist` | 代理的 macOS LaunchAgent（开机自启 + 守护） |

### 启动代理

```bash
python3 tools/dsml_normalize_proxy.py
# 默认监听 127.0.0.1:8899，转发到 https://your-gateway/v1
```

可用环境变量覆盖：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DSML_UPSTREAM` | `https://your-gateway.example.com/v1` | 真实网关地址 |
| `DSML_LISTEN_HOST` | `127.0.0.1` | 监听地址（不建议改） |
| `DSML_LISTEN_PORT` | `8899` | 监听端口 |
| `DSML_PROXY_LOG` | `~/.codex/proxy/dsml-proxy.log` | 日志路径（不含凭证） |

### 关于 dsml-guard 的误报抑制

早期的简易实现只看「消息里是否含 DSML 标记」，会在**模型/agent 正常讨论该问题时误报**。
现在的判定要求同时满足：

1. 剥离代码块与行内代码后，仍存在真正的**块结构**（`invoke` / `parameter` / `tool_calls`），而不只是孤立标记；
2. 标记字符占比超过阈值（即整条消息基本就是个 DSML 块）；
3. 命中的不是「解释性讨论」场景。

并且遵循 fail-open：任何不确定的情况一律放行，绝不锁死对话；
`stop_hook_active` 为真时不再拦截，避免无限循环。

---

## 七、单元测试

DSML 解析器带确定性测试，覆盖标准形态与已知的几类畸形变体：

```bash
python3 tests/test_dsml_parsing.py
```

| 用例 | 期望 | 说明 |
|---|---|---|
| standard | 1 | 标准 DSML 块 |
| mis-closed parameter | 1 | 参数闭合标签被误写（占比最高） |
| two parameters | 1 | 多参数正常闭合 |
| ascii pipe variant | 1 | 全角竖线被替换为 ASCII 竖线 |
| plain text | 0 | 普通文本不应误触发 |
| marker only | 0 | 仅标记、无块结构，不误判 |
| runaway invoke name | 0 | 无法确定工具名时 **fail-closed**，不猜测 |
| anthropic xml | 1 | Anthropic 风格裸 XML（`<function_calls>/<invoke>`，无 DSML 标记） |
| xml opener only | 1 | 孤立 `<invoke>` 开标识别（参数可为空） |

共 **9 个用例**，后两项对应 **Anthropic 裸 XML** 协议（实测发现各模型在强制文本协议时均可能输出，因此代理也需归一化）。流式与非流式均已覆盖。最后一项是设计决定：名称 runaway 时宁可不返回调用，也不猜测一个可能错误的工具名。

---

## 八、快速自检


```bash
python3 tools/deepseek-compat-check.py
```

输出示例：

```text
[PASS] P1 远程压缩       provider name 非 OpenAI（走本地压缩）
[PASS] P2 hook 注入      未注入 additionalContext
[PASS] P3 compaction item 随 P1 一并规避
[PASS] P4 DSML 泄漏      dsml-guard 已注册
[PASS] P5 code-mode host 可解析
合计: 5 项，通过 5，失败 0
```

把该脚本挂到 Codex 的 `SessionStart` hook，即可在每次会话开始时自动校验修复是否仍然生效。

---

## 九、参考

### 上游 issue / PR

| 来源 | 主题 |
|---|---|
| vLLM PR #54686 | Fix DSML leaking for DeepSeek-v4 models（含泄漏率统计） |
| vLLM issue #53831 | 深 reasoning 档位下静默丢弃工具调用 |
| vLLM issue #51914 / #48931 | DSML 包装畸形变体 |
| ZeroClaw issue #11130 | DSML 标记泄漏导致 turn 静默结束（S1） |
| ZeroClaw PR #11135 | normalize DeepSeek DSML marker before parsing |
| openai/codex issue #45393 | 第三方 provider 不支持 `compaction` item |
| openai/codex issue #42313 | provider 显示名隐式控制远程压缩能力 |
| openai/codex issue #37010 | 请求支持按模型配置 remote_compaction |
| MiniMax-M2 issue #69 | 消息顺序校验导致 2013 报错 |
| CodexHub issue #105 | GLM apply_patch freeform 契约适配 |
| sub2api PR #3481 | 单 chunk 上游 tool_call arguments 翻倍 |
| macprovider PR #160 / #844 | Qwen `<tool_call>` / `<function>` 解析 |
| DSCodex issue #9 | hook 上下文插入导致永久 400 |

### 实测环境

- macOS arm64
- Codex Desktop `0.162.0-alpha.2`
- Codex CLI `0.153.0`
- 网关：OpenAI Responses API 兼容
- 测试模型：DeepSeek V4.1 Flash、GLM 5.3 / 5.3 Flash / 5.3 FlashX、
  MiniMax M3、Qwen3.8 Flash / Max、Step 5 Preview

---

## 贡献

欢迎提交新的模型适配问题或修复。请尽量附上：

1. 模型与版本、网关类型；
2. 最小复现（原始请求 + 实际响应）；
3. 期望行为与根因判断；
4. 若已有修复，说明验证方式。

## License

MIT
