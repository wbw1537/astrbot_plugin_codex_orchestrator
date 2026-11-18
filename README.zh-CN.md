# AstrBot Codex 编排插件

[English](README.md) | 简体中文

社区预览版 **0.1.3**。让 AstrBot Agent 将任务交给指定环境中的原生 Codex
App Server 执行。指令、技能、MCP、沙箱及上游登录仍由执行主机上的 Codex 负责；
插件不包含 CLI，也不把 Codex 的上游模型凭据搬到 AstrBot。

## 功能与边界

- 支持本地 stdio 和经过认证的远程 WebSocket 原生 JSON-RPC。
- 管理员配置端点、工作区别名和身份白名单；默认仅管理员可用。查询、续接、
  steer、取消和审批均检查原用户、会话以及当前环境权限。
- SQLite 持久化任务、交互和结果；工作区串行；支持原线程续接、显式 steer，
  返回原生完成状态、计划、diff、用量及命令证据。
- 支持人工命令/文件审批、非机密问题和受限的 MCP 表单；权限授予、未知和机密
  交互均保守拒绝，LLM 不能替人审批。
- Codex 向插件推送事件；`codex_task` 可等待最多 30 秒，之后仍可查询持久化结果。

**尚未实现**超时/重启后自动唤回 Agent，以及自动向 IM 发送 outbox 结果。
WebSocket 仍为实验性；stdio 的断线恢复能力较弱。派发结果不明确时不会盲目重提任务。
产物仅返回引用，不自动下载。端点具有执行用户的权限，不是多租户隔离边界；
工作区别名不等于文件系统沙箱，插件会检查实际生效的原生策略。

实测基线为 **AstrBot 4.29.4、Codex CLI 0.162.1、Python >=3.10**。
其他版本尚未得到同等程度的真实环境验证。

## 一、准备执行主机

