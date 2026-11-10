"""Safe unpacking of submission archives (zip, tar, tar.gz).

Rules:
  * only regular files and directories; no symlinks, hard links, devices, FIFOs
  * no absolute paths, no '..' components, no backslashes or NUL in names
  * limits on member count, per-file size, total size and compression ratio
  * if every member sits under one top-level folder, that folder is stripped
  * only the known top-level entries are kept (rtl/, tests/, assertions/,
    report.md, trajectory/); anything else is dropped with a warning
"""

from __future__ import annotations

import dataclasses
import os
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


@dataclasses.dataclass
class Limits:
    max_members: int = 2000
    max_file_bytes: int = 20 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    max_ratio: float = 200.0  # uncompressed / compressed, whole archive
    max_archive_bytes: int = 50 * 1024 * 1024


ALLOWED_TOP = {"rtl", "tests", "assertions", "report.md", "trajectory"}
REQUIRED = ["rtl", "report.md"]


class ArchiveError(Exception):
    pass


@dataclasses.dataclass
class Member:
    name: str
    is_dir: bool
    size: int
    read: object  # callable returning bytes


def _check_name(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise ArchiveError(f"illegal member name {name!r}")
    p = PurePosixPath(name)
    if p.is_absolute() or name.startswith("/"):
        raise ArchiveError(f"absolute path in archive: {name}")
    parts = [x for x in p.parts if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise ArchiveError(f"path traversal in archive: {name}")
    if not parts:
        raise ArchiveError(f"empty member name {name!r}")
    return PurePosixPath(*parts)


def _members_tar(path: Path) -> list[Member]:
    try:
        tf = tarfile.open(path, "r:*")
    except tarfile.TarError as exc:
        raise ArchiveError(f"not a readable tar archive: {exc}") from exc
    out = []
    for ti in tf.getmembers():
        if ti.isdir():
            out.append(Member(ti.name, True, 0, None))
        elif ti.isreg():
            out.append(Member(ti.name, False, ti.size,
                              (lambda t=ti: tf.extractfile(t).read())))
        elif ti.name.startswith("._") or "/._" in ti.name:
            continue  # macOS resource forks
        else:
            kind = "symlink" if ti.issym() else "hard link" if ti.islnk() else "special file"
            raise ArchiveError(f"{kind} not allowed in archive: {ti.name}")
    return out


def _members_zip(path: Path) -> list[Member]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a readable zip archive: {exc}") from exc
    out = []
    for zi in zf.infolist():
        mode = (zi.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            raise ArchiveError(f"symlink not allowed in archive: {zi.filename}")
        if zi.flag_bits & 0x1:
            raise ArchiveError(f"encrypted member not allowed: {zi.filename}")
        if zi.is_dir():
            out.append(Member(zi.filename, True, 0, None))
        else:
            out.append(Member(zi.filename, False, zi.file_size,
                              (lambda z=zi: zf.read(z))))
    return out


def list_members(path: Path) -> list[Member]:
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic.startswith(b"PK"):
        return _members_zip(path)
    return _members_tar(path)


def safe_extract(archive: str | os.PathLike, dest: str | os.PathLike,
                 limits: Limits | None = None) -> dict:
    """Extract a submission archive into dest. Returns a summary dict."""
    limits = limits or Limits()
    archive = Path(archive)
    dest = Path(dest)
    size = archive.stat().st_size
    if size > limits.max_archive_bytes:
        raise ArchiveError(f"archive is {size} bytes, limit {limits.max_archive_bytes}")
    members = [m for m in list_members(archive)
               if not PurePosixPath(m.name).name.startswith("._")
               and "__MACOSX" not in m.name]
    if len(members) > limits.max_members:
        raise ArchiveError(f"archive has {len(members)} members, limit {limits.max_members}")
    paths = [(_check_name(m.name), m) for m in members]
    total = 0
    for p, m in paths:
        if not m.is_dir:
            if m.size > limits.max_file_bytes:
                raise ArchiveError(f"{p} is {m.size} bytes, limit {limits.max_file_bytes}")
            total += m.size
    if total > limits.max_total_bytes:
        raise ArchiveError(f"archive expands to {total} bytes, limit {limits.max_total_bytes}")
    if size > 0 and total / size > limits.max_ratio and total > 10 * 1024 * 1024:
        raise ArchiveError("archive compression ratio too high")

    # strip a single wrapping folder
    files = [(p, m) for p, m in paths if not m.is_dir]
    tops = {p.parts[0] for p, _ in files}
    if len(tops) == 1 and all(len(p.parts) > 1 for p, _ in files):
        only = next(iter(tops))
        if only not in ALLOWED_TOP:
            files = [(PurePosixPath(*p.parts[1:]), m) for p, m in files]

    warnings: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    dest_real = dest.resolve()
    written = []
    seen = set()
    for p, m in files:
        if p.parts[0] not in ALLOWED_TOP:
            warnings.append(f"ignored {p} (not part of the submission format)")
            continue
        if p.parts[0] == "report.md" and len(p.parts) > 1:
            raise ArchiveError("report.md must be a file")
        key = p.as_posix().lower()
        if key in seen:
            raise ArchiveError(f"duplicate member {p}")
        seen.add(key)
        target = (dest_real / p).resolve()
        if dest_real not in target.parents:
            raise ArchiveError(f"member escapes destination: {p}")
        data = m.read()
        if len(data) != m.size or len(data) > limits.max_file_bytes:
            raise ArchiveError(f"size mismatch for {p}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as fh:
            fh.write(data)
        os.chmod(target, 0o644)
        written.append(p.as_posix())

    present = {Path(w).parts[0] for w in written}
    missing = [r for r in REQUIRED if r not in present]
    if missing:
        raise ArchiveError("submission is missing: " + ", ".join(missing))
    for opt in ("tests", "assertions"):
        if opt not in present:
            warnings.append(f"no {opt}/ directory in the submission")
    return {"files": sorted(written), "warnings": warnings, "bytes": total}


def pack_dir(src: str | os.PathLike, out: str | os.PathLike) -> Path:
    """Create a tar.gz of a submission directory (used by tests and tools)."""
    src = Path(src)
    out = Path(out)
    with tarfile.open(out, "w:gz") as tf:
        for p in sorted(src.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and "sim_build" not in str(p):
                tf.add(p, arcname=p.relative_to(src).as_posix())
    return out

