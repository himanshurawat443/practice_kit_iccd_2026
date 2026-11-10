"""Orchestrates the stages of one Flow run."""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sys
import threading
import traceback
from pathlib import Path

from . import __version__
from .archive import ArchiveError, safe_extract
from .checks import run_build
from .design import load_design
from .mutation import run_mutation
from .ppa import cache_dir, run_ppa
from .public import run_public
from .report import build_report, write_reports
from .score import ppa_score, total_score
from .util import StageResult, Stopwatch, cpu_jobs, sha256_file, tool_version, write_json


def server_available() -> bool:
    return importlib.util.find_spec("pgflow_server") is not None


def tool_versions() -> dict:
    import cocotb
    return {
        "pgflow": __version__,
        "verilator": tool_version(["verilator", "--version"]),
        "yosys": tool_version(["yosys", "-V"]),
        "openroad": tool_version(["openroad", "-version"]),
        "cocotb": cocotb.__version__,
        "python": sys.version.split()[0],
    }


def default_cache() -> Path:
    return Path(os.environ.get("PGFLOW_CACHE", Path.home() / ".cache" / "pgflow"))


def run(design_dir: str, archive: str, out: str, mode: str = "public", jobs: int | None = None,
        cache: str | None = None, keep_work: bool = False, skip: set[str] | None = None,
        timeouts: dict | None = None) -> dict:
    skip = skip or set()
    T = {"build": 300, "public_build": 600, "public_tests": 600, "hidden": 1200,
         "mutation_build": 600, "ppa": 2400}
    T.update(timeouts or {})
    total = Stopwatch()
    jobs = cpu_jobs(jobs)
    out_p = Path(out).resolve()
    out_p.mkdir(parents=True, exist_ok=True)
    logs = out_p / "logs"
    logs.mkdir(exist_ok=True)
    sandbox = (mode == "server" and bool(os.environ.get("PGFLOW_SANDBOX_USER"))
               and os.geteuid() == 0)
    if sandbox:
        # the work tree must be reachable by the sandbox account even when
        # OUTDIR sits below a private directory
        import tempfile
        work = Path(tempfile.mkdtemp(prefix="pgflow-", dir=os.environ.get("PGFLOW_WORK_ROOT", "/tmp")))
        os.chmod(work, 0o755)
    else:
        work = out_p / "work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir()

    if mode == "server" and not server_available():
        raise SystemExit("server mode is not available in this image")

    design = load_design(design_dir)
    cache_root = Path(cache).resolve() if cache else default_cache()
    dcache = cache_dir(design, cache_root)
    sub_hash = sha256_file(archive)

    ctx: dict = {
        "design_id": design.id, "mode": mode, "sub_hash": sub_hash,
        "flow_version": __version__, "status": "scored", "stages": {},
        "warnings": [], "seconds": {}, "tools": tool_versions(),
        "design": design, "archive": str(Path(archive).resolve()), "out": out_p,
        "work": work, "logs": logs, "cache": dcache, "jobs": jobs,
    }
    stages: dict[str, dict] = ctx["stages"]
    if sandbox and design.hidden.is_dir() and design.hidden.stat().st_mode & 0o005:
        (out_p / "internal").mkdir(exist_ok=True)
        (out_p / "internal" / "security_warning.txt").write_text(
            "hidden/ is readable by other users: sandboxed participant code can read it. "
            "Mount it with o-rwx (see INTERNAL.md).\n")

    def record(st: StageResult) -> None:
        stages[st.name] = st.to_dict()
        ctx["seconds"][st.name] = st.seconds
        write_json(out_p / "stages" / f"{st.name}.json", st.to_dict())

    def finish() -> dict:
        ctx["seconds"]["total"] = total.elapsed()
        rep = build_report(ctx)
        write_reports(out_p, rep)
        ctx["report"] = rep
        if mode == "server":
            try:
                from pgflow_server import datacap
                datacap.capture(ctx)
            except Exception:  # noqa: BLE001
                (out_p / "internal").mkdir(exist_ok=True)
                (out_p / "internal" / "datacap_error.txt").write_text(traceback.format_exc())
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
        elif work != out_p / "work":
            shutil.rmtree(out_p / "work", ignore_errors=True)
            shutil.move(str(work), str(out_p / "work"))
        return rep

    def reject(stage: str, reason: str) -> dict:
        ctx["status"] = "rejected"
        ctx["rejected_at"] = stage
        ctx["reason"] = reason
        ctx["score"] = 0.0
        return finish()

    # ---------------------------------------------------------- unpack
    sub = work / "submission"
    sw = Stopwatch()
    try:
        info = safe_extract(archive, sub)
    except ArchiveError as exc:
        st = StageResult("build", "reject", sw.elapsed(), f"archive rejected: {exc}")
        record(st)
        return reject("build", st.message)
    ctx["warnings"] += info["warnings"]
    ctx["unpack"] = info
    ctx["trajectory_included"] = any(f.startswith("trajectory/") for f in info["files"])

    # ---------------------------------------------------------- 1 build
    try:
        st = run_build(design, sub, work / "build", logs, dcache, T["build"])
    except Exception as exc:  # noqa: BLE001
        st = StageResult("build", "error", sw.elapsed(), f"internal error: {exc}")
    st.seconds = round(st.seconds + 0.0, 2)
    record(st)
    if st.status != "pass":
        if st.status == "error":
            ctx["status"], ctx["reason"] = "error", st.message
            return finish()
        return reject("build", st.message)

    # PPA runs in the background from here on (it needs only valid RTL)
    ppa_cancel = threading.Event()
    ppa_box: dict = {}

    def ppa_thread():
        try:
            ppa_box["st"] = run_ppa(design, sub, work / "ppa", logs, dcache, jobs,
                                    T["ppa"], cancel=ppa_cancel)
        except Exception as exc:  # noqa: BLE001
            ppa_box["st"] = StageResult("ppa", "error", 0, f"internal error: {exc}")
            ppa_box["tb"] = traceback.format_exc()

    th = None
    if "ppa" not in skip:
        th = threading.Thread(target=ppa_thread, daemon=True)
        th.start()

    def abort_ppa():
        if th is not None:
            ppa_cancel.set()
            th.join()

    # ---------------------------------------------------------- 2 public
    try:
        st = run_public(design, sub, work / "public", logs, sub_hash, jobs, sandbox,
                        T["public_build"], T["public_tests"])
    except Exception as exc:  # noqa: BLE001
        st = StageResult("public", "error", 0, f"internal error: {exc}")
        (logs / "public_internal_error.txt").write_text(traceback.format_exc())
    record(st)
    if st.status != "pass":
        abort_ppa()
        if st.status == "error":
            ctx["status"], ctx["reason"] = "error", st.message
            return finish()
        return reject("public", st.message)
    evidence_seconds = float(st.details.get("evidence_seconds") or 0.0)

    # ------------------------------------------- 3 hidden + 4 mutation
    # independent of each other: the hidden checks run in a thread next to
    # the mutation stage (and PPA, already running)
    C = None
    hid_box: dict = {}

    def hidden_thread():
        from pgflow_server.hidden import run_hidden
        try:
            hid_box["st"] = run_hidden(design, sub, work / "hidden", out_p / "internal", dcache,
                                       max(1, jobs // 2), sandbox=False, timeout=T["hidden"])
        except Exception as exc:  # noqa: BLE001
            hid_box["st"] = StageResult("hidden", "error", 0, f"internal error: {exc}")
            (out_p / "internal").mkdir(exist_ok=True)
            (out_p / "internal" / "hidden_error.txt").write_text(traceback.format_exc())

    hth = None
    if mode == "server" and "hidden" not in skip:
        hth = threading.Thread(target=hidden_thread, daemon=True)
        hth.start()

    V = 0.0
    if "mutation" not in skip:
        try:
            st = run_mutation(design, sub, work / "mutation", logs, sub_hash, jobs,
                              evidence_seconds, sandbox, T["mutation_build"])
        except Exception as exc:  # noqa: BLE001
            st = StageResult("mutation", "error", 0, f"internal error: {exc}")
            (logs / "mutation_internal_error.txt").write_text(traceback.format_exc())
        record(st)
        V = float(st.details.get("V", 0.0) or 0.0)

    if hth is not None:
        hth.join()
        st = hid_box["st"]
        record(st)
        if st.status == "error":
            abort_ppa()
            ctx["status"], ctx["reason"] = "error", st.message
            return finish()
        C = st.details.get("C", 0.0)

    # ---------------------------------------------------------- 5 ppa
    P = 0.0
    if th is not None:
        th.join()
        st = ppa_box["st"]
        if ppa_box.get("tb"):
            (logs / "ppa_internal_error.txt").write_text(ppa_box["tb"])
        det = st.details
        if st.status == "pass":
            ps = ppa_score(det["baseline"]["final"], det["final"], design.clock_period_ns)
            det["score"] = ps
            P = ps["P"]
        elif st.status == "error":
            ctx["warnings"].append("PPA could not be scored: " + st.message)
        record(st)
        ctx["ppa_work_home"] = det.get("work_home")

    ctx["V"], ctx["P"] = round(V, 4), round(P, 4)
    if C is not None:
        ctx["C"] = round(C, 4)
        ctx["score"] = total_score(C, V, P)
    else:
        ctx["C"] = None
        ctx["score"] = None
        ctx["projected_score"] = total_score(1.0, V, P)
    return finish()
