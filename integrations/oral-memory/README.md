# Offline case memory and visual refinement

The root `train.py` and `test.py` call a multimodal model API directly. Separate visual, retrieval, reasoning and diagnostic-review roles use one descriptor/history candidate pool and the existing versioned case library and decay mechanisms. Retrieval owns source-grounded case derivation and current-image region planning; review runs every round in both training and testing. A separate training-only utility review scores actual regional visual gains without predictions or labels. No patient simulator, inquiry state machine, medical UI or Node/SDK process participates in training/evaluation.

[完整中文流程](BATCH.zh.md) · [Module responsibilities](README.zh.md) · [Verification](BATCH-VERIFICATION.md)
