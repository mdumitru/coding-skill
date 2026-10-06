#!/usr/bin/env python3
"""Atomic UTF-8 state replacement and exclusive, evidence-preserving locks."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator


class StateError(Exception):
    """State cannot safely be read or changed."""


def atomic_write(path: Path, content: str, *, exclusive: bool = False) -> None:
    """Replace a file atomically; optionally refuse any existing destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise StateError(f"refusing symlink state destination: {path}")
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def plan_lock(plan: Path) -> Iterator[None]:
    """Refuse concurrent writers; leave stale lock evidence for manual review."""
    directory = plan.parent / '.state'
    if directory.is_symlink() or not directory.resolve().is_relative_to(plan.parent):
        raise StateError('state directory escapes the plan root')
    directory.mkdir(exist_ok=True)
    lock = directory / f'{plan.stem}.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise StateError(f'plan is locked: {lock}; inspect its PID and preserve evidence before removing a stale lock') from exc
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'pid': os.getpid(), 'plan': str(plan)}, stream, sort_keys=True)
        yield
    finally:
        lock.unlink()
