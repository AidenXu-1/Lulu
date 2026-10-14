"""Platform-neutral recording backend contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence

ProgressCallback = Callable[[str, float], None]


def _noop(_msg: str, _frac: float) -> None:
    pass


class RecordingSource(str, Enum):
    """User-facing source choices, independent of platform implementation."""

    MICROPHONE = "microphone"
    SYSTEM_AUDIO = "system_audio"
    MIXED = "mixed"


class RecordingUse(str, Enum):
    """Where a finished recording is intended to go next."""

    TRANSCRIPTION = "transcription"
    VOICE_SAMPLE = "voice_sample"
    EMOTION_REFERENCE = "emotion_reference"


class RecordingTrackKind(str, Enum):
    """Internal track roles returned by a backend."""

    MICROPHONE = "microphone"
    SYSTEM_AUDIO = "system_audio"
    MIXED = "mixed"


@dataclass(frozen=True)
class RecordingRequest:
    """A request to start a recording session.

    The UI decides the source and destination purpose. The backend decides how
    that source maps to platform APIs such as QtMultimedia, ScreenCaptureKit, or
    WASAPI loopback.
    """

    source: RecordingSource
    output_path: Path
    use: RecordingUse = RecordingUse.TRANSCRIPTION
    max_duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordingTrack:
    """One concrete audio track produced by a backend."""

    kind: RecordingTrackKind
    path: Path
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    is_silent: bool | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordingResult:
    """A completed recording that upper layers can route by purpose."""

    source: RecordingSource
    output_path: Path
    tracks: tuple[RecordingTrack, ...]
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    warnings: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def has_audio(self) -> bool:
        """True when at least one returned track is not known to be silent."""

        return any(track.is_silent is not True for track in self.tracks)


class RecordingSession(Protocol):
    """A running recording session.

    Implementations may wrap Qt objects, a native helper process, or a Windows
    loopback worker. They must not require callers to import platform APIs.
    """

    @property
    def request(self) -> RecordingRequest:
        raise NotImplementedError

    def stop(self, progress: ProgressCallback | None = None) -> RecordingResult:
        """Stop and save the recording."""
        raise NotImplementedError

    def cancel(self) -> None:
        """Cancel the recording and clean temporary files when possible."""
        raise NotImplementedError

    def is_active(self) -> bool:
        raise NotImplementedError


class RecordingBackend(Protocol):
    """Backend contract used by UI / queue / library routing code."""

    backend_id: str
    display_name: str
    platforms: Sequence[str]

    def supports(self, source: RecordingSource) -> bool:
        raise NotImplementedError

    def start(
        self,
        request: RecordingRequest,
        progress: ProgressCallback | None = None,
    ) -> RecordingSession:
        raise NotImplementedError
