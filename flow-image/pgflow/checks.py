"""Stage 1: build. Layout, filelist, ports and constraint checks; lint;
elaboration; synthesizability."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pyslang as ps

from .design import Design, read_filelist
from .util import StageResult, Stopwatch, run_cmd, read_json, write_json

# Run-time file access and other constructs that are not allowed in RTL
FORBIDDEN_CALLS = {
    "$readmemh", "$readmemb", "$fopen", "$fclose", "$fread", "$fscanf", "$fgets",
    "$fgetc", "$sscanf", "$system", "$writememh", "$writememb", "$fwrite",
    "$fdisplay", "$value$plusargs", "$test$plusargs", "$random", "$urandom",
    "$urandom_range", "$dist_uniform",
}
FORBIDDEN_KINDS = {
    "InitialBlock": "initial block",
    "FinalBlock": "final block",
    "DelayControl": "delay control (#)",
    "TimingControlStatement": "timing control statement",
    "ParallelBlockStatement": "fork/join",
    "WaitStatement": "wait statement",
    "WaitForkStatement": "wait fork",
    "ForceStatement": "force",
    "ReleaseStatement": "release",
    "ClassDeclaration": "class",
    "DPIImport": "DPI import",
    "DPIExport": "DPI export",
    "ProgramDeclaration": "program block",
}


def _kind(n) -> str:
    return str(n.kind).split(".")[-1]


def scan_constructs(files: dict[str, str]) -> list[str]:
    """Return 'file:line: message' for every forbidden construct."""
    problems: list[str] = []
    for rel, text in files.items():
        tree = ps.SyntaxTree.fromText(text, rel)
        for d in tree.diagnostics:
            if d.isError():
                problems.append(f"{rel}: syntax error ({d.code})")
                break

        def line_of(node) -> int:
            try:
                return text.count("\n", 0, node.sourceRange.start.offset) + 1
            except Exception:  # noqa: BLE001
                return 0

        def visit(n):
            if n is None or isinstance(n, ps.Token):
                return
            k = _kind(n)
            if k in FORBIDDEN_KINDS:
                # timing control @(...) inside always blocks is fine; only
                # flag delays and explicit waits
                if k == "TimingControlStatement":
                    tc = str(getattr(n, "timingControl", "")).strip()
                    if tc.startswith("#"):
                        problems.append(f"{rel}:{line_of(n)}: delay (#) not allowed in RTL")
                elif k == "DelayControl":
                    problems.append(f"{rel}:{line_of(n)}: delay (#) not allowed in RTL")
                else:
                    problems.append(f"{rel}:{line_of(n)}: {FORBIDDEN_KINDS[k]} not allowed in RTL")
            if k in ("InvocationExpression", "SystemTaskInvocation", "ExpressionStatement"):
                head = str(getattr(n, "left", "") or "").strip()
                if head in FORBIDDEN_CALLS:
                    problems.append(f"{rel}:{line_of(n)}: {head} not allowed in RTL")
            for c in n:
                visit(c)
        visit(tree.root)
        for m in re.finditer(r"`include\s+\"([^\"]+)\"", text):
            inc = m.group(1)
            if inc.startswith("/") or ".." in Path(inc).parts:
                ln = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{ln}: include outside rtl/ not allowed")
    return sorted(set(problems))


def yosys_read(files: list[Path], top: str, work: Path, log: Path, timeout: float,
               json_out: Path | None = None):
    """Elaborate with Yosys (slang frontend), run proc/check, optional JSON."""
    work.mkdir(parents=True, exist_ok=True)
    script = work / f"{log.stem}.ys"
    lines = [
        "read_slang --top " + top + " " + " ".join(str(f) for f in files),
        f"hierarchy -check -top {top}",
        "proc",
        "check",
    ]
    if json_out is not None:
        lines.append(f"write_json -noscopeinfo {json_out}")
    script.write_text("\n".join(lines) + "\n")
    return run_cmd(["yosys", "-q", "-l", str(log) + ".yosys", "-s", str(script)],
                   cwd=work, timeout=timeout, log=log)


def top_ports(yosys_json: Path, top: str) -> dict:
    data = read_json(yosys_json, {})
    mod = (data.get("modules") or {}).get(top) or {}
    ports = mod.get("ports") or {}
    return {name: {"direction": p.get("direction"), "width": len(p.get("bits", []))}
            for name, p in sorted(ports.items())}


def baseline_ports(design: Design, cache: Path, timeout: float) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / "baseline_ports.json"
    cached = read_json(out)
    if cached:
        return cached
    files = [design.root / f for f in design.rtl_files]
    js = cache / "baseline_elab.json"
    res = yosys_read(files, design.top, cache / "ywork", cache / "baseline_yosys.log", timeout, js)
    if not res.ok:
        raise RuntimeError("baseline does not elaborate in Yosys: " + res.tail(20))
    ports = top_ports(js, design.top)
    write_json(out, ports)
    return ports


def run_build(design: Design, sub: Path, work: Path, logs: Path, cache: Path,
              timeout: float = 300) -> StageResult:
    sw = Stopwatch()
    st = StageResult("build")
    det: dict = {}
    st.details = det
    problems: list[str] = []

    # 1. filelist
    fl = sub / design.filelist_rel
    if not fl.is_file():
        st.status, st.message = "reject", f"{design.filelist_rel} missing from the submission"
        st.seconds = sw.elapsed()
        return st
    sub_list = read_filelist(fl)
    base_list = design.rtl_files
    if sub_list != base_list:
        st.status = "reject"
        st.message = "filelist changed: the filelist must match the released filelist exactly"
        det["expected_filelist"] = base_list
        det["submitted_filelist"] = sub_list
        st.seconds = sw.elapsed()
        return st
    missing = [f for f in sub_list if not (sub / f).is_file()]
    if missing:
        st.status, st.message = "reject", "files in the filelist are missing: " + ", ".join(missing)
        st.seconds = sw.elapsed()
        return st
    extra = sorted(p.relative_to(sub).as_posix() for p in (sub / "rtl").rglob("*")
                   if p.is_file() and p.relative_to(sub).as_posix() not in sub_list
                   and p.name != Path(design.filelist_rel).name)
    if extra:
        det["ignored_rtl_files"] = extra

    # 2. constraint: submissions may not carry a different constraint
    base_sdc = design.constraint.read_bytes() if design.constraint.is_file() else b""
    for sdc in sub.rglob("*.sdc"):
        if sdc.read_bytes() != base_sdc:
            problems.append(f"{sdc.relative_to(sub)}: clock constraint differs from the released one")

    # 3. forbidden constructs
    texts = {f: (sub / f).read_text(errors="replace") for f in sub_list}
    problems += scan_constructs(texts)
    if problems:
        st.status, st.message = "reject", "non-synthesizable or disallowed constructs"
        det["problems"] = problems
        st.seconds = sw.elapsed()
        return st

    files = [sub / f for f in sub_list]

    # 4. Verilator lint + elaboration
    lint = run_cmd(["verilator", "--lint-only", "-Wno-fatal", "-Wno-DECLFILENAME",
                    "-Wno-UNUSEDSIGNAL", "-Wno-UNUSEDPARAM", "--top-module", design.top]
                   + [str(f) for f in files], cwd=sub, timeout=timeout,
                   log=logs / "build_verilator_lint.log")
    det["verilator_lint"] = {"ok": lint.ok, "seconds": round(lint.seconds, 2)}
    lint_text = Path(lint.log).read_text(errors="replace")
    det["verilator_warnings"] = len(re.findall(r"^%Warning", lint_text, re.M))
    if not lint.ok:
        st.status = "reject"
        st.message = "Verilator lint/elaboration failed"
        det["log_tail"] = lint.tail(30)
        st.seconds = sw.elapsed()
        return st

    # 5. Yosys read (synthesizability) + ports
    base_ports = baseline_ports(design, cache, timeout)
    js = work / "sub_elab.json"
    yr = yosys_read(files, design.top, work / "ywork", logs / "build_yosys.log", timeout, js)
    det["yosys_read"] = {"ok": yr.ok, "seconds": round(yr.seconds, 2)}
    if not yr.ok:
        st.status = "reject"
        st.message = "Yosys could not read or elaborate the design (non-synthesizable construct?)"
        det["log_tail"] = yr.tail(30)
        st.seconds = sw.elapsed()
        return st
    ports = top_ports(js, design.top)
    if ports != base_ports:
        diff = {}
        for name in sorted(set(ports) | set(base_ports)):
            if ports.get(name) != base_ports.get(name):
                diff[name] = {"released": base_ports.get(name), "submitted": ports.get(name)}
        st.status, st.message = "reject", "top-level ports changed"
        det["port_diff"] = diff
        st.seconds = sw.elapsed()
        return st
    ylog = Path(str(logs / "build_yosys.log") + ".yosys")
    if ylog.exists():
        txt = ylog.read_text(errors="replace")
        m = re.search(r"Found and reported (\d+) problems", txt)
        det["yosys_check_problems"] = int(m.group(1)) if m else 0
    st.status = "pass"
    st.message = "design builds; filelist, ports and constraint unchanged"
    st.seconds = sw.elapsed()
    return st


def dump_json(obj, path: Path) -> None:
    path.write_text(json.dumps(obj, indent=2))
