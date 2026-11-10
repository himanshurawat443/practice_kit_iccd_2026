"""Stage 4: mutation analysis of the participant's evidence.

1. Collect mutation sites in the submitted RTL (pgflow.mutate).
2. Order them reproducibly: RNG seeded from the submission hash, round robin
   over the operator classes.
3. Walk the order and accept candidates until N_MUTANTS are accepted:
     * slang must accept the mutated source set (no semantic errors)
     * Yosys must NOT prove the mutated module equivalent to the submitted
       module (equiv_make + equiv_simple + equiv_induct, submodules flattened);
       proven-equivalent mutants are discarded; a check that times out keeps
       the mutant. Equivalence checking stops (remaining candidates accepted
       unchecked) after SELECTION_BUDGET seconds or when a whole batch of
       checks times out, so large modules do not stall the stage.
4. Build one Verilator model holding every accepted mutant behind a run-time
   selector (mutant schemata), check that the participant's tests still pass
   with no mutant selected, then run the tests once per mutant in parallel.
   A mutant is killed if any test fails, an assertion fires, the simulation
   crashes, or it exceeds the per-mutant timeout.
5. V = killed / accepted (accepted is N_MUTANTS unless the design has fewer
   valid sites).

Fallback: if the schemata build or its sanity run fails, each mutant is
built separately (slower, same verdicts).
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import re
import time
import shutil
from pathlib import Path

from . import mutate as M
from . import sim
from .design import Design
from .public import assertion_files, evidence_seed, sim_plusargs
from .util import StageResult, Stopwatch, run_cmd, sandbox_dir, write_json

N_MUTANTS = 40
EQUIV_TIMEOUT = 10.0
SELECTION_BUDGET = 90.0  # seconds of equivalence checking per submission


def _write_sources(root: Path, sources: dict[str, bytes]) -> None:
    for rel, data in sources.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def equiv_script(top: str, gold: list[Path], gate: list[Path]) -> str:
    prep = (f"hierarchy -top {top}; proc; flatten; memory -nomap; memory_map; "
            "opt_clean; async2sync; dffunmap")
    return "\n".join([
        f"read_slang --top {top} " + " ".join(map(str, gold)),
        prep, f"rename {top} gold", "design -stash gold",
        f"read_slang --top {top} " + " ".join(map(str, gate)),
        prep, f"rename {top} gate", "design -stash gate",
        "design -copy-from gold -as gold gold",
        "design -copy-from gate -as gate gate",
        "equiv_make gold gate eq", "hierarchy -top eq",
        "equiv_simple -seq 5", "equiv_induct -seq 5", "equiv_status -assert", ""])


def check_equivalent(design: Design, gold_root: Path, files: list[str], m: M.Mutant,
                     sources: dict[str, bytes], work: Path) -> str:
    """Return 'equivalent', 'different' or 'unknown' (timeout/error)."""
    d = work / ("eq_" + hashlib.sha1(m.key.encode()).hexdigest()[:12])
    d.mkdir(parents=True, exist_ok=True)
    gate_src = M.standalone(sources, m)
    _write_sources(d, {m.file: gate_src[m.file]})
    gate = [(d / f) if f == m.file else (gold_root / f) for f in files]
    gold = [gold_root / f for f in files]
    # module-level check: equivalence of the mutated module (its own
    # submodules flattened, default parameters) implies design equivalence
    (d / "eq.ys").write_text(equiv_script(m.module, gold, gate))
    r = run_cmd(["yosys", "-q", "-s", str(d / "eq.ys")], cwd=d, timeout=EQUIV_TIMEOUT,
                log=d / "eq.log")
    if r.timed_out:
        verdict = "unknown"
    elif r.returncode == 0:
        verdict = "equivalent"
    else:
        txt = Path(r.log).read_text(errors="replace")
        verdict = "different" if "unproven $equiv" in txt else "unknown"
    shutil.rmtree(d, ignore_errors=True)
    return verdict


def select_mutants(design: Design, sub: Path, work: Path, seed: int, jobs: int,
                   n: int = N_MUTANTS) -> tuple[list[M.Mutant], dict, dict]:
    files = design.rtl_files
    sources = M.load_sources(sub, files)
    sites, modules = M.collect_sites(sources, [design.reset_port])
    live = M.instantiated_modules(sources, design.top)
    dead = len(sites)
    sites = [s for s in sites if s.module in live]
    dead -= len(sites)
    order = M.ordered_candidates(sites, seed)
    stats = {"sites": len(sites), "sites_in_unused_modules": dead,
             "modules": sorted(live), "considered": 0, "rejected_invalid": 0,
             "rejected_equivalent": 0, "equiv_unknown": 0}
    by_class: dict[str, int] = {}
    for s in sites:
        by_class[s.op_class] = by_class.get(s.op_class, 0) + 1
    stats["sites_by_class"] = by_class
    accepted: list[M.Mutant] = []
    eqdir = work / "equiv"
    eqdir.mkdir(parents=True, exist_ok=True)
    pos = 0
    batch = max(2, jobs * 2)
    t0 = time.monotonic()
    check_equiv = True
    stats["equiv_unchecked"] = 0
    with cf.ThreadPoolExecutor(max_workers=jobs) as pool:
        while len(accepted) < n and pos < len(order):
            if check_equiv and time.monotonic() - t0 > SELECTION_BUDGET:
                check_equiv = False  # budget spent: accept the rest unchecked
            chunk = []
            while len(chunk) < batch and pos < len(order):
                cand = order[pos]
                pos += 1
                stats["considered"] += 1
                if M.slang_errors(M.standalone(sources, cand)):
                    stats["rejected_invalid"] += 1
                    continue
                chunk.append(cand)
            futs = [pool.submit(check_equivalent, design, sub, files, c, sources, eqdir)
                    if check_equiv else None for c in chunk]
            unknown_in_batch = 0
            for c, f in zip(chunk, futs):  # accept in order: reproducible
                verdict = f.result() if f is not None else "unchecked"
                if verdict == "unchecked":
                    stats["equiv_unchecked"] += 1
                if verdict == "unknown":
                    unknown_in_batch += 1
                if verdict == "equivalent":
                    stats["rejected_equivalent"] += 1
                    continue
                if verdict == "unknown":
                    stats["equiv_unknown"] += 1
                if len(accepted) < n:
                    accepted.append(c)
            if check_equiv and chunk and unknown_in_batch == len(chunk):
                check_equiv = False  # every check timed out: design too large for it
    # candidates evaluated past the n-th acceptance are not counted
    for i, m in enumerate(accepted, 1):
        m.id = i
    return accepted, modules, stats


def _kill(r: sim.RunOutcome) -> tuple[bool, str]:
    if r.cmd.timed_out:
        return True, "timeout"
    if r.assertion_fired:
        return True, "assertion"
    if not r.tests:
        return True, "simulation error"
    failed = r.failed_names()
    if failed:
        return True, "test failed: " + ", ".join(failed[:3])
    if r.cmd.returncode != 0:
        return True, "simulation error"
    return False, ""


def run_mutation(design: Design, sub: Path, work: Path, logs: Path, sub_hash: str,
                 jobs: int, evidence_seconds: float, sandbox: bool = False,
                 timeout_build: float = 600, n: int = N_MUTANTS) -> StageResult:
    sw = Stopwatch()
    st = StageResult("mutation")
    det: dict = {}
    st.details = det
    tdir = sub / "tests"
    mods = sim.discover_test_modules(tdir) if tdir.is_dir() else []
    seed = M.seed_from_hash(sub_hash)
    det["seed"] = str(seed)
    mutants, modules, stats = select_mutants(design, sub, work, seed, jobs, n)
    det["selection"] = stats
    det["selection_seconds"] = sw.elapsed()
    det["n_mutants"] = len(mutants)
    sources = M.load_sources(sub, design.rtl_files)
    det["mutants"] = [m.to_dict(sources) for m in mutants]
    if not mutants:
        st.status, st.message = "error", "no mutation sites found in the design"
        det["V"] = 0.0
        st.seconds = sw.elapsed()
        return st
    if not mods:
        for d in det["mutants"]:
            d["killed"], d["reason"] = False, "no participant tests"
        det["killed"] = 0
        det["V"] = 0.0
        st.status, st.message = "pass", "no participant tests: no mutant can be killed"
        st.seconds = sw.elapsed()
        return st

    ev_seed = evidence_seed(sub_hash)
    per_mutant_timeout = min(300.0, max(30.0, 2.0 * evidence_seconds + 15.0))
    det["per_mutant_timeout"] = per_mutant_timeout

    # ---- schemata build
    mroot = work / "mut_src"
    sch = M.schemata(sources, mutants, modules)
    _write_sources(mroot, sch)
    asserts = [str(p) for p in assertion_files(sub)]
    bdir = sandbox_dir(work / "build_mut", sandbox)
    sandbox_dir(work / "mut_run", sandbox)
    sandbox_dir(work / "mut_logs", sandbox)
    sandbox_dir(work / "mut_sep", sandbox)
    attempts = 0
    schemata_ok = False
    active = list(mutants)
    while attempts < 3:
        attempts += 1
        b = sim.build(sources=[str(mroot / f) for f in design.rtl_files] + asserts,
                      top=design.top, build_dir=bdir,
                      build_args=list(design.build_args) + ["--assert"],
                      includes=[str(mroot / "rtl")], timescale=design.timescale,
                      timeout=timeout_build, log=logs / "mutation_build.log", jobs=jobs,
                      sandbox=sandbox)
        if b.ok:
            schemata_ok = True
            break
        # drop mutants on lines Verilator complains about, then retry
        txt = Path(b.log).read_text(errors="replace")
        bad_lines = set(re.findall(r"%Error[^:]*: [^:]*?(rtl/[\w./-]+):(\d+)", txt))
        if not bad_lines:
            break
        active = [m for m in active if (m.file, str(m.line)) not in bad_lines]
        sch = M.schemata(sources, active, modules)
        _write_sources(mroot, sch)
    det["schemata_build_seconds"] = round(sw.elapsed() - det["selection_seconds"], 2)

    if schemata_ok:
        sanity = sim.run_tests(modules=mods, top=design.top, build_dir=bdir,
                               test_dir=work / "mut_run" / "m0", pythonpath=[str(tdir)],
                               timeout=max(per_mutant_timeout, evidence_seconds * 3 + 30),
                               log=logs / "mutation_m0.log", plusargs=sim_plusargs(ev_seed)
                               + ["+PGMUT=0"], seed=ev_seed, timescale=design.timescale,
                               sandbox=sandbox)
        schemata_ok = sanity.all_passed
        det["schemata_sanity"] = sanity.all_passed
    det["method"] = "schemata" if schemata_ok else "separate-builds"

    results: dict[int, tuple[bool, str]] = {}
    dropped = {m.id for m in mutants} - {m.id for m in active}

    def run_one_schemata(m: M.Mutant):
        r = sim.run_tests(modules=mods, top=design.top, build_dir=bdir,
                          test_dir=work / "mut_run" / f"m{m.id}", pythonpath=[str(tdir)],
                          timeout=per_mutant_timeout, log=work / "mut_logs" / f"m{m.id}.log",
                          plusargs=sim_plusargs(ev_seed) + [f"+PGMUT={m.id}"], seed=ev_seed,
                          timescale=design.timescale, sandbox=sandbox)
        return m.id, _kill(r)

    def run_one_separate(m: M.Mutant):
        root = work / "mut_sep" / f"m{m.id}"
        _write_sources(root, M.standalone(sources, m))
        sandbox_dir(root, sandbox)
        bd = root / "build"
        b = sim.build(sources=[str(root / f) for f in design.rtl_files] + asserts,
                      top=design.top, build_dir=bd,
                      build_args=list(design.build_args) + ["--assert"],
                      includes=[str(root / "rtl")], timescale=design.timescale,
                      timeout=timeout_build, log=root / "build.log", jobs=1, sandbox=sandbox)
        if not b.ok:
            return m.id, (False, "mutant did not build (not counted)")
        r = sim.run_tests(modules=mods, top=design.top, build_dir=bd, test_dir=root / "run",
                          pythonpath=[str(tdir)], timeout=per_mutant_timeout,
                          log=root / "run.log", plusargs=sim_plusargs(ev_seed), seed=ev_seed,
                          timescale=design.timescale, sandbox=sandbox)
        res = _kill(r)
        shutil.rmtree(bd, ignore_errors=True)
        return m.id, res

    runner = run_one_schemata if schemata_ok else run_one_separate
    todo = [m for m in active] if schemata_ok else list(mutants)
    t_run = Stopwatch()
    with cf.ThreadPoolExecutor(max_workers=jobs) as pool:
        for mid, res in pool.map(runner, todo):
            results[mid] = res
    det["run_seconds"] = t_run.elapsed()

    counted = []
    for d in det["mutants"]:
        mid = d["id"]
        if mid in dropped or results.get(mid, (False, ""))[1].startswith("mutant did not build"):
            d["killed"], d["reason"], d["counted"] = False, "invalid mutant (not counted)", False
            continue
        killed, reason = results.get(mid, (False, "not run"))
        d["killed"], d["reason"], d["counted"] = killed, reason, True
        counted.append(d)
    killed = sum(1 for d in counted if d["killed"])
    det["counted"] = len(counted)
    det["killed"] = killed
    det["V"] = round(killed / len(counted), 4) if counted else 0.0
    st.status = "pass"
    st.message = f"{killed} of {len(counted)} mutants killed"
    st.seconds = sw.elapsed()
    write_json(work / "mutants.json", det["mutants"])
    return st
