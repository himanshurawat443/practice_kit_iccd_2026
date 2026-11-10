"""Participant-facing report (JSON + Markdown).

Only what participants may see goes here: stage status and messages, names
of failed hidden checks, file:line of surviving mutants, PPA per stage.
"""

from __future__ import annotations

from pathlib import Path

from .util import write_json

SCHEMA = "pgflow-report/1"

STAGE_ORDER = ["build", "public", "hidden", "mutation", "ppa"]
STAGE_TITLES = {"build": "1. Build", "public": "2. Public checks", "hidden": "3. Hidden checks",
                "mutation": "4. Mutation", "ppa": "5. Synthesis and place and route"}


def _public_stage(name: str, st: dict) -> dict:
    det = st.get("details") or {}
    out = {"status": st.get("status"), "seconds": st.get("seconds"), "message": st.get("message")}
    if name == "build":
        for k in ("problems", "port_diff", "expected_filelist", "submitted_filelist",
                  "log_tail", "ignored_rtl_files", "verilator_warnings"):
            if k in det:
                out[k] = det[k]
    elif name == "public":
        pt = det.get("public_tb")
        if pt:
            out["public_testbench"] = [{"test": t["name"], "passed": t["passed"]}
                                       for t in pt.get("tests", [])]
            out["public_testbench_assertion_fired"] = pt.get("assertion_fired")
        ev = det.get("participant_tests")
        if ev:
            out["your_tests"] = [{"test": t["name"], "passed": t["passed"],
                                  "message": t.get("message", "")[:300]}
                                 for t in ev.get("tests", [])]
            out["evidence_seconds"] = det.get("evidence_seconds")
        if "log_tail" in det:
            out["log_tail"] = det["log_tail"]
    elif name == "hidden":
        out["passed"] = det.get("passed")
        out["total"] = det.get("total")
        out["failed_checks"] = det.get("failed_names", [])
    elif name == "mutation":
        muts = det.get("mutants", [])
        counted = [m for m in muts if m.get("counted", True)]
        out["mutants"] = len(counted)
        out["killed"] = det.get("killed")
        out["surviving"] = sorted({f"{m['file']}:{m['line']}" for m in counted
                                   if not m.get("killed")},
                                  key=lambda s: (s.split(":")[0], int(s.split(":")[1])))
        out["seed"] = det.get("seed")
    elif name == "ppa":
        out["clock_period_ns"] = det.get("clock_period_ns")
        out["stages"] = det.get("stages")
        out["baseline_stages"] = (det.get("baseline") or {}).get("stages")
        out["final"] = det.get("final")
        out["baseline_final"] = (det.get("baseline") or {}).get("final")
        if det.get("orfs"):
            out["step_seconds"] = det["orfs"].get("step_seconds")
            out["failed_step"] = det["orfs"].get("failed_step")
        if "score" in det:
            out["P_breakdown"] = det["score"]
    return out


def build_report(ctx: dict) -> dict:
    stages = ctx["stages"]
    rep = {
        "schema": SCHEMA,
        "design": ctx["design_id"],
        "mode": ctx["mode"],
        "submission_sha256": ctx["sub_hash"],
        "flow_version": ctx["flow_version"],
        "status": ctx["status"],
        "rejected_at": ctx.get("rejected_at"),
        "reason": ctx.get("reason"),
        "score": ctx.get("score"),
        "C": ctx.get("C"),
        "V": ctx.get("V"),
        "P": ctx.get("P"),
        "stages": {n: _public_stage(n, stages[n]) for n in STAGE_ORDER if n in stages},
        "trajectory_included": bool(ctx.get("trajectory_included")),
        "seconds": ctx.get("seconds"),
        "warnings": ctx.get("warnings", []),
        "tools": ctx.get("tools", {}),
    }
    if ctx["mode"] == "public":
        rep["projected_score_if_all_hidden_checks_pass"] = ctx.get("projected_score")
    return rep


