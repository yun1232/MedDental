# MedDental

Offline multimodal inference for dental enamel white spots. Run `python train.py` to learn a case memory and `python test.py` to evaluate against an immutable memory snapshot. Both call a model API directly; no frontend, application backend, patient simulator or Node/SDK subprocess is started.

| Input mode | Visible patient text | Hidden targets |
| --- | --- | --- |
| `history` | 既往史 | 病因、治疗方案 |
| `clinical` | 既往史、分布、质地、色调、均一性 | 病因、治疗方案 |

Four separate roles handle visual evidence, unified memory retrieval and region planning, diagnostic reasoning, and per-round review. Retrieval combines fixed text, current visual findings and evidence gaps in one candidate pool; it supplies source-grounded case derivations to reasoning. Visual uncertainty or a review conflict routes through retrieval to a crop/mask of a current original, then fresh visual analysis and updated case context. Reasoning-only corrections reuse the evidence. Every round retains all current originals and fixed text. Both train and test run diagnostic review; only training can update memory. Unresolved cases remain explicit when the budget is exhausted.

```sh
python -m pip install -r integrations/oral-memory/requirements.txt
# Copy .env.oral.example to .env.oral and configure a vision-model API.
python train.py --input-mode history
python test.py --input-mode history
python train.py --input-mode clinical
python test.py --input-mode clinical
```

[中文说明](README.zh.md) · [完整流程与参数](integrations/oral-memory/BATCH.zh.md) · [验证记录](integrations/oral-memory/BATCH-VERIFICATION.md)

The original case-library, descriptor/history retrieval and versioned decay mechanisms remain in `integrations/oral-memory/`. Existing upstream Harness/vendor source is retained independently, with its licenses and dependency closure; it is not a dependency of these Python entry points. Retired patient/doctor UI and inquiry workflows are no longer application entry points.
