"""Stage 2: public checks.

Builds the submission with the participant's assertions bound (Verilator
--assert), runs the public testbench (informational) and the participant's
own tests (must all pass, with no assertion failure, or the submission is
rejected).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import sim
from .design import Design
from .util import StageResult, Stopwatch, sandbox_dir


def assertion_files(sub: Path) -> list[Path]:
    d = sub / "assertions"
    if not d.is_dir():
        return []
    return sorted(p for p in d.rglob("*") if p.suffix in (".sv", ".svh", ".v") and p.is_file()
                  and p.suffix != ".svh")


def evidence_seed(sub_hash: str) -> int:
    return int(sub_hash[:8], 16) & 0x7FFFFFFF


def sim_plusargs(seed: int) -> list[str]:
    # Registers and inputs start at zero (rand+reset+0). Random initial values
    # let the first evaluation at time 0 see a clock edge with a random reset
    # value, which fires correct concurrent assertions depending on the seed.
    return [f"+verilator+seed+{seed % 1000000 + 1}", "+verilator+rand+reset+0"]


def build_with_assertions(design: Design, rtl_root: Path, sub: Path, build_dir: Path,
                          log: Path, timeout: float, jobs: int, sandbox: bool,
                          rtl_files: list[str] | None = None):
    sources = [str(rtl_root / f) for f in (rtl_files or design.rtl_files)]
    sources += [str(p) for p in assertion_files(sub)]
    args = list(design.build_args) + ["--assert"]
    return sim.build(sources=sources, top=design.top, build_dir=build_dir, build_args=args,
                     includes=[str(rtl_root / "rtl")], timescale=design.timescale,
                     timeout=timeout, log=log, jobs=jobs, sandbox=sandbox)


def run_public(design: Design, sub: Path, work: Path, logs: Path, sub_hash: str,
               jobs: int, sandbox: bool = False, timeout_build: float = 600,
               timeout_tests: float = 600) -> StageResult:
    sw = Stopwatch()
    st = StageResult("public")
    det: dict = {}
    st.details = det
    seed = evidence_seed(sub_hash)
    det["seed"] = seed
    sandbox_dir(work, sandbox)

    build_dir = work / "build_evidence"
    b = build_with_assertions(design, sub, sub, build_dir, logs / "public_build.log",
                              timeout_build, jobs, sandbox)
    det["build_seconds"] = round(b.seconds, 2)
    det["assertion_files"] = [p.relative_to(sub).as_posix() for p in assertion_files(sub)]
    if not b.ok:
        st.status = "reject"
        st.message = ("the design with your assertions bound did not compile in Verilator "
                      "(see logs/public_build.log)")
        det["log_tail"] = b.tail(30)
        st.seconds = sw.elapsed()
        return st

    # public testbench (informational: the change request may alter behavior)
    pub_mods = sim.discover_test_modules(design.public_tb)
    if pub_mods:
        # copy so that no participant file shadows the public helpers
        pub_dir = work / "public_tb"
        if pub_dir.exists():
            shutil.rmtree(pub_dir)
        shutil.copytree(design.public_tb, pub_dir,
                        ignore=shutil.ignore_patterns("sim_build*", "__pycache__", "*.xml"))
        sandbox_dir(work, sandbox)
        r = sim.run_tests(modules=pub_mods, top=design.top, build_dir=build_dir,
                          test_dir=work / "run_public", pythonpath=[str(pub_dir)],
                          timeout=timeout_tests, log=logs / "public_tb.log",
                          plusargs=sim_plusargs(seed), seed=seed, timescale=design.timescale,
                          sandbox=sandbox)
        det["public_tb"] = r.to_dict()
        det["public_tb_passed"] = r.all_passed
    else:
        det["public_tb"] = None

    # participant tests: must pass on the submission
    tdir = sub / "tests"
    mods = sim.discover_test_modules(tdir) if tdir.is_dir() else []
    det["participant_modules"] = mods
    if not mods:
        st.status = "pass"
        st.message = ("no participant tests found (tests/test_*.py); the mutation score "
                      "will be zero")
        det["evidence_seconds"] = 0.0
        st.seconds = sw.elapsed()
        return st
    r = sim.run_tests(modules=mods, top=design.top, build_dir=build_dir,
                      test_dir=work / "run_evidence", pythonpath=[str(tdir)],
                      timeout=timeout_tests, log=logs / "evidence.log",
                      plusargs=sim_plusargs(seed), seed=seed, timescale=design.timescale,
                      sandbox=sandbox)
    det["participant_tests"] = r.to_dict()
    det["evidence_seconds"] = round(r.cmd.seconds, 2)
    if not r.all_passed:
        st.status = "reject"
        why = []
        if r.cmd.timed_out:
            why.append(f"timed out after {timeout_tests:.0f} s")
        if r.assertion_fired:
            why.append("an assertion failed")
        failed = r.failed_names()
        if failed:
            why.append("failing tests: " + ", ".join(failed))
        if not r.tests:
            why.append("no test results (did the simulation start?)")
        st.message = "your evidence fails on your own submission: " + "; ".join(why)
        st.seconds = sw.elapsed()
        return st
    st.status = "pass"
    st.message = f"{len(r.tests)} participant tests pass with assertions enabled"
    st.seconds = sw.elapsed()
    return st
