# MedDental 医疗专用精简版

以本次上传的 `MedDental-refined(1).zip` 为实际基线重建。上传包没有上一轮未保存的精确删除清单，因此本次的 44 个包是按当前功能边界和依赖核对后重建，不能视为对旧清单逐项复原。

第一批删除 44 个包：办公工具与转换引擎、Office 技能、LSP、外部 Claude/Codex 编程代理与 hooks、Ralph、Forge/GitHub 自动化、worktree、webhook、浏览器与桌面自动操控、创作及插件推荐。第二批删除 6 个包：编程环境提示、tmux 上下文、OAR 外部编程代理及工具、徽标技能及装配。前两批合计 50 个包。完整包名、目录和文件变更见 `integrations/oral-memory/medical-refinement.json`。

| 保留能力 | 实现与变化 |
|---|---|
| 网络检索、网页抓取 | 保留 web、tool-web、DeepSeek 搜索和 HTTP 抓取 provider；患者预设显式启用检索与抓取。搜索仍需要相应 provider 凭据。 |
| 基本代码执行 | 保留 Bash/PowerShell、工作目录、子进程、沙箱、终端及工具执行器。 |
| Python、NumPy、Pandas | 保留 Python runtime 与数值依赖锁定；移除 python-docx、python-pptx、openpyxl、XlsxWriter、et_xmlfile 等办公依赖。 |
| CSV/TSV | 从原 Excel 预览拆出有字节、行数、单元格上限的只读表格；字段作为文字呈现，不执行公式。可使用 Pandas 进一步分析。 |
| PDF、图片预览 | 保留独立 PDF.js、图片读取及预览；移除 Office 转 PDF 与 XLS/XLSX 浏览器解析。 |
| 医疗记忆、问询、医生复核 | 继续使用原有 SQLite、四类记忆、分级检索、低频降权、患者逐轮补全、医生追问与复核；未修改数据库结构。 |
| 框架底座 | 保留 Cordis/vendor、native、MCP、会话与权限机制；同步更新 workspace 依赖、构建引用及锁文件。 |

`pnpm build` 构建保留的运行产物。上游全量治理、快照和发布门禁不属于此发行版的验证范围。根构建脚本使用 `node --import tsx/esm`，避免 tsx CLI 的 IPC 管道在受限环境中失败。SDK 与桌面打包路径中 Office 的接口、侧载下载、必需引擎检查和转换冒烟流程也已移除。第三方声明与仍使用的补丁保留。

## 第三批精简（2026-11-20）

第三批再移除 28 个包，累计 78 个：桌面客户端及发布链、插件市场及索引生成器、模型开发插件的检查工具和界面、会话分享、DeepSeek 聊天导入、开发调试器及统计面板、产品使用埋点、思考过程翻译、工具自动审批、外部应用启动和插件创作推荐入口。移除上游根快照、文档网站、性能基准及对应的专用测试和命令。

保留 Web 患者问诊、独立医生入口，以及语音、多智能体、调度、MCP 和模型适配的可选能力。本次不改疾病字段、数据库结构或诊疗规则；疾病适配层的通用化是后续独立工作。`auto-review` 的删除只涉及工具执行的模型自动授权，与医疗业务的医生确认和纠错无关。

桌面 bundle 不再参与构建；Web 快捷键的 Host 编译依赖已明确声明。依赖清单、锁文件、类型路径、Cordis 装配和前端 Remote 接口同步更新。详细范围及本次实际验证见 [PRUNING.zh.md](PRUNING.zh.md)。

## 使用

需要 Node >=24.9、项目声明版本的 pnpm、Python >=3.10，以及原生编译器和 Node 开发头文件。

```sh
pnpm install --frozen-lockfile
cp .env.oral.example .env.oral
```

填写患者、就诊和医生凭据。数值分析环境需安装 NumPy/Pandas/Pillow，例如 `python3 -m pip install numpy pandas pillow`；可以将 `ORAL_MEMORY_PYTHON` 指向已有虚拟环境解释器，模型通过终端使用该解释器执行分析。

ZIP 不包含 Git 历史；原构建读取 Git 提交信息，首次使用可初始化本地提交：

```sh
git init
git add .
git -c user.name=MedDental -c user.email=meddental@localhost commit -m "Import medical source"
pnpm build
pnpm oral:patient
```

另开终端运行 `pnpm oral:doctor`，访问 `http://127.0.0.1:8765`。患者与医生须使用相同患者、就诊和数据库配置。

`pnpm oral:check` 检查本次审定源码的哈希；它不再表示所有上游文件完全未改动。验证结果与未完成项目见 `integrations/oral-memory/VERIFICATION-medical.txt`。交付为源码包，不包含 node_modules、用户凭据、患者数据库或编译产物。
