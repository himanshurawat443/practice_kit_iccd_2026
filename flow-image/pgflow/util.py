"""Shared helpers: subprocess with timeout, hashing, JSON, stage results."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CANCEL = threading.Event()  # set to abort every running command


class FlowError(Exception):
    """A stage failed in a way that rejects the submission."""


@dataclasses.dataclass
class CmdResult:
    cmd: list[str]
    returncode: int
    timed_out: bool
    seconds: float
    log: str  # path of the combined stdout/stderr log

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tail(self, n: int = 40) -> str:
        try:
            lines = Path(self.log).read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])


def sandbox_prefix() -> list[str]:
    """Command prefix that drops privileges for untrusted code.

    Active only when PGFLOW_SANDBOX_USER is set and we run as root (server
    image). In the participant image the participant runs their own code.
    """
    user = os.environ.get("PGFLOW_SANDBOX_USER")
    if not user or os.geteuid() != 0 or not shutil.which("setpriv"):
        return []
    return ["setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups",
            "--no-new-privs", "--"]


def run_cmd(cmd: Sequence[str], *, cwd: str | os.PathLike | None = None,
            timeout: float | None = None, env: Mapping[str, str] | None = None,
            log: str | os.PathLike, sandbox: bool = False,
            append: bool = False, cancel: "threading.Event | None" = None) -> CmdResult:
    """Run a command in its own process group, kill the group on timeout."""
    full = list(cmd)
    merged = dict(os.environ)
    if env:
        merged.update(env)
    if sandbox and sandbox_prefix():
        full = sandbox_prefix() + full
        merged.update({"HOME": str(cwd or "/tmp"), "CCACHE_DISABLE": "1",
                       "XDG_CACHE_HOME": str(cwd or "/tmp")})
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    timed_out = False
    with open(log, "a" if append else "w") as fh:
        fh.write("$ " + " ".join(str(c) for c in full) + "\n")
        fh.flush()
        proc = subprocess.Popen(full, cwd=cwd, env=merged, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
        cancel = cancel or CANCEL
        rc = None
        while rc is None:
            try:
                rc = proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                over = timeout is not None and time.monotonic() - t0 > timeout
                if over or cancel.is_set():
                    timed_out = over
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    rc = proc.wait()
                    fh.write("\n[pgflow] " + (f"timeout after {timeout:.0f} s" if over else "cancelled")
                             + ", process group killed\n")
                    if not over:
                        rc = -9
    return CmdResult([str(c) for c in full], rc, timed_out,
                     time.monotonic() - t0, str(log))


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(root: str | os.PathLike, exclude: Iterable[str] = ()) -> str:
    """Stable hash of a directory: relative paths and file contents."""
    root = Path(root)
    excl = set(exclude)
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if any(part in excl for part in p.relative_to(root).parts):
            continue
        if p.is_file() and not p.is_symlink():
            h.update(rel.encode() + b"\0" + sha256_file(p).encode() + b"\n")
    return h.hexdigest()


def write_json(path: str | os.PathLike, obj: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=False, default=str) + "\n")
    tmp.replace(path)


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


@dataclasses.dataclass
class StageResult:
    name: str
    status: str = "skipped"  # pass | fail | reject | error | skipped
    seconds: float = 0.0
    message: str = ""
    details: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def elapsed(self) -> float:
        return round(time.monotonic() - self.t0, 2)


def tool_version(cmd: Sequence[str]) -> str:
    try:
        out = subprocess.run(list(cmd), capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else ""
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({exc.__class__.__name__})"


def cpu_jobs(requested: int | None) -> int:
    if requested and requested > 0:
        return requested
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        return os.cpu_count() or 1


def sandbox_dir(path: str | os.PathLike, sandbox: bool) -> Path:
    """Create a work directory the sandbox user may write (server image).

    Everything else (submission sources, hidden material, OUTDIR) stays owned
    by root, so untrusted code cannot alter what is scored.
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    user = os.environ.get("PGFLOW_SANDBOX_USER")
    if sandbox and user and os.geteuid() == 0:
        subprocess.run(["chown", "-R", f"{user}:{user}", str(p)], check=False)
    return p
