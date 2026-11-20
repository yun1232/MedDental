# MedDental development

The project retains the DeepSeek Harness runtime, vendored Cordis, native components, dependency patches and supported dsh launch path. Add oral business behavior through the MCP integration, Cordis overlays and per-agent presets.

- Keep the retained vendor, native and runtime dependency closure intact. This medical distribution intentionally removes the packages recorded in medical-refinement.json; do not restore Office or developer automation by default. Run `pnpm oral:check` to verify the saved baseline; intentional future framework changes also update its manifest.
- Put memory, retrieval, utility and interview behavior in `integrations/oral-memory/`. Patient identity and actor permissions come from host configuration. Keep database writes transactional and versioned; no automatic memory deletion.
- Keep the dsh CLI as the Node application launcher. `pnpm oral:patient` adds the memory overlay and patient preset; do not introduce a parallel agent loop.
- Run `pnpm test:oral` after business changes. `pnpm test:framework` selects the MCP, system-prompt and persona suites. It is not the full upstream test suite.
- Framework artifact changes require `pnpm build` followed by the affected behavior checks. Report environment blockers and incomplete validation explicitly.
- `integrations/oral-memory/refinement.json` lists removed files and commands. Upstream documentation/CI checks whose assets were removed are outside this distribution. Restore the original source distribution before running its governance or release checks.
- Retain LICENSE, THIRD_PARTY_NOTICES.md, dependency pins, patches and sandbox defaults. Never commit deployment credentials or patient databases.
