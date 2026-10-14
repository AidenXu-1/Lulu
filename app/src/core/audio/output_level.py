"""Lulu output-only PCM gain. No model controls or time-varying processing."""
from __future__ import annotations

import array
import math
import os
from pathlib import Path
import sys
import tempfile
import wave

_BLOCK_FRAMES = 32768
_TARGET_RMS_DBFS = -24.0
_PEAK_CEILING_DBFS = -1.0
_MAX_BOOST_DB = 30.0
_QUIET_FLOOR_DBFS = -70.0


def _samples(raw: bytes) -> array.array:
    values = array.array('h', raw)
    if sys.byteorder != 'little':
        values.byteswap()
    return values


def level_pcm16_wav(path: Path, *, check_cancelled=lambda: None) -> dict:
    """Apply one gain to the entire completed file, with bounded memory.

    The target matches the accepted listening comparison, not a LUFS standard.
    A sample-peak cap takes priority over the RMS target. It prevents new PCM
    clipping; it cannot repair clipping/distortion already in the source.
    Cancellation or write failure leaves the original WAV intact.
    """
    path = Path(path)
    check_cancelled()
    with wave.open(str(path), 'rb') as source:
        params = source.getparams()
        if params.sampwidth != 2 or params.comptype != 'NONE' or params.nframes <= 0:
            raise ValueError('音量调整需要有效的 PCM16 WAV。')
        total = 0
        sum_squares = 0
        peak = 0
        while True:
            check_cancelled()
            raw = source.readframes(_BLOCK_FRAMES)
            if not raw:
                break
            if len(raw) % (2 * params.nchannels):
                raise ValueError('生成音频不完整，无法调整音量。')
            values = _samples(raw)
            total += len(values)
            sum_squares += sum(v * v for v in values)
            peak = max(peak, max(abs(v) for v in values))
        if total != params.nframes * params.nchannels:
            raise ValueError('生成音频不完整，无法调整音量。')
        rms = math.sqrt(sum_squares / total) / 32768
        rms_db = 20 * math.log10(rms) if rms else None
        meta = {
            'method': 'lulu_constant_gain_v1', 'applied': False,
            'target_rms_dbfs': _TARGET_RMS_DBFS,
            'peak_ceiling_dbfs': _PEAK_CEILING_DBFS,
            'input_rms_dbfs': rms_db, 'input_peak': peak / 32768,
            'gain_db': 0.0, 'peak_limited': False,
        }
        # Do not turn near-silent failed output into loud quantization/noise.
        if rms_db is None or rms_db < _QUIET_FLOOR_DBFS:
            return {**meta, 'reason': 'near_silence', 'output_rms_dbfs': rms_db,
                    'output_peak': peak / 32768}
        peak_ceiling = math.floor(32768 * 10 ** (_PEAK_CEILING_DBFS / 20))
        desired_gain = min(10 ** ((_TARGET_RMS_DBFS - rms_db) / 20),
                           10 ** (_MAX_BOOST_DB / 20))
        gain = min(desired_gain, peak_ceiling / peak)
        meta.update(gain_db=20 * math.log10(gain), peak_limited=gain < desired_gain)
        if abs(meta['gain_db']) < .001:
            return {**meta, 'output_rms_dbfs': rms_db, 'output_peak': peak / 32768}
        check_cancelled()
        source.rewind()
        temporary = None
        out_squares = 0
        out_peak = 0
        try:
            # Same directory ensures atomic replacement even for external output folders.
            with tempfile.NamedTemporaryFile(prefix='.lulu-level-', suffix='.wav',
                                             dir=path.parent, delete=False) as temp:
                temporary = Path(temp.name)
                with wave.open(temp, 'wb') as output:
                    output.setparams(params)
                    while True:
                        check_cancelled()
                        raw = source.readframes(_BLOCK_FRAMES)
                        if not raw:
                            break
                        values = _samples(raw)
                        adjusted = array.array('h', (round(v * gain) for v in values))
                        out_squares += sum(v * v for v in adjusted)
                        out_peak = max(out_peak, max(abs(v) for v in adjusted))
                        if sys.byteorder != 'little':
                            adjusted.byteswap()
                        output.writeframesraw(adjusted.tobytes())
                temp.flush()
                os.fsync(temp.fileno())
            check_cancelled()
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        output_rms = math.sqrt(out_squares / total) / 32768
        return {**meta, 'applied': True,
                'output_rms_dbfs': 20 * math.log10(output_rms) if output_rms else None,
                'output_peak': out_peak / 32768}
