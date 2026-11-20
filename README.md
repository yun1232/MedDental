# MedDental

口腔白斑辅助问询、病例记忆与医生复核程序。面向本地使用，Python 3.10+ 即可运行，运行时只使用标准库，无需 Node.js、pnpm、Docker 或前端构建。

## 启动

在本项目目录打开终端：

```sh
python run.py
```

macOS / Linux 如使用 `python3` 命令，运行 `python3 run.py`。

| 入口 | 地址 |
| --- | --- |
| 患者交流与病例摘要 | http://localhost:8080 |
| 医生输入与记忆管理 | http://localhost:8080/doctor |

医生凭据首次运行自动生成在 `data/doctor-token.txt`，复制其中内容到医生页面的“访问凭据”。也可在环境变量 `ORAL_DOCTOR_TOKEN` 中设置至少 16 位凭据。患者和医生入口由同一个进程提供，无需再启动 8765 或另一套 Web 服务。按 Ctrl+C 停止。

## 模型配置

不配置 API 时可完成逐项问询、白斑文字表征录入、病例持久化、检索和医生复核。需要自然语言理解和回答时，将 `.env.example` 复制为 `.env`，填写：

```dotenv
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
ORAL_MODEL=deepseek-chat
ORAL_MEMORY_PATIENT_ID=patient_001
```

重新运行 `python run.py` 后，在患者页面选择“自然语言交流”。配置模型时，当前陈述和相关病例上下文会发送到所配置的模型服务；密钥仅由本地后端读取。提取结果经过 JSON 校验后交给原有问询控制器，模型不能自行写入医生确诊意见或修改记忆效用分。

本版本处理文字问询和文字表征，不内置医学图像识别模型。原有记忆模块同样不负责图像推理；如需从照片自动生成表征，需要接入对应视觉模型。现有 MCP 工具仍可接收外部视觉模型已提取的结构化描述。

## 保留的功能

| 功能 | 实现 |
| --- | --- |
| 患者多轮交流 | 逐项问询；配置模型后提取自然语言信息并回答患者问题 |
| 问询控制 | 医生追问优先、冲突澄清、依赖字段、暂缓回答、追问次数限制 |
| 白斑文字表征 | 部位、侧别、邻近牙位、颜色、形态、表面、边界、范围和尺寸；保留 OLK/1 表示 |
| 四类记忆 | 白斑表征、口腔经历、诊疗经历、医生反馈分别存储 |
| 分级检索 | 白斑表征相关性优先，关联诊疗相关性其次；效用只影响同等相关组 |
| 记忆评分 | 使用与医生反馈分开；低效用记忆进入低频，所有记忆长期保留 |
| 医生输入 | 补充、纠正、追问、取消/确认追问、最终诊断、字段更新、效用反馈、频率管理 |
| 持久化 | SQLite、不可变历史版本、乐观版本校验、重复轮次幂等、防止患者伪造医生确认 |
| MCP 接口 | 保留原 `oral_memory/server.py`，无需依赖完整 Harness |

默认数据库在 `data/oral-memory.sqlite3`。一个运行实例绑定一个患者；同一患者可使用不同就诊编号。更换患者请修改 `ORAL_MEMORY_PATIENT_ID` 后重启，患者身份不能由页面或工具参数切换。

## 文件结构

| 路径 | 用途 |
| --- | --- |
| `run.py` | 唯一本地启动文件，默认 8080 |
| `oral_memory/local_app.py` | 患者 HTTP 接口、医生入口路由与轮次持久化 |
| `oral_memory/model_client.py` | 可选的模型服务连接 |
| `oral_memory/store.py` | 四类记忆与版本存储 |
| `oral_memory/retrieval.py` | OLK/1 表征与分级检索 |
| `oral_memory/extensions.py` | 效用、频率、反馈和问询数据库表 |
| `oral_memory/workflow.py` | 原有患者问询与医生提交流程 |
| `oral_memory/schemas.py` | 输入、记忆与工具的结构校验 |
| `oral_memory/doctor_portal.py` | 保留的医生输入处理逻辑 |
| `oral_memory/server.py` | 保留的 MCP stdio 协议接口 |
| `oral_memory/web/` | 患者及医生的静态页面、样式和交互 |
| `oral_memory/questionnaire.json` | 问询模板 |
| `oral_memory/prompts/` | 原有记忆操作提示词 |
| `oral_memory/tests/` | 业务与本地服务回归测试 |

## 已有数据与测试

迁移既有业务数据库时，先备份原文件，在 `.env` 中设置 `ORAL_MEMORY_DB` 为既有数据库的绝对路径，并保持原患者 ID。记忆库支持原有 v1 → v2 迁移；问询字段或模板版本变更仍需显式迁移。新本地入口只增加独立的 `local_chat_turns` 表，不清空既有记忆、版本或问询状态。

```sh
python -m unittest discover -s oral_memory/tests -v
```

已验证原 39 项业务测试与新增 7 项本地应用测试，共 46 项。新增测试覆盖真实 8080 启动、患者/医生共用端口、重启恢复、模型提取模拟、失败回滚、重复请求和权限隔离。未进行真实模型或临床诊断效果评估。文件裁剪范围见 [精简说明](CLEANUP.md)。