def _fmt(v, nd=3):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_md(rep: dict) -> str:
    L = []
    L.append(f"# Flow report: {rep['design']}")
    L.append("")
    L.append(f"Submission `{rep['submission_sha256'][:16]}`, mode `{rep['mode']}`, "
             f"flow {rep['flow_version']}.")
    L.append("")
    if rep["status"] == "rejected":
        L.append(f"**Rejected at stage `{rep['rejected_at']}`:** {rep['reason']}")
        L.append("")
    elif rep["status"] == "error":
        L.append(f"**Flow error:** {rep['reason']}")
        L.append("")
    L.append("| Score | C | V | P |")
    L.append("|---|---|---|---|")
    L.append(f"| {_fmt(rep['score'], 2)} | {_fmt(rep['C'])} | {_fmt(rep['V'])} | {_fmt(rep['P'])} |")
    L.append("")
    if rep["mode"] == "public":
        L.append("Public mode does not run the hidden checks, so C is unknown. "
                 f"Projected score if every hidden check passes (C = 1): "
                 f"**{_fmt(rep.get('projected_score_if_all_hidden_checks_pass'), 2)}**.")
        L.append("")
    L.append("Score = 100 x C x (0.5 V + 0.5 P)."
             + (" A trajectory is included." if rep.get("trajectory_included") else ""))
    L.append("")
    L.append("## Stages")
    L.append("")
    L.append("| Stage | Status | Seconds | Message |")
    L.append("|---|---|---|---|")
    for n in STAGE_ORDER:
        st = rep["stages"].get(n)
        if not st:
            continue
        msg = (st.get("message") or "").replace("|", "/")
        L.append(f"| {STAGE_TITLES[n]} | {st['status']} | {_fmt(st.get('seconds'), 1)} | {msg} |")
    L.append("")
    b = rep["stages"].get("build", {})
    if b.get("problems") or b.get("port_diff") or b.get("log_tail"):
        L.append("### Build problems")
        L.append("")
        for p in b.get("problems", []):
            L.append(f"- {p}")
        if b.get("port_diff"):
            for k, v in b["port_diff"].items():
                L.append(f"- port `{k}`: released {v['released']}, submitted {v['submitted']}")
        if b.get("log_tail"):
            L.append("")
            L.append("```")
            L.append(b["log_tail"])
            L.append("```")
        L.append("")
    pub = rep["stages"].get("public", {})
    if pub.get("public_testbench") or pub.get("your_tests"):
        L.append("### Public checks")
        L.append("")
        L.append("| Suite | Test | Result |")
        L.append("|---|---|---|")
        for t in pub.get("public_testbench", []):
            L.append(f"| public | {t['test']} | {'pass' if t['passed'] else 'FAIL'} |")
        for t in pub.get("your_tests", []):
            L.append(f"| yours | {t['test']} | {'pass' if t['passed'] else 'FAIL'} |")
        L.append("")
    hid = rep["stages"].get("hidden")
    if hid:
        L.append("### Hidden checks")
        L.append("")
        L.append(f"{hid.get('passed')} of {hid.get('total')} passed.")
        if hid.get("failed_checks"):
            L.append("")
            L.append("Failed: " + ", ".join(f"`{n}`" for n in hid["failed_checks"]))
        L.append("")
    mut = rep["stages"].get("mutation")
    if mut and mut.get("mutants") is not None:
        L.append("### Mutation")
        L.append("")
        L.append(f"{mut.get('killed')} of {mut.get('mutants')} mutants killed.")
        if mut.get("surviving"):
            L.append("")
            L.append("Surviving mutants (file:line): " + ", ".join(f"`{s}`" for s in mut["surviving"]))
        L.append("")
    ppa = rep["stages"].get("ppa")
    if ppa and ppa.get("stages"):
        L.append("### PPA per stage (sky130hd)")
        L.append("")
        L.append(f"Clock period {ppa.get('clock_period_ns')} ns.")
        L.append("")
        L.append("| Stage | Area (um^2) | Cells | Util | WNS (ns) | TNS (ns) | Power (mW) | Wirelength (um) |")
        L.append("|---|---|---|---|---|---|---|---|")
        for sname, m in ppa["stages"].items():
            pw = m.get("power_w")
            L.append(f"| {sname} | {_fmt(m.get('area_um2'), 1)} | {_fmt(m.get('cell_count'), 0)} | "
                     f"{_fmt(m.get('utilization'), 3)} | {_fmt(m.get('wns_ns'))} | "
                     f"{_fmt(m.get('tns_ns'))} | {_fmt(pw * 1e3 if isinstance(pw, float) else None)} | "
                     f"{_fmt(m.get('wirelength_um'), 0)} |")
        L.append("")
        bf, sf = ppa.get("baseline_final") or {}, ppa.get("final") or {}
        if bf and sf:
            L.append("| Final | Baseline | Submission |")
            L.append("|---|---|---|")
            L.append(f"| Area (um^2) | {_fmt(bf.get('area_um2'), 1)} | {_fmt(sf.get('area_um2'), 1)} |")
            L.append(f"| WNS (ns) | {_fmt(bf.get('wns_ns'))} | {_fmt(sf.get('wns_ns'))} |")
            L.append(f"| Power (W) | {_fmt(bf.get('power_w'), 6)} | {_fmt(sf.get('power_w'), 6)} |")
            L.append("")
        pb = ppa.get("P_breakdown")
        if pb:
            L.append(f"P = {pb['P']} (area {pb['a']:+.4f}, timing {pb['t']:+.4f}, power {pb['p']:+.4f}"
                     + ("; capped at 0.3 because timing is not met" if pb.get("capped") else "")
                     + ").")
            L.append("")
    if rep.get("warnings"):
        L.append("### Warnings")
        L.append("")
        for w in rep["warnings"]:
            L.append(f"- {w}")
        L.append("")
    L.append("### Wall-clock seconds")
    L.append("")
    L.append(" | ".join(f"{k} {v}" for k, v in (rep.get("seconds") or {}).items()))
    L.append("")
    return "\n".join(L)


def write_reports(out: Path, rep: dict) -> None:
    write_json(out / "report.json", rep)
    (out / "report.md").write_text(render_md(rep))
