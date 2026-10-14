"""Closed DTOs for one approved public-link transcription operation."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class LinkTranscriptionRequestV1:
    source: str
    engine: str = "local_sensevoice"
    format: str = "txt"
    prefer_subtitles: bool = False
    subtitle_fallback: str = "local_transcription"
    save_media: bool = False
    media_mode: str = "audio"
    media_directory: str | None = None
    save_cover: bool = False
    output_destination: str = "shared_lulu_transcript_default"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LinkTranscriptionPlanV1:
    plan_version: str
    capability_ids: tuple[str, ...]
    installation_id: str
    typed_request: LinkTranscriptionRequestV1
    effects: dict[str, object]
    targets: dict[str, object]
    selected_profiles: dict[str, str]
    idempotency: dict[str, object]
    missing_questions: tuple[str, ...]
    plan_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_version": self.plan_version,
            "capability_ids": list(self.capability_ids),
            "installation_id": self.installation_id,
            "typed_request": self.typed_request.as_dict(),
            "effects": dict(self.effects),
            "targets": dict(self.targets),
            "selected_profiles": dict(self.selected_profiles),
            "idempotency": dict(self.idempotency),
            "missing_questions": list(self.missing_questions),
            "plan_digest": self.plan_digest,
        }
