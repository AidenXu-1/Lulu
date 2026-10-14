"""Recording backend contracts.

This package defines the platform-neutral boundary for Lulu recording sources.
Concrete macOS / Windows capture backends live outside the UI and plug into this
contract.
"""

from .base import (
    ProgressCallback,
    RecordingBackend,
    RecordingRequest,
    RecordingResult,
    RecordingSession,
    RecordingSource,
    RecordingTrack,
    RecordingTrackKind,
    RecordingUse,
    _noop,
)
from .audio_tools import RecordingAudioError, analyze_recording_file, mix_recording_tracks
from .macos_system_audio import (
    MacOSSystemAudioBackend,
    RecordingBackendError,
    ensure_macos_system_audio_helper,
    is_macos_system_audio_available,
)

__all__ = [
    "ProgressCallback",
    "RecordingBackend",
    "RecordingRequest",
    "RecordingResult",
    "RecordingSession",
    "RecordingSource",
    "RecordingTrack",
    "RecordingTrackKind",
    "RecordingUse",
    "MacOSSystemAudioBackend",
    "RecordingAudioError",
    "RecordingBackendError",
    "analyze_recording_file",
    "ensure_macos_system_audio_helper",
    "is_macos_system_audio_available",
    "mix_recording_tracks",
    "_noop",
]
