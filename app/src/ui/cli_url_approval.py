"""Same-bundle GUI host for one bounded public-link confirmation."""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from typing import BinaryIO, Callable


class PointerConfirmationGate:
    """Accept only a spontaneous primary-pointer release."""

    def __init__(self) -> None:
        self._confirmed = False

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    def confirm(self, *, spontaneous: bool, button: str) -> bool:
        if spontaneous is True and button == "left" and not self._confirmed:
            self._confirmed = True
            return True
        return False


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _dialog_items(plan: dict[str, object]) -> tuple[tuple[str, str], ...]:
    typed = plan["typed_request"]
    effects = plan["effects"]
    targets = plan["targets"]
    platform = str(typed.get("source", "")).split("://", 1)[-1].split("/", 1)[0]
    subtitle = (
        "优先字幕；无字幕时"
        + ("本地转录" if typed.get("subtitle_fallback") == "local_transcription" else "停止")
        if typed.get("prefer_subtitles")
        else "不优先字幕；使用本地转录"
    )
    media_mode = "视频" if typed.get("media_mode") == "video" else "音频"
    return (
        ("平台/链接", f"{platform} · {typed.get('source', '')}"),
        ("引擎", "本地 SenseVoice"),
        ("字幕与回退", subtitle),
        ("下载类型", media_mode),
        ("保存媒体", "是" if typed.get("save_media") else "否"),
        ("保存封面", "是" if typed.get("save_cover") else "否"),
        ("逐字稿格式", str(typed.get("format", "txt")).upper()),
        ("保存位置", str(targets.get("transcript", typed.get("output_destination", "")))),
        (
            "联网/磁盘效果",
            f"联网：{'会' if effects.get('network') else '不会'}；"
            f"逐字稿：{'会写入' if effects.get('writes_shared_transcript') else '不写入'}；"
            f"媒体：{'会写入' if effects.get('writes_media') else '不写入'}；"
            f"封面：{'会写入' if effects.get('writes_cover') else '不写入'}",
        ),
    )


def _show_link_approval_dialog(plan: dict[str, object]) -> dict[str, object]:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QScrollArea,
        QVBoxLayout,
        QWidget,
    )

    app = QApplication.instance() or QApplication(["Lulu"])
    gate = PointerConfirmationGate()
    state = {"timed_out": False}

    class PointerOnlyButton(QPushButton):
        def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
            accepted = bool(
                event.spontaneous()
                and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())
            )
            super().mouseReleaseEvent(event)
            if accepted and gate.confirm(spontaneous=True, button="left"):
                dialog.accept()

    dialog = QDialog()
    dialog.setWindowTitle("Lulu：确认联网转录")
    dialog.setModal(True)
    dialog.setMinimumWidth(620)
    layout = QVBoxLayout(dialog)
    title = QLabel("请确认这一次链接转录")
    title.setStyleSheet("font-size: 18px; font-weight: 600;")
    layout.addWidget(title)
    notice = QLabel("确认后 Lulu 会联网取得内容，并按下列设置写入本机。聊天中的“OK”只确认配置。")
    notice.setWordWrap(True)
    layout.addWidget(notice)

    rows = QWidget()
    rows_layout = QVBoxLayout(rows)
    for label, value in _dialog_items(plan):
        row = QLabel(f"<b>{label}</b><br>{value}")
        row.setTextFormat(Qt.TextFormat.RichText)
        row.setWordWrap(True)
        row.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rows_layout.addWidget(row)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(rows)
    layout.addWidget(scroll)

    actions = QHBoxLayout()
    actions.addStretch(1)
    cancel = QPushButton("取消")
    confirm = PointerOnlyButton("确认联网执行")
    confirm.setAutoDefault(False)
    confirm.setDefault(False)
    cancel.setAutoDefault(True)
    cancel.setDefault(True)
    cancel.clicked.connect(dialog.reject)
    actions.addWidget(cancel)
    actions.addWidget(confirm)
    layout.addLayout(actions)
    cancel.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def time_out() -> None:
        state["timed_out"] = True
        dialog.reject()

    timer = QTimer(dialog)
    timer.setSingleShot(True)
    timer.timeout.connect(time_out)
    timer.start(120_000)
    dialog.exec()
    timer.stop()
    app.processEvents()
    if gate.confirmed:
        return {"decision": "confirmed", "pointer_verified": True}
    if state["timed_out"]:
        return {"decision": "timed_out", "pointer_verified": False}
    return {"decision": "cancelled", "pointer_verified": False}


def run_link_approval_host(
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    dialog_runner: Callable[[dict[str, object]], dict[str, object]] | None = None,
) -> int:
    """Read one canonical request and write one canonical response, with no logs."""
    from ..core.cli_runtime.link_host_bridge import (
        MAX_HOST_MESSAGE_BYTES,
        decode_host_frame,
        encode_host_frame,
        validate_host_request,
    )

    input_stream = stdin or sys.stdin.buffer
    output_stream = stdout or sys.stdout.buffer
    try:
        frame = input_stream.read(MAX_HOST_MESSAGE_BYTES + 5)
        request = decode_host_frame(frame)
        validate_host_request(request)
        request_digest = hashlib.sha256(_canonical_json(request)).hexdigest()
        outcome = (dialog_runner or _show_link_approval_dialog)(request["plan"])
        if type(outcome) is not dict or frozenset(outcome) != {
            "decision",
            "pointer_verified",
        }:
            return 2
        decision = outcome.get("decision")
        pointer_verified = outcome.get("pointer_verified") is True
        if decision == "confirmed" and not pointer_verified:
            decision = "cancelled"
        if decision not in {"confirmed", "cancelled", "timed_out"}:
            return 2
        response = {
            "schema_id": "lulu.link-approval-host-response/v1",
            "schema_revision": 1,
            "request_id": request["request_id"],
            "request_digest": request_digest,
            "decision": decision,
            "action_source": "spontaneous_pointer" if decision == "confirmed" else "none",
            "action_nonce": secrets.token_urlsafe(24) if decision == "confirmed" else "",
        }
        output_stream.write(encode_host_frame(response))
        output_stream.flush()
        return 0
    except Exception:  # noqa: BLE001 - closed child exit, never leak request
        return 2
