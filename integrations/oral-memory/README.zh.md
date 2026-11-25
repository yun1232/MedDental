> 最新业务机制以 [TEAM.zh.md](TEAM.zh.md) 为准：病例驱动问询和流程由患者交流 Agent 决定；两阶段价值独立，视觉先做独立基线；医生确认的纠错经验可复用。下方旧的单一迭代分和问卷固定调度描述仅供兼容接口参考。

> 当前三类业务记忆：白斑表征、患者经历、医生反馈。视觉阶段仅患者经历；诊断阶段白斑描述＋患者经历。经历统一读写，旧 experience/care 及版本、分数保留兼容。最新机制见 [TEAM.zh.md](TEAM.zh.md)。


当前统一入口：`put_patient_experience`、`search(category=patient_experience)`、`get_patient_experience`。`retrieve(purpose=visual)` 只检索患者经历，`purpose=diagnosis` 再加入白斑描述。下文旧表和四路接口属于保留的兼容层，不作为新的业务分类。

## 旧存储与接口兼容说明

---
description: "通过现有 DSH MCP 客户端配置与使用患者隔离的口腔白斑记忆的参考说明。"
---

# 口腔白斑记忆

精简项目请先阅读[项目说明](../../REFINEMENT.zh.md)。`pnpm oral:patient` 同时加载下述记忆 overlay 与专用患者预设，`pnpm oral:doctor` 读取相同部署配置。下述独立 overlay 说明仅覆盖记忆服务。启动器按患者分离 Harness 配置与会话目录，继续使用原有 dsh 入口。

[English](README.md) | 中文

## 概述

该集成把白斑表征、口腔经历、诊疗经历和医生反馈保存在四张独立 SQLite 表中。检索先召回白斑描述，再按白斑表征相关性、关联诊疗相关性排序。四类旧记录表不保存图片字节或图像向量，不调用外部 embedding 服务。当前匹配器采用显式字段匹配和文本词元覆盖率。

## 目录

