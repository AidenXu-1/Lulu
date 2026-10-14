"""Read-only ASR discovery using the same model registry and storage as the GUI."""
from importlib.util import find_spec

from .engines.local_models import LOCAL_MODELS

DEFAULT_ENGINE = 'local_sensevoice'
MODEL_MESSAGES = {
    'MODEL_NOT_INSTALLED': '本地转录模型未安装，请在 Lulu 的 AI 大模型设置中安装所选模型。',
    'MODEL_INSTALLATION_INCOMPLETE': '本地转录模型文件不完整或安装校验未通过，请在 Lulu 中检查或修复所选模型。',
    'MODEL_RUNTIME_UNAVAILABLE': '本地转录模型已安装，但当前运行环境不支持，请查询 transcribe status 查看原因。',
    'MODEL_READINESS_PROBE_FAILED': '无法检查本地转录模型状态，请在 Lulu 中检查模型目录和运行环境。',
}


class LocalModelUnavailable(RuntimeError):
    def __init__(self, code, reason_code):
        self.code, self.reason_code = code, reason_code
        super().__init__(MODEL_MESSAGES[reason_code])


def normalize_engine(engine):
    # The original GUI DTO used this short name. CLI exposes canonical IDs only.
    if engine == 'sensevoice':
        engine = DEFAULT_ENGINE
    if type(engine) is not str or engine not in LOCAL_MODELS:
        raise ValueError('请选择 Lulu 支持的本地转录模型')
    return engine


def model_status(engine):
    from . import models_manager as mm
    engine = normalize_engine(engine)
    model = LOCAL_MODELS[engine]
    result = dict(engine=engine, name=model.display_name, timestamps=model.timestamps,
                  state='ready', installed=True, ready=True, reason_code=None, message='模型已就绪。')
    try:
        if not mm.is_downloaded(engine):
            path = mm.model_path(engine)
            has_files = path.is_symlink() or (path.exists() and
                (not path.is_dir() or next(path.iterdir(), None) is not None))
            reason = 'MODEL_INSTALLATION_INCOMPLETE' if has_files else 'MODEL_NOT_INSTALLED'
            result.update(state='needs_repair' if has_files else 'not_installed', installed=False,
                          ready=False, reason_code=reason, message=MODEL_MESSAGES[reason])
        else:
            issue = mm.runtime_issue(engine)
            dependencies = {'funasr': ('torch', 'funasr'),
                            'transformers': ('torch', 'qwen_asr'), 'mlx': ('mlx', 'mlx_whisper')}
            if not issue:
                missing = [name for name in dependencies[model.runtime] if find_spec(name) is None]
                if missing:
                    issue = '运行依赖缺失：' + '、'.join(missing) + '。请更新 Lulu。'
            if issue or not model.ready:
                result.update(state='runtime_unavailable', ready=False,
                              reason_code='MODEL_RUNTIME_UNAVAILABLE',
                              message=issue or '当前 Lulu 尚未提供此模型的运行适配。')
    except (OSError, RuntimeError, ValueError, ImportError):
        result.update(state='check_failed', installed=None, ready=False,
                      reason_code='MODEL_READINESS_PROBE_FAILED',
                      message=MODEL_MESSAGES['MODEL_READINESS_PROBE_FAILED'])
    return result


def probe_local_model_readiness(engine):
    value = model_status(engine)
    if not value['ready']:
        code = 'MODEL_NOT_READY' if value['state'] in {'not_installed', 'needs_repair'} else 'RUNTIME_NOT_READY'
        raise LocalModelUnavailable(code, value['reason_code'])
    return True
