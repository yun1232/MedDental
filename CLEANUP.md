# 项目精简说明

本版本从 `deepseek-harness-oral-memory.zip` 最新版本抽取口腔白斑业务，整理为独立 MedDental 应用。原完整项目包保留，可用于回看通用 Harness 功能。精简针对应用源码与启动依赖，不删除已有病例或记忆。

## 删除范围与原因

| 原项目内容 | 处理原因 |
| --- | --- |
| `apps/desktop*` | 桌面客户端、打包、签名、发布均不属于本地网页程序 |
| 原 `apps/web`、`apps/cli` | 通用 Harness Web/CLI 宿主由单端口 Python 入口替代 |
| `apps/plugin-market-generator` | 插件市场生成器与白斑业务无关 |
| `packages/`、`vendor/`、`native/` | 通用插件框架、代码执行、终端、SSH、浏览器控制、桌面原生插件等不再是本程序依赖 |
| `python/` | 通用 Harness SDK 与运行时；白斑业务模块单独完整保留 |
| `website/`、原 `docs/`、`snapshots/`、`benchmarks/` | 通用文档站、宿主快照与性能基准 |
| 原 `scripts/`、发布与 CI 配置 | 通用构建、发布、代码生成流程不再适用于此应用 |
| 原 `package.json`、pnpm 锁文件、TS/Vite/Vitest 配置、Makefile | 本地应用不再需要 Node 工作区和前端构建 |
| 原开发归档、翻译索引、多语言重复说明 | 以当前应用 README 和精简说明替代 |
| `dsh-smoke.mjs` | 验证完整 DSH 宿主的桥接脚本；业务 MCP 协议回放测试仍保留 |
| `doctor-dom-smoke.mjs` | 原脚本依赖 Node/jsdom 并另启 8765 医生服务，统一入口使用 HTTP 回归测试覆盖 |

这里删除的是通用宿主及其可选能力，不再提供通用编码助手、桌面应用、插件市场或终端操作。白斑应用保持四类记忆、问询编排、医生输入、分级检索、效用与低频策略、权限和历史版本；自然语言入口改用小型模型客户端。

## 原样保留的业务文件

以下文件从 `integrations/oral-memory/` 移至 `oral_memory/`，内容保持不变：`store.py`、`retrieval.py`、`extensions.py`、`workflow.py`、`schemas.py`、`server.py`、`doctor_portal.py`、`questionnaire.json`、`prompts/memory-policy.zh.txt`、医生页面三个文件、`MIGRATIONS.txt` 和原 Python 测试与回放夹具。原 MIT 许可证保留。

## 新增或替换

| 文件 | 功能 |
| --- | --- |
| `run.py` | 一个命令启动，绑定本机回环地址，默认端口 8080 |
| `oral_memory/local_app.py` | 患者交流、医生路由、同库读写、请求幂等 |
| `oral_memory/model_client.py` | 可选自然语言提取和回答，无第三方运行依赖 |
| `oral_memory/web/patient.*` | 患者问询、病例摘要和白斑文字表征表单 |
| `.env.example`、`.gitignore` | 模型配置示例；排除密钥、患者数据库和运行数据 |
| `oral_memory/tests/test_local_app.py` | 独立应用的 HTTP 与启动回归测试 |
| `README.md`、`CLEANUP.md` | 当前使用方法与精简边界 |

## 验证范围

46 项 Python 测试通过，包含真实本机 HTTP 和默认 8080 子进程启动；两个页面的 JavaScript 语法检查通过。自然语言提取使用模拟模型验证输入、持久化与失败处理，未连接真实模型。当前环境阻止 Chromium 创建所需 Unix socket，未完成真实浏览器视觉验收，不能将 HTTP 测试视为视觉验证。
