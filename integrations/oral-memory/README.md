> 新增多模态团队入口与共享病例经验，见 [TEAM.zh.md](TEAM.zh.md)。下文四类记忆描述的是保留的患者私有 MCP 接口；新增图片、共享检索和五角色运行由团队模块提供。

---
description: "Reference for configuring and using patient-bound oral leukoplakia memory through the existing DSH MCP client."
---

# Oral leukoplakia memory

For the refined project distribution, start with [the project guide](../../REFINEMENT.zh.md). `pnpm oral:patient` applies both the memory overlay below and a dedicated patient preset, and `pnpm oral:doctor` shares its deployment configuration. The standalone overlay instructions below describe the memory service alone. The launcher separates Harness homes by patient while retaining the original dsh entry point.

English | [中文](README.zh.md)

## Summary

This integration saves lesion descriptions, oral experiences, care events, and doctor notes in four independent SQLite tables. It recalls lesion descriptions first and orders candidates by phenotype relevance, then associated care relevance. The four legacy record tables store no image bytes or image embeddings and uses no external embedding service. The current matcher uses explicit fields and lexical token coverage.

## Contents

- [Configuration](#setup)
- [Memory and retrieval](#memory-retrieval)
- [Verification](#checks)
- [Limits](#limitations)

<a id="setup"></a>

## Configuration

The service requires Python 3.10+ with SQLite and a working DSH checkout. It uses Python's standard library only. From the repository root, bind a patient using trusted deployment configuration and select the overlay:

```sh
export ORAL_MEMORY_PATIENT_ID=patient_001
export ORAL_MEMORY_ACTOR_ROLE=patient
export ORAL_MEMORY_DB=/absolute/private/path/oral-memory.sqlite3
pnpm dsh web --patch apps/cli/config/examples/mcp-memory/oral-leukoplakia.cordis.yml
```

The Web command is a deployment entry point; a live Web/model session was not exercised in this delivery. The CLI configuration parser and the real DSH MCP tool bridge were exercised independently. The server returns the policy in its MCP initialization response, and the existing client contributes that literal text to system-prompt assembly. No persona replacement or agent-loop change is required.

`ORAL_MEMORY_PATIENT_ID` is mandatory. The role is `patient` by default; a trusted doctor deployment selects `doctor`. Tool arguments cannot change either identity. One MCP instance serves one patient; use separate instances or agent-scoped compositions for separate patients. This is deployment scoping, not a new login or signature service. A doctor-scoped tool call is not evidence of an independent human approval unless the caller supplies it through its own doctor workflow.

| Variable | Default | Meaning |
|---|---|---|
| `ORAL_MEMORY_ROOT` | Current directory | Repository root containing the Python service |
| `ORAL_MEMORY_PYTHON` | `python3` | Python executable |
| `ORAL_MEMORY_DB` | `~/.dsh/oral-memory/memory.sqlite3` | Dedicated database; do not point at another application's database |
| `ORAL_MEMORY_ACTOR_ROLE` | `patient` | Host-selected actor role |
| `ORAL_MEMORY_MIN_PHENOTYPE_SCORE` | `0.2` | Minimum positive first-stage score |
| `ORAL_MEMORY_RECALL_LIMIT` | `100` | Maximum first-stage candidates; truncation is reported |
| `ORAL_MEMORY_PER_CATEGORY_LIMIT` | `10` | Maximum care or supplementary records per result group |
| `ORAL_MEMORY_MAX_SCAN_RECORDS` | `10000` | Per-patient, per-category scan ceiling; exceeding it fails explicitly |
| `ORAL_MEMORY_BUSY_TIMEOUT_MS` | `5000` | SQLite write-lock wait budget |
| `ORAL_MEMORY_FIELD_WEIGHTS` | `{}` | Positive field weights within phenotype matching; omitted fields weigh 1 |

Field weights apply only within phenotype matching. They never turn the two retrieval priorities into a weighted sum. The caller's `top_k` must be between 1 and 50.

<a id="memory-retrieval"></a>

## Memory and retrieval

The four `put_*` tools take a typed `record` and `expected_version`. The [`schemas.py`](schemas.py) definitions are authoritative. `put_doctor` is absent from patient deployments, and patient writers cannot claim doctor confirmation or replace confirmed records. New records use version 0 as the expected predecessor; changes require the exact current version. Identical repeated writes are idempotent. Every changed write atomically stores the current record and an immutable revision.

| Category | Content | Association |
|---|---|---|
| `lesion` | Site, side, nearby tooth references, appearance, extent, size description, separately sourced pathology | One lesion observation per encounter; repeated observations share `lesion_id` |
| `experience` | Oral symptoms, habits, injuries, self-care and other oral experiences | Explicit lesion IDs, or encounter-wide when no lesion IDs are supplied |
| `care` | Visits, examinations, diagnoses, surgery, medication and follow-up | Explicit lesion IDs, or encounter-wide when no lesion IDs are supplied |
| `doctor` | Supplements, corrections, open/answered questions and final diagnoses | Related record IDs plus lesion or encounter links |

The server renders lesion descriptions as deterministic `OLK/1` text while retaining typed JSON. Unknown values remain explicit. Pathology comes from a supplied report or a doctor source and never participates in image-appearance matching. Historical image bytes are not required by the memory database. This integration does not alter the original Harness attachment retention policy.

`retrieve` takes separate `lesion_query`, `care_query`, `experience_query` and `doctor_query` inputs. Stage 1 compares known phenotype fields, with optional appearance-only lexical text. Unknown fields never match other unknowns. Stage 2 searches only care records associated with each recalled lesion. Results preserve phenotype relevance first and associated care relevance second. Within exactly equal relevance groups, utility and recall frequency control weighted sampling with a nonzero exploration probability; care or utility cannot promote a poorer phenotype match. Experiences and doctor notes are independently retrieved supplementary evidence and never change that ranking.

An explicit lesion link spans encounters; an empty lesion-link list applies only to its own encounter. A record explicitly linked to another lesion does not attach merely because its encounter matches. No known phenotype produces `needs_clarification`; no positive match produces `no_match`. Neither response silently falls back to another category. Use `search` for an explicit single-category query or `get` for a known record and its revisions. An empty search query means recent writes, not semantic relevance or event chronology.

Doctor-question searches support `note_type=follow_up` and `question_status=open`. The most recently entered final diagnosis uses `category=doctor`, `note_type=final_diagnosis`, an empty query and `top_k=1`. The `doctor_submit` operation atomically saves an explicitly confirmed note, optional target-memory corrections and inquiry observations. Direct `put_*` calls remain independent writes. Patient answers are recorded separately; they do not overwrite the doctor's original question.


## Patient communication and doctor input

`case_state` reads the persistent questionnaire and revision. `patient_turn` commits extracted observations, optional memory writes and one next action. Fields distinguish unknown, known, uncertain, denied, deferred and conflict; a separate pending question identifies what has already been asked. The scheduler selects doctor questions first, then conflicts, then missing required fields and other configured fields. It preserves refusals, limits question attempts and lets the assistant answer a patient's own question first. The current DSH model extracts information and speaks to the patient under the contributed policy; `workflow.py` provides the stateful controller, not a newly trained model.

The template is configurable through `ORAL_MEMORY_QUESTIONNAIRE`; the included `questionnaire.json` is an inquiry template, not a diagnostic decision protocol. Each turn uses an event ID for retry and the exact case revision. Memory changes, inquiry updates and the stored response commit in one transaction. Cases survive restarts. New record versions retain prior facts and start with neutral utility.

The doctor input form shares the same database and patient binding. Start it from the repository root after setting the same `ORAL_MEMORY_DB` and `ORAL_MEMORY_PATIENT_ID`:

```sh
export ORAL_DOCTOR_TOKEN='<local doctor credential of at least 16 characters>'
python3 integrations/oral-memory/doctor_portal.py --port 8765
```

Open `http://127.0.0.1:8765`, enter the token and encounter ID, then load the case. The form supports supplements, corrections, follow-up questions, final diagnoses, field updates, question cancellation, feedback and frequency overrides. All data APIs require a bearer token. The service binds loopback only and serves one patient; it is a local input entry point, not hospital account authentication. Patient conversation continues through the existing DSH interface.

## Utility and low frequency

No delete tool or automatic deletion is provided. Utility is `(1+positive)/(2+positive+negative)`. Newly written versions start at 0.5. Delivery, actual use and verified usefulness are different events. The model can report `used` only for a reference contained in its retrieval receipt; this increments use count without rewarding itself. Doctor feedback contributes +1 positive for helpful, +1 negative for not helpful, or +0.1 negative for a confirmed unused opportunity. Duplicate feedback of the same kind for the same receipt and version is ignored.

After at least 10 deliveries and utility below 0.3, a record enters low frequency. Within an equal-relevance group, its weight is `(utility+0.05)*0.2`; normal records use `utility+0.05`. Sampling mixes 90 percent weighted selection and 10 percent uniform exploration. These defaults are deployment variables named `ORAL_MEMORY_LOW_FREQUENCY_MIN_EXPOSURES`, `ORAL_MEMORY_LOW_FREQUENCY_THRESHOLD`, `ORAL_MEMORY_LOW_FREQUENCY_MULTIPLIER`, `ORAL_MEMORY_UTILITY_FLOOR`, `ORAL_MEMORY_EXPLORATION_RATE` and `ORAL_MEMORY_UNUSED_PENALTY`. Unseen records receive no inactivity penalty.

Required context directly includes unresolved doctor questions, current diagnoses and conflict state, independently of frequency. `get` and explicit recent-history lookup retain low-frequency records. `set_frequency` lets a doctor restore normal frequency or protect a current version. Schema v1 databases migrate additively to v2; existing memory and revision rows are preserved. See `MIGRATIONS.txt` for the transition.

<a id="checks"></a>

## Verification

Run the dependency-free persistence and protocol tests from the repository root:

```sh
python3 -m unittest discover -s integrations/oral-memory/tests -v
```

With the checkout's Node dependencies installed, run the real MCP bridge smoke:

```sh
node --import tsx/esm integrations/oral-memory/tests/dsh-smoke.mjs
```

The smoke mounts the existing DSH MCP client, checks the system prompt and memory tools, commits a patient turn, reads persistent case state and verifies unload. The Python suite includes 39 tests with actual loopback HTTP and committed stdio workflow replay. `node integrations/oral-memory/tests/doctor-dom-smoke.mjs` drives the real form JavaScript and HTTP submission. All test records are synthetic.

<a id="limitations"></a>

## Limits

The matcher is a low-cost structured/lexical baseline, not a semantic embedding model. Query and stored text use Chinese bigrams and Latin/number tokens. Different clinical wording can require vocabulary normalization. The service scans a bounded category for one patient; it is not a large cross-patient case-search engine. It neither calls a vision model nor trains a model. The current multimodal assistant extracts descriptors and calls the tools under the contributed policy; extraction and tool selection have not been evaluated with a live model here.

Whole-repository artifact building requires a Git HEAD, which the uploaded source archive does not contain. Whole-repository checks are not a passed validation item. Chromium visual inspection was blocked by the environment, while the DOM and actual HTTP flow were tested. See `VERIFICATION.txt` for exact outcomes and remaining live-model validation.
