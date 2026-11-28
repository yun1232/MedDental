# MedDental offline development

Use `python train.py` and `python test.py`. They directly call a multimodal model API and the existing memory modules in `integrations/oral-memory/`. Do not restore patient/doctor portals, simulated patients, questionnaire routes or Node/SDK batch subprocesses.

- `history` exposes only 既往史; `clinical` exposes exactly 既往史、分布、质地、色调、均一性. 病因 and 治疗方案 are hidden targets. 编号 binds data; 边界 and other columns are not model inputs.
- Every inference retains all current original images and fixed input text. Visual uncertainties may request one crop or mask from a CURRENT original. Do not crop a previous crop, copy historical coordinates into current images, or infer masked content.
- Update training memory only after the case's inference ends. Open test memory read-only. Keep case images together in splits and exclude the current case or byte-identical images from retrieval.
- Reuse native case-library/retrieval/decay contracts. Memory stays versioned and is never automatically deleted. Judge visual utility using actual regional evidence, independently of target accuracy.
- Run `python -m unittest discover -s integrations/oral-memory/tests -v`, a real-data `train.py --dry-run`, and `python integrations/oral-memory/check_core.py`. Update integrity hashes for intentional changes/removals. Report unrun real-model experiments.
- Retained upstream Harness/vendor/native packages are independent of the offline entry points. Keep their dependency closure, licenses, pins and patches. Do not restore removed Office/developer automation. Changes to retained framework artifacts require their affected build/checks.
- Do not commit credentials, memory databases or episode/patient outputs. Use ignored `runs/` or an explicitly selected output directory.
