# wb2codex

把 **Codex CLI**（只说 OpenAI Responses 协议）接到 **只提供 `/v1/chat/completions`** 的本地网关上。

开箱即用：本包已随附网关 `wb2api.exe` 与管理面板 `wbgui.exe`，**双击 `start-all.cmd` 即可**。

---

## 这解决什么问题

Codex CLI 从 v0.116 起只走 Responses 协议（`/v1/responses`）。而大量自建 / OpenAI 兼容网关只实现了旧的 Chat Completions（`/v1/chat/completions`）。两边说不通，Codex 会直接 404。

本包在中间做翻译：

```
Codex ──Responses──► 7863 桥接 ──Chat Completions──► 7865 网关 ──► 上游
```

**关键设计：不动客户端。**
不需要改 `~/.codex/config.toml`，也不用跟 cc-switch 抢控制权（它每次保存都会把 `wire_api` 强制写回 `responses`，改了必被覆盖）。做法是让服务端适配客户端——把 Codex 已经写死的端口让给桥接，网关后撤一位。

---

## 系统要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11（脚本为 `.cmd` + PowerShell） |
| Python | 3.8+，**仅标准库，无需 pip install** |
| Go | **不需要**——除非你要自己编译网关/面板 |
| Redis | **不需要**——网关缺省走本地文件存储 |

桥接层是纯 Python 标准库实现，没有 FastAPI / uvicorn / requests 之类的依赖。

---

## 快速开始

### 1. 生成配置

```bat
copy config.example.json config.json
copy workbuddy2api-gui\config.example.json workbuddy2api-gui\config.json
```

编辑 `config.json`：

- `listen` 保持 `127.0.0.1:7865`（**不要改成 7863**，那是桥接的端口）
- `api_key` 换成你自己生成的随机串

编辑 `workbuddy2api-gui\config.json`：把 `ui.password` 改掉。

### 2. 把同一个 key 填给桥接

编辑 `start-bridge.cmd`：

```bat
set "BR_KEY=REPLACE_WITH_YOUR_GATEWAY_API_KEY"
```

改成和 `config.json` 里 `api_key` **相同**的值。

### 3. 启动

双击 **`start-all.cmd`**，依次拉起网关 → 桥接 → 面板。

打开 http://127.0.0.1:8787 用面板添加账号（网页 OAuth，不需要命令行）。

停止用 `stop-all.cmd`；改完桥接代码用 **`restart-all.cmd`**（直接 `start-all` 不会重载）。

### 4. Codex 侧

保持默认即可，无需改动：

```toml
model_provider = "openai"
openai_base_url = "http://127.0.0.1:7863/v1"
```

---

## 端口约定

| 端口 | 进程 | 作用 |
|---|---|---|
| 7865 | `wb2api.exe` | 网关，只认 Chat Completions |
| 7863 | `responses_bridge.py` | 协议转换，**Codex 连这个** |
| 8787 | `wbgui.exe` | Web 管理面板 |

---

## 三个坑（这才是本包真正的价值）

普通的协议直译很好写，但真跑起来会撞上这些：

**1. 工具调用序列被打断 → 上游报 `11148 tool_call_sequence_broken`**

Codex 在会话被中断、历史重放或上下文压缩后，会提交带 `function_call` 却没有对应结果的 history。上游严格校验配对，对不上就 503。
本包在出站前做三层整形：合并相邻 assistant 消息、丢弃孤儿结果、摘掉悬空调用、按声明顺序重排结果。

**2. `call_id` 生成两次**

上游流式返回若不提供 tool_call id，`output_item` 事件和 `response.completed` 各自生成随机 id，Codex 回填的结果就对不上。已改为生成一次全程复用。

**3. 所有会话塌缩成同一个缓存键**

网关用 body 里的 `conversation_id` 做 `prompt_cache_key` 的哈希源；缺省时该键对同一账号恒定。桥接若从不发送 `conversation_id`，**所有对话共用一个提示缓存键**，某次对话的脏状态会扩散到后续每一个新会话——这也解释了为什么"开新会话重试"有时并不管用。
本包用「instructions + 首个 user 文本 + 工具名集合」生成会话指纹，写入 `conversation_id`。

每次请求会把消息序列的**形状**（不记录正文）打到 `data/bridge.out.log`，触发修复时显示 `tool-seq repaired: A ==> B`。排查问题先看这个日志。

---

## 排错

| 现象 | 检查 |
|---|---|
| 404 on `/v1/responses` | 桥接没起来。看 `data/bridge.err.log` |
| 503 + `11148` | 看 `data/bridge.out.log` 里有没有 `tool-seq repaired`；仍失败就开一个新 Codex 会话 |
| 7863 端口被占 | `netstat -ano \| findstr :7863`，结束对应 PID 后重启 |
| 面板打不开 | 确认 `workbuddy2api-gui\config.json` 已创建 |

---

## 环境适配提示

> **每个人的环境都不一样——Python 路径、端口占用、网关版本、Codex 版本都可能有差异。**
> 如果遇到本 README 没覆盖的报错，**建议把这份 README 和完整报错信息一起贴给 AI，让它针对你的本地环境给出适配方案**。比自己一条条试要快得多。

---

## 随附二进制

为方便起见，本包随附了两个预编译二进制。它们**不是本项目开发的**，均来自上游 MIT 项目：

| 文件 | 大小 | SHA256 |
|---|---|---|
| `wb2api.exe` | 12.9 MB | `eb44fdd21f39d6820fba384aca6d4cb1f57ba139488f9b4014c39064d691b97f` |
| `workbuddy2api-gui/wbgui.exe` | 11.6 MB | `ce4c8fafafcb5f4baa78dfd36d3e69838b30c3b62f52993225806fe6cef2501a` |

- 来源：**[workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)**（MIT，Copyright (c) 2026 Sliverkiss）
- 面板来源：`workbuddy2api-gui`，同样 MIT
- 上游许可证全文见 `THIRD-PARTY-workbuddy2api.txt` 与 `workbuddy2api-gui/LICENSE-gui.txt`

**不放心预编译二进制的话**：删掉这两个 `.exe`，按上游仓库的说明自行从源码编译（`go build`），目录结构和脚本无需改动。

---

## 致谢与版权

- 网关与面板：见上表，MIT，版权归各自作者。本项目仅做整合与协议适配，不修改其源码。
- `responses_bridge.py` 及本目录下的启动脚本：MIT，见 `LICENSE`。

## 免责声明

本项目仅为**本地协议适配与整合工具**，不提供任何服务、账号或额度。请遵守你所使用的上游服务的条款，并自行承担使用后果。作者不对账号封禁、服务中断或任何数据损失负责。
