"""转录引擎层。

所有引擎实现统一抽象 TranscriptionEngine(base.py),
UI 与 pipeline 只依赖抽象,由 registry.py 按配置选择具体实现。
见 ADR-0003。
"""