- [配置](#setup)
- [记忆与检索](#memory-retrieval)
- [验证](#checks)
- [限制](#limitations)

<a id="setup"></a>

## 配置

服务需要支持 SQLite 的 Python 3.10+ 和能工作的 DSH 项目，只使用 Python 标准库。从项目根目录运行，由可信部署配置绑定患者并选择 overlay：

```sh
export ORAL_MEMORY_PATIENT_ID=patient_001
export ORAL_MEMORY_ACTOR_ROLE=patient
export ORAL_MEMORY_DB=/absolute/private/path/oral-memory.sqlite3
pnpm dsh web --patch apps/cli/config/examples/mcp-memory/oral-leukoplakia.cordis.yml
```

Web 命令是部署入口；本次没有运行真实 Web/模型会话。CLI 配置解析和真实 DSH MCP 工具桥接已分别验证。服务在 MCP 初始化响应中返回操作提示词，现有客户端把这段原文加入系统提示词。无需替换 persona 或修改 agent-loop。

`ORAL_MEMORY_PATIENT_ID` 必填。角色默认为 `patient`，可信医生部署选择 `doctor`。工具参数不能修改患者或角色。一个 MCP 实例服务一个患者；不同患者使用独立实例或 Agent 作用域组合。这是部署范围隔离，不是新增的登录或签名服务。医生作用域的工具调用本身不代表额外的人类审批；需要医生工作流的调用方自行提供确认。

| 变量 | 默认值 | 含义 |
|---|---|---|
| `ORAL_MEMORY_ROOT` | 当前目录 | 包含 Python 服务的项目根目录 |
| `ORAL_MEMORY_PYTHON` | `python3` | Python 可执行程序 |
| `ORAL_MEMORY_DB` | `~/.dsh/oral-memory/memory.sqlite3` | 专用数据库，不能指向其他应用数据库 |
| `ORAL_MEMORY_ACTOR_ROLE` | `patient` | 宿主选择的调用角色 |
| `ORAL_MEMORY_MIN_PHENOTYPE_SCORE` | `0.2` | 第一级最低正相关分数 |
| `ORAL_MEMORY_RECALL_LIMIT` | `100` | 第一级候选上限，返回结果说明是否截断 |
| `ORAL_MEMORY_PER_CATEGORY_LIMIT` | `10` | 每组诊疗或补充记录的数量上限 |
| `ORAL_MEMORY_MAX_SCAN_RECORDS` | `10000` | 每患者每类别扫描上限，超限显式报错 |
| `ORAL_MEMORY_BUSY_TIMEOUT_MS` | `5000` | SQLite 等待写锁的时间预算 |
| `ORAL_MEMORY_FIELD_WEIGHTS` | `{}` | 表征字段内部的正权重，未指定字段权重为 1 |

字段权重只作用于表征匹配内部，不会把两级检索优先级变成加权求和。调用方的 `top_k` 必须在 1 到 50 之间。

<a id="memory-retrieval"></a>

## 记忆与检索

四个 `put_*` 工具接收类型化 `record` 和 `expected_version`，以 [`schemas.py`](schemas.py) 为准。患者部署不公开 `put_doctor`，患者写入不能伪造医生确认或替换已确认记录。创建时预期前置版本为 0，修改时必须提供真实当前版本。相同内容重复写入幂等。每次变更在同一事务中保存当前记录与不可变历史版本。

| 类别 | 内容 | 关联方式 |
|---|---|---|
| `lesion` | 部位、侧别、邻近牙位、外观、范围、尺寸描述，以及独立注明来源的病理结果 | 每次就诊的一次白斑观察，多次观察共用 `lesion_id` |
| `experience` | 口腔症状、习惯、损伤、自行处理和其他口腔经历 | 显式白斑 ID；未指定时仅作用于本次就诊 |
| `care` | 就诊、检查、诊断、手术、用药和随访 | 显式白斑 ID；未指定时仅作用于本次就诊 |
| `doctor` | 补充、纠正、未回答/已回答的追问和最终诊断 | 关联记录 ID，加白斑或就诊关联 |

服务把白斑描述渲染成确定性的 `OLK/1` 文本，同时保留类型化 JSON。未知信息保持显式未知。病理结果来自提供的报告或医生来源，不参与图片外观匹配。记忆库不依赖历史图片字节。该集成不会改变原框架现有的附件保留策略。

`retrieve` 分别接收 `lesion_query`、`care_query`、`experience_query` 和 `doctor_query`。第一级匹配已知表征字段，可附加仅含外观的文本。未知字段不能因双方均未知而匹配。第二级只检索每条候选关联的诊疗记录。保持白斑表征相关性优先、关联诊疗相关性其次。在相关性完全相同的组内，迭代分和频率控制原 MCP 接口的加权抽样；团队初排不重复应用这些权重，并保留非零探索概率；诊疗或效用不能越过更相关的表征。经历和医生反馈独立检索为补充证据，不改变该排序。

显式白斑关联可以跨就诊，空白斑关联列表仅适用于本次就诊。明确关联其他白斑的记录，不能因就诊相同而挂接。没有已知表征时返回 `needs_clarification`，没有正相关结果时返回 `no_match`，均不暗中回退到其他类别。明确查询某一类别时用 `search`，读取指定记录及版本用 `get`。空查询表示最近写入顺序，不代表语义相关性或事件时间顺序。

未完成追问可使用 `note_type=follow_up`、`question_status=open`。最近录入的最终诊断使用 `category=doctor`、`note_type=final_diagnosis`、空查询和 `top_k=1`。`doctor_submit` 在同一事务中保存明确确认的医生反馈、可选的目标记忆纠正和问询字段更新。直接调用 `put_*` 仍是独立写入。患者回答单独记录，不覆盖医生原始追问。


## 患者交流与医生输入

`case_state` 读取持久化问询模板与版本；`patient_turn` 提交提取的字段、可选记忆写入，并返回一个下一步动作。字段区分未知、已知、不确定、否认、暂缓和冲突，独立的待回答问题记录哪些内容已经问过。调度先处理医生追问，再处理冲突，然后补齐模板必需字段及其他字段；尊重暂缓，限制追问次数，患者自己提出问题时优先回答。现有 DSH 模型按提示词提取信息并与患者交流，`workflow.py` 提供有状态控制器，不是新训练的模型。

模板通过 `ORAL_MEMORY_QUESTIONNAIRE` 配置，附带的 `questionnaire.json` 是问询模板，不是诊断决策规范。每轮使用事件 ID 重试，并提供真实病例版本。记忆变更、问询更新和响应记录在一个事务中提交，病例重启后保留。新记忆版本保留旧事实，效用从中性值重新开始。

医生输入表单共用相同数据库和患者绑定。在项目根目录配置相同的 `ORAL_MEMORY_DB`、`ORAL_MEMORY_PATIENT_ID` 后启动：

```sh
export ORAL_DOCTOR_TOKEN='<local doctor credential of at least 16 characters>'
python3 integrations/oral-memory/doctor_portal.py --port 8765
```

打开 `http://127.0.0.1:8765`，输入凭据和就诊编号，再读取病例。表单支持补充、纠正、追问、最终诊断、字段更新、取消追问、使用反馈和频率调整。数据 API 均要求 bearer token。服务仅监听本机回环地址，每实例服务一个患者，是本地输入入口，不是医院账号认证。患者交流继续使用现有 DSH 界面。

## 效用与低频

没有删除工具或自动删除。迭代分从1开始，每次唯一无帮助反馈扣0.1；临时模型分只用于本轮比较。兼容效用统计为 `(1+positive)/(2+positive+negative)`，新版本从 0.5 开始。提供给模型、实际使用和确认有帮助分别记录；模型只能对检索回执中包含的记录报告 `used`，增加使用次数而不给自己加分。医生确认有帮助累计正反馈 1，确认无帮助累计负反馈 1，确认有机会但未使用累计弱负反馈 0.1。同一回执同一版本的同类反馈去重。

至少提供 10 次且效用低于 0.3 后进入低频。在同等相关组内，低频权重为 `(decay_score+0.05)*0.2`，正常权重为 `decay_score+0.05`，抽样混合 90% 加权选择和 10% 均匀探索。默认值可用 `ORAL_MEMORY_LOW_FREQUENCY_MIN_EXPOSURES`、`ORAL_MEMORY_LOW_FREQUENCY_THRESHOLD`、`ORAL_MEMORY_LOW_FREQUENCY_MULTIPLIER`、`ORAL_MEMORY_UTILITY_FLOOR`、`ORAL_MEMORY_EXPLORATION_RATE`、`ORAL_MEMORY_UNUSED_PENALTY` 配置。从未提供的记忆不会因为没有使用而扣分。

必读上下文直接包含未完成医生追问、当前诊断和冲突状态，不受频率影响。`get` 和明确的最近历史查询保留低频记录。医生通过 `set_frequency` 恢复正常频率或保护当前版本。v1 数据库以新增表方式迁移到 v2，已有记忆和版本行保持不变，过程见 `MIGRATIONS.txt`。

<a id="checks"></a>

## 验证

在项目根目录运行不依赖第三方包的持久化与协议测试：

```sh
python3 -m unittest discover -s integrations/oral-memory/tests -v
```

安装项目 Node 依赖后，运行真实 MCP 桥接冒烟测试：

```sh
node --import tsx/esm integrations/oral-memory/tests/dsh-smoke.mjs
```

冒烟测试挂载现有 DSH MCP 客户端，检查提示词和记忆工具，提交患者轮次，读取持久化问询状态并验证卸载。Python 套件包含 39 项测试，涵盖真实回环 HTTP 和已保存 stdio 问询回放；`node integrations/oral-memory/tests/doctor-dom-smoke.mjs` 驱动真实表单 JavaScript 与 HTTP 提交。测试记录均为合成数据。

<a id="limitations"></a>

## 限制

当前匹配器是低成本的结构化/词面匹配基线，不是语义 embedding 模型。文本匹配使用中文双字片段和英文/数字词元，不同临床说法可能需要补充词表归一化。服务扫描一个患者的有界类别，不是大规模跨患者病例搜索引擎。服务不调用视觉模型，也不训练模型；当前多模态助手根据提示词提取描述并调用工具，本次未用真实模型评估提取或工具选择效果。

整个仓库的产物构建依赖 Git HEAD，而上传源码包不包含该信息，全仓检查不属于已通过验证项。Chromium 视觉检查受到环境限制，DOM 与真实 HTTP 流程已经测试。准确结果及仍需完成的真实模型验证见 `VERIFICATION.txt`。
