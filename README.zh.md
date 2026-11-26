> 最新功能：患者交流 Agent 根据检索病例控制追问和流程；两阶段独立价值、独立视觉基线、候选去重与医生纠错经验；视觉价值由审查 Agent 核对区域视觉判断后自动更新；医生判断后的病例自动存为本人记忆。完整说明见 [功能与业务流程](integrations/oral-memory/TEAM.zh.md)。


> 当前医疗精简版：累计移除 78 个无关包，保留 Web 问诊入口。最新删除范围与验证结果见 [PRUNING.zh.md](PRUNING.zh.md)，启动方法见 [REFINEMENT.zh.md](REFINEMENT.zh.md)。

# MedDental

项目配置已集中到 [`config/`](config/README.md)，原有启动和构建命令继续使用；运行指定测试可使用 `pnpm vitest run ...`。

基于 DeepSeek Harness 的口腔白斑记忆与渐进问询系统。

[English](README.md) · [精简与验证说明](REFINEMENT.zh.md) · [MIT 许可证](LICENSE)

保留 Cordis、框架包、原生能力、补丁、构建脚本和应用入口，通过现有 MCP 客户端与患者专用预设接入业务。

- 三类业务记忆：白斑表征、患者经历（合并口腔与诊疗事件）、医生反馈。
- 视觉阶段只按患者经历检索；诊断阶段再用白斑描述＋患者经历共同检索。
- 记忆长期保留；视觉和诊断价值分独立。第一阶段由审查 Agent 读取各区域视觉判断、依据新增信息自动更新，第二阶段依据医生确认评估成功与否。失败仅扣对应阶段0.1，未知结果不计分；临时模型分乘本阶段价值后选参考。
- 患者交流 Agent 根据检索病例判断信息缺口，每轮只问一个主要问题，并控制视觉、检索、推理和返工。
- 视觉先做不含历史参考的原图基线，再做病例引导的定位和局部复核；候选选择减少近似重复。
- 医生工作台提交补充、纠正、追问、成功结局和阶段反馈；纠错经验经确认后按条件复用，跨患者需授权。

## 启动

需要 Node >=24.9、项目固定版本 pnpm、Python >=3.10、Node 开发头文件和 C 编译器。模型和 API 凭据继续使用原框架配置；五角色图像评估需选择已声明图像输入能力的模型。

```sh
pnpm install --frozen-lockfile
python3 -m pip install -r integrations/oral-memory/requirements.txt
cp .env.oral.example .env.oral
```

编辑 `.env.oral`，填写患者、就诊、数据库和医生凭据。若代码来自 ZIP，需先初始化 Git 并创建本地提交，供原构建读取版本信息，具体步骤见[项目说明](REFINEMENT.zh.md)。

```sh
pnpm build
pnpm oral:patient
```

另开终端运行 `pnpm oral:doctor`，打开 `http://127.0.0.1:8765`，输入医生凭据并读取病例。默认就诊编号为 `current`；改成其他编号时，医生表单也应填写相同编号。两个入口读取同一份 `.env.oral`，患者配置与会话目录按患者分开。新会话默认使用 `oral-patient`；已有用户预设选择仍可能优先，需要时在界面切换。

## 检查与扩展

```sh
pnpm oral:check
pnpm test:oral
pnpm test:framework
```

`test` 执行业务集成检查；`test:framework` 覆盖 MCP、系统提示词和 persona，不等于上游全仓测试。移除的文档、治理和发布命令记录在 `integrations/oral-memory/refinement.json`。部分保留的上游测试会检查已移除的治理资料；运行完整上游门禁前应恢复原始发行内容。

业务实现见 [`integrations/oral-memory`](integrations/oral-memory/README.zh.md)，配置见 [`oral-patient.cordis.yml`](apps/cli/config/examples/mcp-memory/oral-patient.cordis.yml)，启动器见 [`scripts/oral-project.mjs`](scripts/oral-project.mjs)。agent-loop、会话格式、SDK 协议和沙箱逻辑保留原实现。

完整构建、患者 Web 启动、五角色 Harness 链路及医生发布/跨患者召回检查已通过。两项上游 HTTP 协商测试仍与原始代码一样失败，其余 309 项框架定向检查通过。角色链路使用模拟外部模型；尚未验证真实模型的诊断效果。交付的是源码，未包含预构建产物。详情见[验证记录](integrations/oral-memory/VERIFICATION.txt)。
