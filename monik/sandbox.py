"""Linux Landlock write boundary. No source file descriptors exist when this is applied.

Reference: https://docs.kernel.org/userspace-api/landlock.html
ABI >= 3 is mandatory for production serving, including truncate protection.
The application permits file mutations only underneath its private data directory.
This is a filesystem boundary, not a general promise of process/network isolation.
"""
from __future__ import annotations
import ctypes
import os
import platform
import threading
from pathlib import Path

CREATE, ADD, RESTRICT = 444, 445, 446
# READ_FILE, READ_DIR and EXECUTE are deliberately unhandled, not forbidden.
WRITE_RIGHTS = sum(1 << bit for bit in (1,4,5,6,7,8,9,10,11,12,13,14))

class Ruleset(ctypes.Structure):
    _fields_=[('handled_access_fs',ctypes.c_uint64)]

class Beneath(ctypes.Structure):
    _pack_=1
    _fields_=[('allowed_access',ctypes.c_uint64),('parent_fd',ctypes.c_int32)]


def abi() -> int:
    if platform.system()!='Linux' or platform.machine() not in ('x86_64','aarch64'):
        return 0
    libc=ctypes.CDLL(None,use_errno=True)
    return max(0,int(libc.syscall(CREATE,0,0,1)))


def enforce(data_dir: Path) -> int:
    """Fail closed. Apply once, before Uvicorn/collector threads are created."""
    version=abi()
    if version<3:
        raise RuntimeError('Linux Landlock ABI >= 3 is required; no unsafe fallback is enabled')
    if threading.active_count()!=1:
        raise RuntimeError('Filesystem restriction must be installed before threads start')
    data_dir=Path(data_dir).resolve(strict=True)
    libc=ctypes.CDLL(None,use_errno=True)
    attr=Ruleset(WRITE_RIGHTS)
    rules=int(libc.syscall(CREATE,ctypes.byref(attr),ctypes.sizeof(attr),0))
    if rules<0: raise OSError(ctypes.get_errno(),'Cannot create Landlock ruleset')
    directory=os.open(data_dir,os.O_PATH|os.O_CLOEXEC)
    try:
        rule=Beneath(WRITE_RIGHTS,directory)
        if libc.syscall(ADD,rules,1,ctypes.byref(rule),0)<0:
            raise OSError(ctypes.get_errno(),'Cannot allow private data directory')
        if libc.prctl(38,1,0,0,0)<0:
            raise OSError(ctypes.get_errno(),'Cannot set no_new_privs')
        if libc.syscall(RESTRICT,rules,0)<0:
            raise OSError(ctypes.get_errno(),'Cannot enforce filesystem boundary')
    finally:
        os.close(directory);os.close(rules)
    return version
