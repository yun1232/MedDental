> 当前医疗精简版：累计移除 78 个无关包，保留 Web 问诊入口。最新删除范围与验证结果见 [PRUNING.zh.md](PRUNING.zh.md)，启动方法见 [REFINEMENT.zh.md](REFINEMENT.zh.md)。

# MedDental

Project configuration is centralized in [`config/`](config/README.md). Use `pnpm vitest run ...` for selected tests; the root `tsconfig.json` remains the automatic discovery entry.

Oral leukoplakia memory and progressive patient interviewing on DeepSeek Harness.

[中文](README.zh.md) · [Refinement guide](REFINEMENT.zh.md) · [MIT license](LICENSE)

This distribution preserves the Harness runtime, Cordis vendor sources, native capabilities, dependency patches and application entry points. Oral business behavior uses the existing MCP client and a patient-specific agent preset.

Four memory categories store lesion descriptions, oral experiences, care history and doctor feedback. Retrieval prioritizes lesion phenotype relevance before associated care relevance. Low utility reduces selection frequency among equally relevant records; records are retained. The interview asks one main question at a time, handles missing or conflicting information and prioritizes doctor follow-ups. A local doctor form supplies corrections, questions and review decisions.

## Start

Requirements: Node >=24.9, the pinned pnpm version, Python >=3.10, Node development headers and a C compiler. Existing Harness model-provider configuration still applies.

```sh
pnpm install --frozen-lockfile
cp .env.oral.example .env.oral
```

Edit `.env.oral` to set the patient, encounter, database and doctor credential. Configure the model using the existing Harness provider mechanism. If extracting a ZIP without Git metadata, first initialize Git and create a local commit; the upstream build embeds the commit hash. The [project guide](REFINEMENT.zh.md) includes the steps.

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

The business integration checks pass. Full builds were blocked by IPC restrictions, missing Node development headers and absent Git metadata. Two upstream HTTP negotiation tests fail identically in the original source. No live model interview or clinical evaluation was performed. This package contains source, not prebuilt application artifacts. See [verification](integrations/oral-memory/VERIFICATION.txt).
