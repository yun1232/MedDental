> 最新功能：患者交流 Agent 根据检索病例控制追问和流程；两阶段独立价值、独立视觉基线、候选去重与医生纠错经验；医生判断后的病例自动存为本人记忆。完整说明见 [功能与业务流程](integrations/oral-memory/TEAM.zh.md)。

> 当前医疗精简版：累计移除 78 个无关包，保留 Web 问诊入口。最新删除范围与验证结果见 [PRUNING.zh.md](PRUNING.zh.md)，启动方法见 [REFINEMENT.zh.md](REFINEMENT.zh.md)。

# MedDental

Project configuration is centralized in [`config/`](config/README.md). Use `pnpm vitest run ...` for selected tests; the root `tsconfig.json` remains the automatic discovery entry.

Image-aware, case-memory medical collaboration on DeepSeek Harness. Five frozen-model roles collect information, rank prior cases, inspect images, reason and audit; doctor-reviewed cases automatically enter personal memory, while cross-patient publication requires separate authorization.

Implementation and usage: [五角色医疗框架](integrations/oral-memory/TEAM.zh.md).

[中文](README.zh.md) · [Refinement guide](REFINEMENT.zh.md) · [MIT license](LICENSE)

This distribution preserves the Harness runtime, Cordis vendor sources, native capabilities, dependency patches and application entry points. Oral business behavior uses the existing MCP client and a patient-specific agent preset.

The patient communication agent controls case-grounded questions and bounded workflow routing. Stage 1 retrieves by patient experience for visual reference; stage 2 merges and reranks cases using lesion descriptors plus experience. Each stage uses its own persistent value multiplier. Visual value depends on doctor-confirmed new evidence from a case-guided mask location; diagnostic value depends on confirmed task success. Visual inspection starts with a blinded original-image baseline. Candidate diversity reduces redundant references. Doctor-confirmed correction lessons can be recalled under explicit patient/shared boundaries. Records and prior versions are retained.

## Start

Requirements: Node >=24.9, the pinned pnpm version, Python >=3.10, Node development headers and a C compiler. Existing Harness model-provider configuration still applies.

```sh
pnpm install --frozen-lockfile
python3 -m pip install -r integrations/oral-memory/requirements.txt
cp .env.oral.example .env.oral
```

Edit `.env.oral` to set the patient, encounter, database and doctor credential. Configure an image-capable model using the existing Harness provider mechanism; all five roles receive original images and questions. If extracting a ZIP without Git metadata, first initialize Git and create a local commit; the upstream build embeds the commit hash. The [project guide](REFINEMENT.zh.md) includes the steps.

```sh
pnpm build
pnpm oral:patient
```

In a second terminal, run `pnpm oral:doctor`. Open `http://127.0.0.1:8765`, enter the configured credential and load the encounter. The default encounter is `current`; use the same ID in the doctor form if changing it. Both commands read `.env.oral`. Patient launches use patient-scoped tools and a separate Harness home per patient. New sessions default to `oral-patient`; existing user-selected defaults can take precedence.

## Validate and extend

```sh
pnpm oral:check
pnpm test:oral
pnpm test:framework
```

`test` selects the oral integration checks. `test:framework` selects MCP client, system-prompt and persona suites, not every upstream test. Removed upstream documentation, governance and release commands are listed in `integrations/oral-memory/refinement.json`. Some retained upstream tests expect removed governance assets; restore the original distribution before running its complete governance suite.

Business code: [`integrations/oral-memory`](integrations/oral-memory/README.md). Composition: [`oral-patient.cordis.yml`](apps/cli/config/examples/mcp-memory/oral-patient.cordis.yml). Launcher: [`scripts/oral-project.mjs`](scripts/oral-project.mjs). The original loop, session format, SDK protocols and sandbox implementations are preserved.

The retained application builds successfully with Node development headers. Medical tests cover private/shared isolation, second-key retrieval, grounded visual views, doctor publication and feedback, plus real Harness role execution with a scripted external model. Two upstream HTTP negotiation tests fail identically in the original source. No live model interview or clinical evaluation was performed. This package contains source, not prebuilt application artifacts. See [verification](integrations/oral-memory/VERIFICATION.txt).
