# Offline Python verification — 2026-11-28

The application was replaced with direct Python entry points (`train.py`, `test.py`). Patient/doctor portals, patient simulation, questionnaire routing, the medical Harness bridge and Node SDK batch launchers were removed. Retained upstream Harness/vendor packages were not rewritten.

Validation uses 16 retained native storage tests and 22 offline application tests (38 total, passing). The tests exercise strict field allowlists and two hidden labels; deterministic case/image splits; actual crop and mask pixels; unchanged original images; repeated-region and no-new-evidence stopping; real native case publication/retrieval/decay; current-case and identical-image exclusion; SQLite read-only evaluation; failure rollback; and completed-case resume without repeated model calls.

An end-to-end regression starts a synthetic local OpenAI-compatible model endpoint, then executes the real root `train.py` and `test.py` commands for BOTH `history` and `clinical` modes. Each episode follows visual -> unified retrieval -> reasoning -> diagnostic review; visual gaps route through retrieval region planning, an actual crop/mask, fresh visual analysis and updated case derivations. Both CLI input modes pass. Runtime uses Python and direct model API calls, with no project frontend/backend, Node/SDK or patient/doctor service. The synthetic endpoint is test infrastructure only.

Additional regressions cover diagnostic review on every train/test round; reviewer visual conflicts when reasoning is already confident; reasoning uncertainty overriding reviewer approval; reasoning-only corrections with no extra visual/retrieval call; case-retrieval retries using the current gap without a crop; unified visual recall even when history is unknown; multiple originals with a crop from the selected original; provenance-preserving case derivations; rejecting fabricated source evidence and cross-role output fields; per-region visual judgments saved into memory; and utility review receiving visual evidence rather than predictions/audit verdicts/labels.

The supplied workbook was validated in both input modes: 53 complete labeled cases, 42 training and 11 test cases at seed 42. Current-case 病因 and 治疗方案 remain targets; 边界 and all unlisted text columns are not model inputs. Empty/unlabeled rows are listed explicitly.

No real or paid model experiment was run, and no real-data accuracy or clinical treatment success is claimed. Test metrics measure agreement with reference labels, not clinical reliability.

The optional original stdio memory-storage endpoint remains covered by its retained compatibility test; the new train/test pipeline never starts it. Its former patient inquiry tools are removed. Historical storage table/record identities and memory revisions are retained without deleting user memory.

The previous full Harness build/type-check and MCP HTTP timeout issues belong to the retained upstream framework, which the new Python entry points do not invoke. This change does not claim to repair that separate framework. The application checks are the Python business suite, two real-data dry runs, whitespace check and source-integrity check. The runtime configuration now identifies pipeline four-agent-unified-memory (version 4); use a fresh output directory when moving from the old pipeline.
