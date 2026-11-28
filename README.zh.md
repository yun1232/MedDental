# MedDental

使用图片与固定患者文本预测牙釉质白斑的病因和治疗方案。有视觉不确定点时，生成当前原图的裁剪图或遮挡图，与原图一起再次推理。训练仅积累 memory，不更新模型权重；测试使用只读训练记忆。

| 模式 | 输入文本 | 始终作为标签 |
| --- | --- | --- |
| history | 既往史 | 病因、治疗方案 |
| clinical | 既往史、分布、质地、色调、均一性 | 病因、治疗方案 |

编号只绑定病例和图片，边界及其他未列出的列不输入模型。

```sh
python -m pip install -r integrations/oral-memory/requirements.txt
# 复制 .env.oral.example 为 .env.oral，设置模型接口。
python train.py --input-mode history
python test.py --input-mode history
python train.py --input-mode clinical
python test.py --input-mode clinical
```

两个入口直接调用模型 API、memory 和图片处理函数，不启动前端、业务后端、患者/医生客户端或 Node/SDK 子进程。旧患者模拟、问询循环和医疗页面已移除。

完整说明见 [离线流程](integrations/oral-memory/BATCH.zh.md)，验证范围见 [验证记录](integrations/oral-memory/BATCH-VERIFICATION.md)。默认输出在忽略的 `runs/vision/` 下，两种模式分别保存 memory 和结果。

原有病例库、版本化存储、检索和衰退分机制保留在 `integrations/oral-memory/`。上游 Harness/vendor 源码及其许可、依赖闭包单独保留，不参与本 Python 推理流程。
