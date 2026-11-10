"""Stage 5: synthesis and place-and-route with OpenROAD-flow-scripts on sky130hd.

The flow script is fixed: Yosys (slang frontend) synthesis, then OpenROAD
floorplan, placement, clock tree synthesis, global and detailed routing and
final reporting, with the released clock constraint. Metrics are read from the
per-stage JSON files ORFS writes.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
from pathlib import Path

from .design import Design
from .util import StageResult, Stopwatch, read_json, run_cmd, write_json

ORFS = Path(os.environ.get("PGFLOW_ORFS", "/OpenROAD-flow-scripts"))
PLATFORM = "sky130hd"

MAKE_STEPS = ["synth", "floorplan", "place", "cts", "route", "finish"]

# stage name, ORFS metrics file, metric key prefix
METRIC_FILES = [
    ("synth", "1_synth.json", "synth"),
    ("floorplan", "2_1_floorplan.json", "floorplan"),
    ("place", "3_5_place_dp.json", "detailedplace"),
    ("cts", "4_1_cts.json", "cts"),
    ("global_route", "5_1_grt.json", "globalroute"),
    ("route", "5_2_route.json", "detailedroute"),
    ("final", "6_report.json", "finish"),
]

FIXED_VARS = {
    "PLATFORM": PLATFORM,
    "SYNTH_HDL_FRONTEND": "slang",
    "LEC_CHECK": 0,
    "SYNTH_HIERARCHICAL": 0,
}


def orfs_dirs(work_home: Path, nickname: str) -> dict[str, Path]:
    return {k: work_home / k / PLATFORM / nickname / "base"
            for k in ("logs", "reports", "results", "objects")}


def write_config(path: Path, *, top: str, nickname: str, verilog: list[Path] | None,
                 sdc: Path, options: dict, netlist: Path | None = None) -> None:
    lines = [f"export DESIGN_NAME = {top}", f"export DESIGN_NICKNAME = {nickname}"]
    for k, v in FIXED_VARS.items():
        lines.append(f"export {k} = {v}")
    if netlist is not None:
        lines.append(f"export SYNTH_NETLIST_FILES = {netlist}")
        lines.append(f"export VERILOG_FILES = {netlist}")
    else:
        lines.append("export VERILOG_FILES = " + " ".join(str(v) for v in verilog or []))
    lines.append(f"export SDC_FILE = {sdc}")
    for k, v in options.items():
        if k in FIXED_VARS or k in ("DESIGN_NAME", "VERILOG_FILES", "SDC_FILE"):
            continue
        lines.append(f"export {k} = {v}")
    path.write_text("\n".join(lines) + "\n")


def run_orfs(*, top: str, nickname: str, work_home: Path, verilog: list[Path] | None,
             sdc: Path, options: dict, jobs: int, timeout: float, log_dir: Path,
             netlist: Path | None = None, cancel=None) -> dict:
    """Run the fixed ORFS flow. Returns {'ok', 'failed_step', 'step_seconds', 'dirs'}."""
    work_home.mkdir(parents=True, exist_ok=True)
    cfg = work_home / "config.mk"
    write_config(cfg, top=top, nickname=nickname, verilog=verilog, sdc=sdc, options=options,
                 netlist=netlist)
    step_seconds: dict[str, float] = {}
    sw = Stopwatch()
    failed = None
    for step in MAKE_STEPS:
        left = timeout - sw.elapsed()
        if left <= 0:
            failed = step
            step_seconds[step] = 0.0
            break
        # 'finish' without the GDS merge (KLayout): fill + final report only
        target = "do-finish" if step == "finish" else step
        cmd = ["bash", "-c",
               f"source {ORFS}/env.sh >/dev/null 2>&1; exec make --no-print-directory "
               f"-C {ORFS}/flow DESIGN_CONFIG={cfg} WORK_HOME={work_home} "
               f"NUM_CORES={jobs} {target}"]
        r = run_cmd(cmd, cwd=work_home, timeout=left, log=log_dir / f"ppa_{nickname}_{step}.log",
                    cancel=cancel)
        step_seconds[step] = round(r.seconds, 2)
        if not r.ok:
            failed = step
            break
    return {"ok": failed is None, "failed_step": failed, "step_seconds": step_seconds,
            "dirs": {k: str(v) for k, v in orfs_dirs(work_home, nickname).items()},
            "config": str(cfg)}


def _pick(d: dict, prefix: str, *suffixes: str):
    for s in suffixes:
        v = d.get(f"{prefix}__{s}")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return v
    return None


def read_metrics(work_home: Path, nickname: str) -> dict:
    """Per-stage summary metrics from ORFS JSON logs."""
    logs = orfs_dirs(work_home, nickname)["logs"]
    stages: dict[str, dict] = {}
    raw: dict[str, dict] = {}
    for stage, fname, pre in METRIC_FILES:
        d = read_json(logs / fname)
        if not d:
            continue
        raw[stage] = d
        stages[stage] = {
            "area_um2": _pick(d, pre, "design__instance__area__stdcell", "design__instance__area"),
            "cell_count": _pick(d, pre, "design__instance__count__stdcell", "design__instance__count"),
            "utilization": _pick(d, pre, "design__instance__utilization"),
            "wns_ns": _pick(d, pre, "timing__setup__ws"),
            "tns_ns": _pick(d, pre, "timing__setup__tns"),
            "hold_wns_ns": _pick(d, pre, "timing__hold__ws"),
            "power_w": _pick(d, pre, "power__total"),
            "wirelength_um": _pick(d, pre, "route__wirelength", "global_route__wirelength",
                                   "dpl__route__wirelength__estimated", "route__wirelength__estimated"),
            "drc_errors": _pick(d, pre, "route__drc_errors"),
            "core_area_um2": _pick(d, pre, "design__core__area"),
            "die_area_um2": _pick(d, pre, "design__die__area"),
        }
        stages[stage] = {k: v for k, v in stages[stage].items() if v is not None}
    # global-route overflow from the GRT log
    grt = logs / "5_1_grt.log"
    if grt.is_file():
        txt = grt.read_text(errors="replace")
        m = re.findall(r"^Total\s+(\d+)\s+(\d+)\s+([\d.]+)%\s+(\d+)\s*/\s*(\d+)\s*/\s*(\d+)", txt, re.M)
        if m:
            res, dem, use, oh, ov, ot = m[-1]
            stages.setdefault("global_route", {}).update({
                "grt_usage_pct": float(use), "grt_overflow_h": int(oh),
                "grt_overflow_v": int(ov), "grt_overflow_total": int(ot)})
    return {"stages": stages, "raw": raw}


def final_summary(metrics: dict) -> dict | None:
    f = metrics.get("stages", {}).get("final")
    if not f or f.get("area_um2") is None:
        return None
    return {"area_um2": f.get("area_um2"), "wns_ns": f.get("wns_ns"),
            "tns_ns": f.get("tns_ns"), "power_w": f.get("power_w")}


# ---------------------------------------------------------------------- cache

def cache_dir(design: Design, cache_root: Path) -> Path:
    from .util import sha256_tree
    h = sha256_tree(design.root, exclude=("hidden", "worked-example", "__pycache__", "tb"))
    d = cache_root / f"{design.id}-{h[:12]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def baseline_ppa(design: Design, cache: Path, jobs: int, timeout: float, log_dir: Path,
                 keep_work: bool = False) -> dict:
    """Run the baseline once per design (cached, file-locked)."""
    out = cache / "baseline_ppa.json"
    lock = open(cache / "baseline_ppa.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        cached = read_json(out)
        if cached and cached.get("ok"):
            return cached
        wh = cache / "baseline_orfs"
        if wh.exists():
            shutil.rmtree(wh)
        res = run_orfs(top=design.top, nickname="baseline", work_home=wh,
                       verilog=[design.root / f for f in design.rtl_files],
                       sdc=design.constraint, options=design.ppa_options, jobs=jobs,
                       timeout=timeout, log_dir=log_dir)
        res["metrics"] = read_metrics(wh, "baseline")["stages"]
        res["final"] = final_summary({"stages": res["metrics"]})
        res["clock_period_ns"] = design.clock_period_ns
        write_json(out, res)
        if not keep_work and res["ok"]:
            shutil.rmtree(wh / "objects", ignore_errors=True)
        return res
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def run_ppa(design: Design, sub: Path, work: Path, logs: Path, cache: Path, jobs: int,
            timeout: float = 1800, cancel=None) -> StageResult:
    sw = Stopwatch()
    st = StageResult("ppa")
    det: dict = {}
    st.details = det
    import concurrent.futures as cf
    wh = work / "orfs"
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        fb = pool.submit(baseline_ppa, design, cache, jobs, timeout, logs)
        res = run_orfs(top=design.top, nickname="submission", work_home=wh,
                       verilog=[sub / f for f in design.rtl_files], sdc=design.constraint,
                       options=design.ppa_options, jobs=jobs, timeout=timeout, log_dir=logs,
                       cancel=cancel)
        base = fb.result()
    metrics = read_metrics(wh, "submission")
    det["orfs"] = {k: v for k, v in res.items() if k != "dirs"}
    det["work_home"] = str(wh)
    det["stages"] = metrics["stages"]
    det["final"] = final_summary(metrics)
    det["baseline"] = {"ok": base.get("ok"), "final": base.get("final"),
                       "stages": base.get("metrics")}
    det["clock_period_ns"] = design.clock_period_ns
    if not res["ok"]:
        st.status = "fail"
        st.message = f"place and route failed at step '{res['failed_step']}'"
    elif not det["final"]:
        st.status = "fail"
        st.message = "no final metrics produced"
    elif not base.get("ok") or not base.get("final"):
        st.status = "error"
        st.message = "baseline place and route failed (organizer issue)"
    else:
        st.status = "pass"
        st.message = "place and route complete"
    st.seconds = sw.elapsed()
    return st
