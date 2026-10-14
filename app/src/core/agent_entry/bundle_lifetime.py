"""Exit this Core when its App is replaced, including by a legacy updater."""
import os
from pathlib import Path
import select
import threading


def watch_app_replacement(app, on_replacement):
    """Kernel notifications only; no polling of bundle bytes or other processes."""
    app = Path(app)
    queue = select.kqueue()
    descriptors = []
    cancelled = threading.Event()
    try:
        for path, writes in ((app, False), (app/'Contents/MacOS/lulu-agent-core', True)):
            descriptor = os.open(path, getattr(os, 'O_EVTONLY', os.O_RDONLY))
            descriptors.append(descriptor)
            mask = select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE
            if writes:
                mask |= select.KQ_NOTE_WRITE
            event = select.kevent(descriptor, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR, fflags=mask)
            queue.control([event], 0, 0)
    except Exception:
        for descriptor in descriptors:
            os.close(descriptor)
        queue.close()
        raise

    def observe():
        try:
            while not cancelled.is_set():
                if queue.control(None, 2, 1) and not cancelled.is_set():
                    on_replacement()
                    return
        finally:
            queue.close()
            for descriptor in descriptors:
                os.close(descriptor)
    thread = threading.Thread(target=observe, name='lulu-bundle-lifetime', daemon=True)
    thread.start()
    def stop():
        cancelled.set()
        thread.join(timeout=2)
    return stop
