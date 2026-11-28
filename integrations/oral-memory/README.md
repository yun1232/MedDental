# Offline case memory and visual refinement

The root `train.py` and `test.py` call a multimodal model API directly. They use the existing case library, descriptor/history retrieval and versioned decay mechanisms. No patient simulator, inquiry state machine, medical UI or Node/SDK process participates in training/evaluation.

[完整中文流程](BATCH.zh.md) · [Module responsibilities](README.zh.md) · [Verification](BATCH-VERIFICATION.md)