按[官方安装指南](https://learn.chatgpt.com/docs/codex/cli)在实际运行任务的机器上
安装 Codex CLI，以将来运行服务的用户登录。
已有的原生登录可以直接复用：

```sh
codex --version
codex login status
# 如果尚未登录，交互式完成原生登录：
codex login
mkdir -p "$HOME/codex-project"
```

工作区也可以选择自己的项目/worktree，绝对路径必须对这个 Codex 用户可用。
原生登录文件保留在执行主机，不要复制给 AstrBot。

远程模式使用独立的 App Server 访问令牌：

```sh
umask 077
mkdir -p "$HOME/.config/codex-astrbot"
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$HOME/.config/codex-astrbot/server.token"
chmod 600 "$HOME/.config/codex-astrbot/server.token"
```

以同一用户启动原生服务，将示例地址换为执行主机的真实内网地址：

```sh
codex app-server \
  --listen ws://192.168.10.20:14550 \
  --ws-auth capability-token \
  --ws-token-file "$HOME/.config/codex-astrbot/server.token"
```

试用时保持终端运行。需要常驻时，让进程管理器以同一用户运行这条命令。
只有 AstrBot 与服务位于同一网络命名空间时，才使用 `127.0.0.1`。
非回环 `ws` 是明文通信，必须显式启用后文的私网选项，仅用于可信私网或已有隧道，
不要增加公网转发。`wss` 需要自行提供经过认证的 TLS 端点，保留 WebSocket 升级和
Authorization 头。

将**仅这个端点令牌**安全复制到 AstrBot 服务可读的位置，文件权限为 0600，
父目录也应限制访问。它赋予 Codex 用户级别的执行权限，不要粘贴到聊天或 URL。
不要把它与模型 API key 或原生登录文件混用。

## 二、安装与配置 AstrBot

在 AstrBot 插件管理中通过本仓库地址安装。插件管理器安装 `requirements.txt`；
手工安装时，应在 AstrBot 的 Python 环境安装依赖。

先使用默认的空环境列表安装插件。面板支持配置 JSON 编辑时可直接配置；
如果面板不能编辑对象列表，先等进行中的任务结束，停用本插件，再编辑专用文件：

```text
data/config/astrbot_plugin_codex_orchestrator_config.json
```

将文件内容替换为以下示例，修改地址和路径后重新启用/重载插件。
这不是 AstrBot 主配置 `cmd_config.json`：

```json
{
  "database": "data/plugin_data/astrbot_plugin_codex_orchestrator/codex-orchestrator.sqlite3",
  "environments": [{
    "name": "dev",
    "host_label": "devbox",
    "transport": "websocket",
    "url": "ws://192.168.10.20:14550",
    "private_ws_opt_in": true,
    "token_file": "/AstrBot/data/plugin_data/astrbot_plugin_codex_orchestrator/server.token",
    "workspaces": {"repo": "/home/your-user/codex-project"},
    "admins": [],
    "allowlist": [],
    "sandbox": "workspace-write",
    "approval": "on-request"
  }]
}
```

空白名单仅允许 AstrBot 管理员。普通用户条目必须精确匹配完整 actor key：
`platform_id:UMO:user_id`，其中 UMO 本身含分隔符。不要只用群聊 UMO 放行整群。
首次试用建议保持管理员默认配置；任务和结果仍绑定原用户及 conversation ID。

`host_label` 是可选的 Codex 执行主机标签，填写可在对话里显示的名称。Agent 工具在
`execution` 中提供连接方式和该标签。工作区别名用于选择 Codex 执行任务的项目目录。

### Docker 路径关系

| 配置 | 在哪里解析 |
|---|---|
| `workspaces.repo` | Codex 执行主机 |
| `token_file` | AstrBot 进程/容器内部 |
| `database` | AstrBot 工作目录，应放在持久化 `data/plugin_data/astrbot_plugin_codex_orchestrator/` 中 |
| stdio 的 `command` | AstrBot 进程/容器内部 |

如果数据挂载为 `HOST_DATA:/AstrBot/data`，上述令牌应放在 AstrBot 宿主机的
`HOST_DATA/plugin_data/astrbot_plugin_codex_orchestrator/server.token`，容器内对应
`/AstrBot/data/plugin_data/astrbot_plugin_codex_orchestrator/server.token`。确保 AstrBot 的 UID 可读且权限为 0600。
只复制端点令牌，不复制 Codex 登录文件。容器内的 `127.0.0.1` 不代表宿主机或远程主机。

首次启动时会自动创建数据库的父目录。0.1.2 将默认数据库位置改为上述插件专属
数据目录。已保存的 `database` 配置会保留；已有安装应先停用插件，备份并迁移原有
SQLite 数据库，再更新配置。仅修改路径会创建空的任务库。

### 可选：本地 stdio

仅当 AstrBot 进程本身能找到 Codex CLI 和原生登录时，将环境条目改为：

```json
{
  "name": "local",
  "transport": "stdio",
  "command": ["codex", "app-server"],
  "workspaces": {"repo": "/workspace/project"},
  "admins": [],
  "allowlist": [],
  "sandbox": "workspace-write",
  "approval": "on-request"
}
```

只在 Docker 宿主机安装 CLI 不会使容器内也能使用它。环境不一致时使用远程模式。

## 三、提交与查看结果

在已建立 AstrBot 对话的管理员会话中发送：

```text
/codex environments
/codex submit dev repo 创建 hello.txt，写入 hello from AstrBot，再读回确认
/codex status dev TASK_ID
/codex result dev TASK_ID
```

也可以让 Agent 依次调用 `codex_environments`、`codex_submit`、
`codex_task(env="dev", task_id="...", wait_seconds=30)`。工具通过显式 schema 声明
必填参数、整数游标和等待/分页限制。另有 `codex_sessions`、`codex_steer`、
`codex_cancel`。出现待处理交互时，`codex_task` 会提前返回，给出审批或回答命令及所需格式，
提示 Agent 转告原用户并保留任务。用户处理后，可以发送回包给出的状态查询命令，或另发消息
让 Agent 查询。`codex_cancel` 返回 `cancelling` 时，请用 `codex_task` 查到
`interrupted` 后再确认任务已取消。

`status` 每页显示一项待处理交互，审批信息优先于冗长进度。
`result` 分页显示最终文本。按响应中的 `nextStatusCommand` 或
`nextResultCommand` 继续读取；OFFSET 分别是待处理交互序号或最终文本字符偏移。
处理交互后待处理列表已变化，请从 OFFSET 0 刷新状态。
响应始终是 3,800 字节内的有效 JSON。Agent 工具保留完整结构化接口：
`cursor`/`limit` 分页读取有界事件，`interaction_offset` 分页读取交互。
原生命令退出码属于观测证据，不等于模型声称某项特定检查已通过。

人工交互必须来自原用户/原会话：

```text
/codex approve dev TASK_ID INTERACTION_ID
/codex deny dev TASK_ID INTERACTION_ID
/codex approve dev TASK_ID INTERACTION_ID acceptForSession
/codex answer dev TASK_ID INTERACTION_ID {"answers":{"question_id":["value"]}}
```

只能选择服务端实际提供的 decision。MCP 表单的回答格式是
`{"action":"accept","content":{"field":"value"}}`。机密问题留在本地处理。
详情截断会明确标记：先用 `codex_task` 查看更多信息；无法充分审查时拒绝请求。
当前预览版拒绝权限授予。取消只是中断请求，须由原生终态确认，也不会回滚已修改文件。

续接：`/codex resume dev repo THREAD_ID OBJECTIVE`；
追加输入：`/codex steer dev TASK_ID TEXT`；取消：`/codex cancel dev TASK_ID`。
续接必须是同一工作区、用户和会话拥有的线程。

## 常见问题

| 现象 | 检查项 |
|---|---|
| 插件连接失败 | 原生服务运行状态；从 AstrBot 所在网络能否访问地址/端口 |
| HTTP 401 | 两端的端点令牌是否一致；不能拿模型 API key 代替 |
| 找不到令牌或权限不足 | 容器路径、挂载、服务 UID 和 0600 权限 |
| 看不到环境/无权限 | 实际 AstrBot 管理员身份，或完整 actor 白名单 |
| 缺少对话身份 | 先发送一条普通消息，让 AstrBot 建立当前 conversation |
| 换会话后查不到旧任务 | 归属包含 conversation ID，回到原会话查询 |
| stdio 找不到 codex | CLI 必须在 AstrBot 进程环境中可用，含其容器内部 |
| 原生策略不匹配 | 检查服务端配置是否更宽或不同，不要绕过策略校验 |
| 任务结束后没有主动回复 | 让 Agent 查询 `codex_task`；自动唤回尚未实现 |

## 开发

```sh
pip install -e ".[test]"
python -m pytest
ruff check .
ruff format --check .
python -m build
python scripts/build_plugin.py --output dist/astrbot_plugin_codex_orchestrator.zip
```

回归测试使用本地 fixtures，不调用模型服务。真实测试还验证了新建任务、原线程续接、
结果返回 Agent、独立文件/校验和检查，以及跨会话查询拒绝。0.1.1 在隔离进程中额外
验证了实际 AstrBot 的 FunctionTool 注册、schema 和上下文调用；不据此承诺真实故障
恢复或自动唤回。MIT 许可，详见 LICENSE 和 NOTICE。
