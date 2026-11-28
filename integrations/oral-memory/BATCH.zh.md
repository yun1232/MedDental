# 批量 memory 训练与测试

使用 `data/label.xlsx` 与 `data/pic/`，无需启动患者/医生网页。模型权重固定；训练指执行问诊，并从训练集标注积累 memory。模型调用和 agent 生命周期仍由现有 Harness 管理，Python 只负责数据、阶段和持久化。

## 配置与一键运行

需要 Node.js 24.9+、pnpm、Python 3.10+。在仓库根目录安装依赖：

```sh
pnpm install
python -m pip install -r integrations/oral-memory/requirements.txt
```

复制 `.env.oral.example` 为 `.env.oral`，配置支持图像和工具调用的 OpenAI 兼容接口，例如本地 vLLM 的 Qwen3-VL：

```dotenv
ORAL_MEMORY_PYTHON=python
ORAL_BATCH_PROVIDER=oral-vlm
ORAL_BATCH_BASE_URL=http://127.0.0.1:8000/v1
ORAL_BATCH_MODEL=Qwen3-VL-8B-Instruct
ORAL_BATCH_API_KEY=your-key
```

模型名必须与服务实际暴露的名字一致。无鉴权本地服务也填写一个非空占位 key；Linux 的 Python 命令通常是 `python3`。模型服务需先启动，脚本不会下载模型或启动 GPU 服务。自定义其他 provider 时，用 `ORAL_BATCH_PROVIDER_PATCH` 指定追加的 Harness provider 配置。

```sh
# 检查真实数据和固定划分，不调用模型
pnpm oral:batch --dry-run

# 医生模式：自动先训练后测试
pnpm oral:batch --mode doctor

# 患者模式：自动先训练后测试
pnpm oral:batch --mode patient

# 两种模式依次运行，各自独立训练 memory
pnpm oral:batch

# 也可分阶段运行
pnpm oral:train --mode doctor
pnpm oral:test --mode doctor

# 接口调试，训练与测试分别只跑两例
pnpm oral:batch --mode patient --limit 2 --output runs/patient-smoke
```

## 表格和图像

- 第一列 `编号` 是病例 ID，最后一个有效列 `治疗方案` 是标签，尾部空白列忽略。临床描述列为医生事实。
- `文件名` 中含姓名和治疗提示，只保留匿名元信息，不把原值发给模型，避免身份和答案泄漏。图片按编号绑定。
- 支持 `44.JPG`、`44-1.JPG`、`44-2.JPG`，后缀不区分大小写，同一病例图片不跨集合；沿用每病例最多三张的限制。
- 当前有效标注病例 53 个，默认 seed=42，42 个训练、11 个测试。编号 22、59–78 只有编号，29、49 缺治疗方案，明确记录在排除清单，不补造标签。
- 缺图、重复编号、重复/空有效表头或无编号却有内容会报错。重复图像的病例绑定到同一个划分。
- 批处理默认使用 `enamel-white-spot`：牙釉质白斑、龋性白斑、氟斑牙、MIH；不套用口腔黏膜病变的病理问卷。

## 两种模式

| 模式 | 交流 Agent 的输入 | 新患者 Agent 的职责 |
| --- | --- | --- |
| doctor | 编号、全部临床列、原图；不含治疗标签 | 不启用 |
| patient | 简短患者主诉、原图，随后逐轮收到回答 | 读取隐藏的医生描述，建立画像，回答交流 Agent 的具体问题 |

画像包含患者可感知的外观、已知经历、可回答事实和未知字段。未记录的年龄、性别、职业、病程和症状不编造。专业诊断分级不会直接变成患者口中的诊断。患者 agent 无文件、记忆、shell 或检索工具，也拿不到治疗标签；它的画像保存在结果中，不直接暴露给交流团队。

两种模式都调用同一个 `runMedicalConsultation`，复用现有交流控制、经历检索 Key1、独立视觉基线、历史区域提示、裁剪/遮挡复核、联合检索 Key2、推理与审查流程。保留区域视觉价值的独立审查，不用治疗标签匹配替代区域价值判断。

患者对话默认上限 6 轮，`--max-turns 1..12` 可调整。医生模式一次性给齐已有资料，不为未知信息临时编造患者回答。未通过审查、需要人工判断或用完轮数时弃答，不强制生成治疗方案。

## 学习、冻结与恢复

训练先独立预测；团队审查通过后生成候选治疗方案，再由宿主侧数据教师读取训练标签，通过现有医生审核/病例发布接口写回标注病例。未获团队通过的病例不发布共享 memory。标签表示训练集参考方案，不代表实际治疗成功，真实疗效保留未知。

每个训练病例在上一份 memory 的副本上运行，成功后发布不可变快照并原子切换指针。中断后相同命令会跳过已完成病例，保持固定顺序。两种模式的 memory 相互独立。

测试必须已有训练快照。每个测试病例从同一快照复制独立数据库与共享图像，测试产生的曝光计数、区域评分和工作状态只写本病例副本，不改变训练快照，也不传给下一测试病例。测试不执行标签写回，并检查快照指纹。

数据、模型、划分或对话配置改变，需要新的 `--output`。`--limit` 仅限制本次病例数，汇总标记 `partial`，不冒充全量实验。失败会停止批次，避免跳过训练病例后继续改变 memory。同目录有互斥锁；强制结束留下 `batch.lock` 时，须先确认旧进程退出再清理。

## 输出

默认 `runs/oral-batch/` 已被 Git 忽略，包含私有病例、对话和标签，按研究数据管理。

| 路径 | 内容 |
| --- | --- |
| manifest.json | 数据指纹、固定划分、排除病例、配置 |
| doctor/memory/current.json | 医生模式训练快照指针 |
| patient/memory/current.json | 患者模式训练快照指针 |
| 模式/train或test/case-N.json | 预测、参考标签、轮数、弃答和指标 |
| 模式/train或test/case-N-attempt-*/episode.json | 画像、完整对话和团队输出 |
| 模式/train或test/case-N-attempt-*/worker.log | 私有日志，失败时另有 failure.json |
| 模式/train或test/summary.json | 指标汇总和部分运行标记 |
| 模式/test/frozen-memory.json | 固定测试快照和指纹 |

指标为标准化治疗组成项匹配、组成项 precision/recall/F1、弃答率和平均轮数。干预顺序不影响匹配；同义词不会自动等同。所有病例（含弃答）进入分母。指标只衡量治疗文本一致性，不等于临床正确率或真实疗效。

## 校验

```sh
python -m unittest discover -s integrations/oral-memory/tests -v
pnpm vitest run integrations/oral-memory/tests/batch.spec.ts integrations/oral-memory/tests/harness-team.spec.ts
node --import tsx/esm integrations/oral-memory/tests/batch-sdk-smoke.mjs
```

最后一项使用本机合成 OpenAI 兼容服务，验证真实 SDK、CLI、图像、医生模式、患者追问与治疗输出，不调用外部模型，也不产生真实实验成绩。
