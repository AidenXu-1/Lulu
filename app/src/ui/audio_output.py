"""Create playback outputs without a first-use PySide lock inversion."""

from PySide6.QtCore import QObject
from PySide6.QtMultimedia import QAudioOutput


def create_audio_output(parent=None) -> QAudioOutput:
    output = QAudioOutput(parent)
    # PySide 6.11 resolves disconnectNotify's Python override on first use.
    # FFmpeg can first call it while destroying a receiver on its audio thread,
    # holding a Qt connection mutex and waiting for the GIL. A simultaneous UI
    # show/repolish holds the GIL and can wait for that same mutex. Resolve the
    # unmodified native callbacks here, before handing the output to a player.
    # This connection is never emitted and only this connection is removed.
    connection = output.volumeChanged.connect(output.setVolume)
    QObject.disconnect(connection)
    return output
