"""Replace complete files atomically and serialize cooperating ledger writers."""

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def atomic_output(destination, *, backup=True):
    """Yield a same-directory staging path; leave the last good file on failure."""
    target = Path(destination).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(prefix=f".{target.stem}-", suffix=target.suffix, dir=target.parent)
    os.close(descriptor)
    backup_staged = None
    try:
        yield staged
        with open(staged, "r+b") as handle:
            os.fsync(handle.fileno())
        if backup and target.exists():
            descriptor, backup_staged = tempfile.mkstemp(prefix=f".{target.stem}-backup-", dir=target.parent)
            os.close(descriptor)
            shutil.copyfile(target, backup_staged)
            with open(backup_staged, "r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(backup_staged, str(target) + ".bak")
        os.replace(staged, target)
    finally:
        for temporary in (staged, backup_staged):
            if temporary and os.path.exists(temporary):
                os.remove(temporary)


def save_json_atomic(data, destination):
    with atomic_output(destination) as staged:
        with open(staged, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4)


@contextmanager
def ledger_lock(ledger_path):
    """OS-released advisory lock, including after a killed process. Fail fast."""
    lock_path = Path(str(ledger_path) + ".lock").absolute()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Another pull or data-control operation is using {ledger_path}") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
