# Batch verification — 2026-11-28

The offline entry points reuse `runMedicalConsultation`; Python does not implement a replacement model loop. Root SDK tools are scoped to `batch_run`, and both evidence and simulated-patient children use isolated presets. Trusted repository YAML expressions are materialized before SDK startup because the CLI/preset validators require literal module names.

Validated with the supplied workbook: 53 labeled cases, a stable 42/11 train/test split, numeric image binding (including both images for case 44), and explicit exclusion of empty or unlabeled rows. Original filenames are withheld because they contain patient names and treatment hints.

Checks:

- `pnpm test:oral` passed: launcher checks, source integrity, the complete Python suite, MCP stdio integration, doctor HTTP/DOM behavior, patient preset, existing Harness team, batch boundary tests and both SDK modes.
- Python business suite: 109 tests passed, including real store/retrieval/teacher publication, label isolation, independent frozen test copies and resume behavior.
- Focused Harness suites: 13 tests passed (10 existing workflow checks and 3 batch information-boundary checks).
- Real SDK/CLI/provider smoke passes for doctor and patient modes using a synthetic local OpenAI-compatible SSE server. Patient mode completes a real question/answer cycle. This smoke also passed with the host native addon temporarily unavailable: batch conversations are ephemeral; the existing medical database and episode files hold durable evidence.
- No paid or external model calls were made; no real-data model accuracy or treatment success is reported.

Broader baseline limitations observed during validation:

- Full artifact build cannot complete in this environment: the primary Node installation lacks development headers. A separate host-library build also reports type errors in unchanged inspector, timeline, Typert test, release and primary-runtime test files. These unrelated files were not changed. The batch SDK source launcher avoids requiring a complete Web artifact build.
- `pnpm test:framework`: 309/311 passed. The existing HTTP credential probe and HTTP negotiation retry tests time out (`credentials.spec.ts`, `negotiation-lifecycle.spec.ts`). The batch entry does not use MCP HTTP transport, and these tests/code were not modified.
- Offline dependency installation recognizes the updated workspace lock entry but cannot finish its supply-chain metadata audit because the local package mirror lacks registry metadata. Runtime checks used installed dependencies; a fresh installation requires access to its normal registry.

The new `@deepseek-ai/dsh-tools` workspace dependency and root lock importer are kept in sync. Source integrity hashes include the batch files. Runtime outputs, labels, patient transcripts, snapshots and credentials are not committed.
