"""Current product processes only; polite GUI quit and normal CLI termination."""
from __future__ import annotations
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import time

APP=Path('/Applications/Lulu.app')


def product_processes():
    lib=ctypes.CDLL('/usr/lib/libproc.dylib')
    lib.proc_pidpath.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32]
    lib.proc_pidpath.restype=ctypes.c_int
    result=subprocess.run(['/bin/ps','-axo','pid='],capture_output=True,text=True,check=True)
    wanted={str(APP/'Contents/MacOS'/name):name for name in ('Lulu','lulu-agent-cli','lulu-agent-core')}
    found={}
    for item in result.stdout.split():
        pid=int(item)
        if pid==os.getpid():continue
        buffer=ctypes.create_string_buffer(4096)
        if lib.proc_pidpath(pid,buffer,len(buffer))>0:
            name=wanted.get(os.fsdecode(buffer.value))
            if name:found[pid]=name
    return found


def _terminate_gui(pid):
    ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
    objc=ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    objc.objc_getClass.argtypes=[ctypes.c_char_p];objc.objc_getClass.restype=ctypes.c_void_p
    objc.sel_registerName.argtypes=[ctypes.c_char_p];objc.sel_registerName.restype=ctypes.c_void_p
    address=ctypes.cast(objc.objc_msgSend,ctypes.c_void_p).value
    lookup=ctypes.CFUNCTYPE(ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int)(address)
    terminate=ctypes.CFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)(address)
    app=lookup(objc.objc_getClass(b'NSRunningApplication'),objc.sel_registerName(b'runningApplicationWithProcessIdentifier:'),pid)
    if not app:
        raise RuntimeError('PRODUCT_GUI_QUIT_REQUIRED')
    # Qt can decline the immediate AppKit reply while completing normal quit.
    # The existing bounded process observation decides whether it really exited.
    terminate(app,objc.sel_registerName(b'terminate'))


def clients_stopped():
    return not any(name!='lulu-agent-core' for name in product_processes().values())


def stop_clients():
    for pid,name in product_processes().items():
        if name=='Lulu':_terminate_gui(pid)
        elif name=='lulu-agent-cli':
            try:os.kill(pid,signal.SIGTERM)
            except ProcessLookupError:pass
    deadline=time.monotonic()+30
    while not clients_stopped() and time.monotonic()<deadline:time.sleep(0.2)
    if not clients_stopped():raise RuntimeError('PRODUCT_CLIENTS_STILL_RUNNING')


def product_service_controller():
    from .effects import CurrentUserLaunchctlService
    class ProductService(CurrentUserLaunchctlService):
        def stop(self):
            super().stop()
            deadline=time.monotonic()+30
            while any(name=='lulu-agent-core' for name in product_processes().values()) and time.monotonic()<deadline:
                time.sleep(0.2)
            if any(name=='lulu-agent-core' for name in product_processes().values()):
                raise RuntimeError('PRODUCT_CORE_STILL_RUNNING')
    return ProductService()
