"""检索评测离线包：语料快照、金标集、IR 指标与消融评测主入口。

不随应用启动加载，也不进 pytest 默认套件（依赖真实 embedding/LLM API）；
在 backend 目录下以 `python -m evals.<module>` 方式手动执行。
"""
