"""语音合成主链路。"""

from __future__ import annotations

from pathlib import Path

from .base import SynthesisRequest, SynthesisResult
from .registry import get_synthesis_engine
from .voices import get_voice


def synthesize_text(
    text: str,
    voice_id: str,
    output_path: str | Path,
    output_format: str = "wav",
    progress=None,
    is_cancelled=None,
    engine: str | None = None,
    engine_options: dict | None = None,
    voice_profile=None,
) -> SynthesisResult:
    voice = voice_profile if voice_profile is not None else (get_voice(voice_id) if voice_id else None)
    design = engine == 'local_voxcpm2' and (engine_options or {}).get('mode') == 'design'
    design = design or (engine == 'local_qwen3_tts' and (engine_options or {}).get('mode') in ('official','design'))
    if voice is None and not design:
        raise RuntimeError("请选择一个已保存的音色。")
    request = SynthesisRequest(
        text=text,
        voice=voice,
        output_path=Path(output_path),
        output_format=output_format,
        engine_options=engine_options or {},
    )
    # 引擎由配音页面板显式指定(engine);未指定时回退音色自带的 voice.engine。
    # 音色本质是「参考音频+参考文本」,可跨引擎复用,故以面板选择为准。
    selected_engine = engine or voice.engine
    if selected_engine != 'local_qwen3_tts':
        from .qwen3_subprocess import shutdown_persistent_worker
        if not shutdown_persistent_worker():
            raise RuntimeError('Qwen 正在生成，请先等待或取消。')
    from .resource_lock import voxcpm2_resource
    with voxcpm2_resource():
        return get_synthesis_engine(selected_engine).synthesize(request, progress, is_cancelled)
