历史记录：患者/医生客户端与旧 SDK 流程已于 2026-11-28 退出当前主流程。现入口为根目录 train.py、test.py，详见 README.zh.md 和 integrations/oral-memory/BATCH.zh.md。

# MedDental 无关功能精简记录

基线：`eebbc3dd6de1e1d167df222e1c3a0eb3e6cadaa8`。本次在前两批已删除 50 个包的基础上再删除 28 个，累计 78 个。删除 3,974 个原有文件，并新增本说明、验证记录和 Web 启动检查；文件数按 Git 源文件计算，不含依赖和构建产物。

## 删除范围

| 类别 | 目录或模块 |
|---|---|
| 桌面应用与发布 | `apps/desktop`、`apps/desktop-host`、`apps/desktop-publisher` |
| 插件市场 | `apps/plugin-market-generator`、`packages/boot/plugin-market`、`packages/experimental/plugin-market-bundle`、`packages/client/ui-plugin-market`、`packages/util/plugin-market-index` |
| 模型开发插件的检查工具与界面 | `packages/extensions/tool-cordis`、`packages/extensions/ui-cordis` |
| 会话分享 | `packages/experimental/liveshare`、`packages/experimental/liveshare-protocol` |
| 外部聊天导入 | `packages/experimental/deepseek-chat-import` |
| 开发调试和统计面板 | `inspector`、`inspector-profile`、`session-inspector`、`client-ui-stats` |
| 产品使用埋点 | `packages/client/product-analytics`、`packages/host/product-telemetry-otel`，以及使用它们的前端事件采集代码 |
| 思考过程翻译 | `cot-translation-bundle`、`client-ui-cot-translation`、`translator` |
| 工具模型自动授权 | `packages/experimental/auto-review`；医疗医生复核保持不变 |
| 启动外部应用 | `packages/host/open-in-app`、`packages/client/ui-open-in-app` |
| 插件创作推荐入口 | `packages/client/ui-entry-customization` |
| 上游开发与发布素材 | 根 `snapshots`、`website`、`benchmarks`，专用测试、适配脚本及命令 |

逐包清单及附加文件记录见 `integrations/oral-memory/medical-refinement.json` 第三批。Git 提交保留删除前版本，未改写历史。

## 保留范围

保留患者 Web 问诊、医生表单、医疗记忆与分级检索、低频降权、逐轮问询、医生纠错和确认；保留网络检索、HTTP 抓取、基本代码执行、Python/NumPy/Pandas、CSV、PDF 与图片预览。

保留 Cordis/vendor、native、MCP、模型适配、会话、来源记录、事务、权限和沙箱。语音、调度、多智能体、SSH 和 SDK 等可能服务后续医疗部署的能力未在本批裁剪。插件市场不再存在，受控的配置装配和疾病扩展底座仍在。

本次不修改疾病字段、问询算法、数据库结构或诊疗规则。目前口腔白斑的疾病适配通用化仍需单独实现；替换问卷并不等于已支持任意疾病。

## 配套修改

- 清理各 manifest 的依赖、浏览器 external、类型路径、编译引用、默认 Cordis 行、可选插件列表及 Remote 接口。
- 移除 Electron 构建步骤，明确声明 Web 快捷键的 Host 编译引用；保留 Host 编译、Host bundle、Client 编译、Client bundle 和 Web 的顺序。
- 更新锁文件，移除不再使用的 Electron 打包补丁及遗留 Office 测试依赖；保留仍使用的补丁与第三方声明。
- 删除已移除功能的测试和埋点断言，保留并运行其余会话、模型选择、界面与构建检查。
- 更新 `oral:check` 的审定哈希；增加对已删除包的源码导入、浏览器 external、默认装配和 bundle patch 引用的检查。

## 本次验证

| 检查 | 结果 |
|---|---|
| 冻结锁文件安装 | 通过 |
| `pnpm build` | 7 个构建阶段通过，包括原生组件、前后端及 Web 产物 |
| `pnpm test:oral` | 39 项 Python 医疗测试、启动参数检查、真实 MCP 桥接、医生表单 DOM/HTTP 和患者预设检查通过 |
| 受影响的前端及构建测试 | 25 个文件，727 项通过 |
| `pnpm test:medical-web` | 使用独立合成患者和临时目录，实际启动 Web，通过本地 token 换取 cookie 后访问真实前端；未调用模型 |
| `pnpm test:framework` | 309 项通过，2 项既有 MCP HTTP 协商测试失败，见下文 |

两项 HTTP 测试位于 `packages/mcp/mcp-client/tests/credentials.spec.ts` 和 `negotiation-lifecycle.spec.ts`，分别涉及带凭据的 HTTP 探测和失败探测的重试。它们已在上一版的 `VERIFICATION-medical.txt` 中列为已知问题；本批没有修改 MCP 实现或这两项测试。此结果不表示全部上游测试通过。

构建环境原先缺少 Node-API 开发头文件，本次使用临时 Node 目录与 `node-api-headers@1.9.0` 完成原生编译；没有向项目添加此依赖，也没有更改原生构建的检查要求。部署环境仍需满足 README 中的 Node、编译器和开发头文件要求。

未进行真实模型问诊或临床效果评估。原有完整上游快照、治理和发布检查不属于本发行版；不要把该发行版的检查结果当作上游完整发行验证。
