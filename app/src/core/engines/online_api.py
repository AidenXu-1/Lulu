"""云端引擎:OpenAI 兼容的 /audio/transcriptions(覆盖 硅基流动 / Groq / OpenAI)。

⚠️ 选择云端 = 16k 单声道音频会上传到对应厂家。本地引擎才是默认且离线(见 ADR-0003/0007)。
本引擎只在用户显式选择云端时才会被构建调用;不缓存、不打印 key。
"""

from __future__ import annotations

from pathlib import Path

from ..models import Segment, Transcript
from .base import CancelCallback, ProgressCallback, TranscriptionEngine, _noop


class OpenAICompatEngine(TranscriptionEngine):
    requires_network = True

    def __init__(self, *, engine_id: str, display_name: str,
                 base_url: str, model: str, api_key: str | None):
        self.name = engine_id
        self.display_name = display_name
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = (api_key or "").strip()

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def test_connection(self) -> str:
        """轻量验证 key 是否有效(GET /models)。成功返回提示文案,失败抛异常。"""
        if not self._api_key:
            raise RuntimeError("请先填入 API Key。")
        import requests

        try:
            r = requests.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"连接失败(检查网络/代理):{exc}") from exc
        if r.status_code == 401:
            raise RuntimeError("API Key 无效或无权限(401)。")
        if r.status_code >= 400:
            raise RuntimeError(f"返回 {r.status_code}:{r.text[:200]}")
        return "服务连接成功，已取得模型列表。尚未测试音频转写；转写权限和所选模型能否使用，需要实际转写确认。"

    def transcribe(
        self,
        wav_path: Path,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> Transcript:
        progress = progress or _noop
        is_cancelled = is_cancelled or (lambda: False)
        if is_cancelled():
            raise InterruptedError("转录已取消")
        if not self._api_key:
            raise RuntimeError(
                f"{self.display_name} 还没配置 API Key。请在「AI 大模型配置」→「文案提取」→ 云端 API 里填入,或切回本地引擎。"
            )
        progress(f"上传音频到 {self.display_name}…", -1.0)
        # 优先要 verbose_json(带分段/时间戳),厂家不支持时退回纯 json
        data = self._request(wav_path, "verbose_json")
        if data is None:
            data = self._request(wav_path, "json")
        if is_cancelled():
            raise InterruptedError("转录已取消")
        progress("解析云端结果…", -1.0)
        return self._parse(data)

    # ------------------------------------------------------------------ #
    def _request(self, wav_path: Path, response_format: str) -> dict | None:
        import requests

        url = f"{self._base_url}/audio/transcriptions"
        try:
            with open(wav_path, "rb") as f:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    files={"file": (Path(wav_path).name, f, "audio/wav")},
                    data={"model": self._model, "response_format": response_format},
                    timeout=300,
                )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"连接 {self.display_name} 失败(检查网络 / 代理):{exc}"
            ) from exc

        if resp.status_code == 401:
            raise RuntimeError(f"{self.display_name}:API Key 无效或无权限(401)。请在「AI 大模型配置」→「文案提取」→ 云端 API 核对 Key。")
        if resp.status_code == 403:
            raise RuntimeError(f"{self.display_name}:无权限或余额不足(403)。")
        if resp.status_code == 429:
            if "insufficient_quota" in resp.text or "exceeded your current quota" in resp.text:
                raise RuntimeError(
                    f"{self.display_name}:账号余额 / 额度不足(429)。请到该平台充值后再用,或换其它引擎。"
                )
            raise RuntimeError(f"{self.display_name}:触发限流(429),稍后再试,或切回本地。")
        if resp.status_code >= 400:
            if response_format == "verbose_json":
                return None  # 让上层退回 json 再试一次
            raise RuntimeError(
                f"{self.display_name} 返回错误 {resp.status_code}:{resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"text": resp.text}

    def _parse(self, data: dict) -> Transcript:
        text = (data.get("text") or "").strip()
        segments: list[Segment] = []
        for s in data.get("segments") or []:
            seg_text = (s.get("text") or "").strip()
            if not seg_text:
                continue
            segments.append(
                Segment(
                    text=seg_text,
                    start=s.get("start") if isinstance(s.get("start"), (int, float)) else None,
                    end=s.get("end") if isinstance(s.get("end"), (int, float)) else None,
                )
            )
        if not text and segments:
            text = "".join(s.text for s in segments).strip()
        return Transcript(
            text=text,
            segments=segments,
            language=data.get("language"),
            engine=self.name,
        )
