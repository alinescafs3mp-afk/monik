"""Bounded optimistic snapshots of source metadata, without source SQLite handles.

The source DB and WAL are copied with ordinary read-only file descriptors. SQLite
opens only the temporary private copy. A changing source or rollback journal is
unavailable, not silently repaired. Stable file signatures plus quick_check are
readability evidence, not a transaction boundary supplied by Codex.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import tempfile
import time

from .config import forbidden


def open_regular(path: Path) -> int:
    """Reject symbolic links in every component after allowlist resolution."""
    path = Path(path).absolute()
    if forbidden(path):
        raise PermissionError('Excluded source path')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise PermissionError('Source is not a regular file')
        return fd
    finally:
        os.close(directory)


def _signature(path: Path):
    try:
        s = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(s.st_mode):
        raise PermissionError('Metadata source must be a regular file')
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def source_signature(path: Path):
    return tuple(_signature(Path(str(path) + suffix)) for suffix in ('', '-wal', '-journal'))


@contextmanager
def private_snapshot(path: Path, data_dir: Path, byte_budget: int):
    path = Path(path).absolute()
    before = source_signature(path)
    if before[0] is None:
        raise FileNotFoundError(path)
    if before[2] is not None and before[2][2] > 0:
        raise ValueError('Rollback journal present; metadata snapshot deferred')
    if sum(s[2] for s in before[:2] if s is not None) > byte_budget:
        raise ValueError('Metadata snapshot byte budget exceeded')
    deadline = time.monotonic() + 2
    with tempfile.TemporaryDirectory(prefix='.metadata-', dir=data_dir) as directory:
        target = Path(directory) / 'state.sqlite'
        for suffix, expected in zip(('', '-wal'), before[:2]):
            if expected is None:
                continue
            source = Path(str(path) + suffix)
            fd = open_regular(source)
            try:
                s = os.fstat(fd)
                actual = (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                if actual != expected:
                    raise ValueError('Metadata changed during snapshot')
                out = os.open(str(target) + suffix, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(out, 'wb') as stream:
                    remaining = s.st_size
                    while remaining:
                        if time.monotonic() > deadline:
                            raise ValueError('Metadata snapshot time budget exceeded')
                        chunk = os.read(fd, min(1048576, remaining))
                        if not chunk:
                            raise ValueError('Metadata changed during snapshot')
                        stream.write(chunk)
                        remaining -= len(chunk)
            finally:
                os.close(fd)
        if source_signature(path) != before:
            raise ValueError('Metadata changed during snapshot')
        yield target
