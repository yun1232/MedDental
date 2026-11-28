# 离线病例记忆与视觉复查

入口为根目录 `train.py`、`test.py`。完整输入边界、循环、训练/测试隔离和参数见 [流程说明](BATCH.zh.md)。

主流程模块：`batch_data.py`（输入白名单/标签拆分）、`model_client.py`（直接多模态 API）、`vision_loop.py`（不确定性驱动的裁剪/掩码复查）、`offline_memory.py`（原病例库/检索/衰退适配）、`offline_run.py`（训练/测试、快照与恢复）。

原生存储基础保留：`case_library.py`、`store.py`、`retrieval.py`、`extensions.py`、`visual_memory.py`、`patient_experience.py`、`schemas.py`。其中 patient_experience 表示患者经历数据类别，不是患者 agent。旧数据库的历史表和版本不自动删除。

`server.py` 仅保留原存储工具的可选 stdio 兼容接口，批处理不会启动它，也没有患者问询工具。患者/医生医疗页面、模拟患者、问询状态机和 SDK 启动链已删除。
