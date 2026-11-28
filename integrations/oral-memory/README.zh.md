# 离线病例记忆与视觉复查

入口为根目录 `train.py`、`test.py`。完整输入边界、循环、训练/测试隔离和参数见 [流程说明](BATCH.zh.md)。

主流程模块：`batch_data.py`（输入白名单/标签拆分）、`model_client.py`（直接多模态 API）、`agents.py`（四角色提示词与严格输出契约）、`vision_loop.py`（视觉/检索/推理/每轮审查状态机、当前图像裁剪与掩码）、`offline_memory.py`（统一候选池、病例原始证据与标签、原生衰退适配）、`offline_run.py`（训练/测试、快照与恢复）。

原生存储基础保留：`case_library.py`、`store.py`、`retrieval.py`、`extensions.py`、`visual_memory.py`、`patient_experience.py`、`schemas.py`。其中 patient_experience 表示患者经历数据类别，不是患者 agent。旧数据库的历史表和版本不自动删除。

`server.py` 仅保留原存储工具的可选 stdio 兼容接口，批处理不会启动它，也没有患者问询工具。患者/医生医疗页面、模拟患者、问询状态机和 SDK 启动链已删除。

诊断审查每轮运行，训练和测试一致；区域视觉记忆价值审查使用单独上下文，训练时才允许反馈。原生 `retrieval.unified()` 是本批处理唯一检索入口，旧分阶段存储接口只为兼容保留。
