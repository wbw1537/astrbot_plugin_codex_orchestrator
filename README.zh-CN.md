# AstrBot Codex 编排插件

[English](README.md) | 简体中文

这是 AstrBot 的 Codex App Server 客户端预览版。Codex 执行、指令、技能、MCP 与沙箱策略仍由配置的执行主机负责；插件不包含 Codex CLI，也不管理上游模型凭据。

## 当前功能

- 通过本地 stdio 或管理员配置的 WebSocket 连接原生 App Server。远程连接使用 `wss`；非回环 `ws` 必须显式启用私网/隧道选项。
- 环境、工作区别名和身份白名单；空白名单默认仅管理员可用。每次操作均检查平台、UMO、用户和会话身份。
- SQLite 持久化任务、交互和结果；请求键按环境和用户隔离；同一工作区任务串行；支持状态/结果查询、取消请求和显式 steer。
- 原生命令/文件审批、权限请求拒绝、非机密工具问题及受支持的原始类型/枚举/范围子集的 MCP 表单；不支持的 schema 约束会拒绝。机密问题会直接拒绝且不保存答案；URL elicitation 仅支持拒绝/取消。
- 提供 `/codex` 命令及已注册的 Agent 工具。完成结果写入 outbox，可由原会话显式查询。

## 限制

AstrBot 4.29.4 没有经过验证的稳定公开接口，可安全地让完成结果重新进入原 Agent。本插件不会唤醒其他会话或改写历史。当前通过持久化 inbox/outbox 和显式拉取结果交付；自动发消息需要后续验证适配器。重启后的远程历史恢复为尽力而为，绝不重放有副作用的 start。stdio 的恢复能力较弱。Codex WebSocket 仍属实验性。远端端点是信任边界，不是多租户沙箱；别名不会限制文件系统访问，必须核验生效的原生策略。远端产物只返回引用，不自动下载。

| 功能 | 状态 |
|---|---|
| stdio 与 WebSocket App Server 通信 | 预览支持 |
| ACL、工作区别名、状态、取消、steer | 预览支持 |
| 原生审批和问题处理 | 预览支持，仅人工响应 |
| 持久化任务/outbox 与手动拉取结果 | 预览支持 |
| 自动重新进入 AstrBot Agent | 暂缓；4.29.4 已验证公开 API 不支持 |
| Goal、原生队列界面、fork/compact、产物传输、动态工具、多模态 | 暂缓 |

## 配置

可在 AstrBot 中通过本仓库地址安装，也可运行 `python scripts/build_plugin.py --output dist/astrbot_plugin_codex_orchestrator.zip` 生成插件压缩包。安装依赖 `requirements.txt`。在插件设置中配置至少一个环境。英文 README 提供示例配置。请替换工作区和身份；不要把令牌写入 URL 或文档。远程令牌文件应仅供服务账户读取（建议 0600）。Codex 上游登录保留在执行主机。不要公开暴露原生端点。

使用 `/codex environments`、`/codex submit ENV WORKSPACE OBJECTIVE`、`/codex resume ENV WORKSPACE THREAD OBJECTIVE`、`/codex status ENV TASK`、`/codex result ENV TASK` 和 `/codex sessions ENV`。Agent 工具支持查看授权环境/工作区、提交新任务或恢复自有线程、查询状态、取消、steer 及列出会话。`codex_submit` 返回受理回执；`codex_task` 可等待 0–30 秒，并把结构化结果/错误、原生证据或待处理交互返回给调用它的 Agent。其 `cursor`/`limit` 参数分页读取有界进度事件；响应包含 `nextCursor`、`hasMore` 和明确的截断标记。单条序列化进度上限为 4 KiB、整页为 64 KiB（含截断元数据）；最终文本上限为 8,000 UTF-8 字节，最多摘要八条最近命令观测，并标记省略。原生计划、diff、token 用量及命令退出信息独立放在 `nativeEvidence` 中；命令退出码仅为观察结果，不代表某项指定检查已验证通过。待处理交互绑定原始用户和会话；使用 `/codex approve ENV TASK ID`、`/codex deny ENV TASK ID` 或 `/codex answer ENV TASK ID JSON`。工具问题 JSON 为 `{"answers":{"question_id":["value"]}}`，MCP 表单 JSON 为 `{"action":"accept","content":{"field":"value"}}`。LLM 工具不能替用户审批；机密问题必须在本地 Codex UI 处理。

## 开发

Python >=3.10。运行 `pip install -e ".[test]"` 安装开发依赖；测试、格式检查及打包命令见英文 README。回归测试使用本地 fixtures，不调用模型服务。

已验证版本为 AstrBot 4.29.4、Codex CLI 0.162.1，回归测试共 38 项。真实 WebChat 测试验证了新建任务、同线程续接、结构化结果返回 Agent、文件与校验和，以及跨会话查询拒绝。

已知问题：AstrBot 4.29.4 装饰器生成的工具 schema 缺少 `required` 声明。Agent 可能漏传 `env` 等必填参数，收到参数错误后需要补齐重试；显式声明必填参数仍待完善。

MIT 许可，详见 LICENSE 和 NOTICE。
