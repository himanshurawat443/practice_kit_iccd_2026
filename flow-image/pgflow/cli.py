"""Command-line entry point: pgflow run | baseline | sweep | version."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__


def _cmd_run(a) -> int:
    from .pipeline import run
    skip = {s.strip() for v in (a.skip or []) for s in v.split(",") if s.strip()}
    rep = run(a.design, a.submission, a.out, mode=a.mode, jobs=a.jobs, cache=a.cache,
              keep_work=a.keep_work, skip=skip)
    summary = {k: rep.get(k) for k in ("status", "rejected_at", "reason", "score", "C", "V", "P")}
    if rep.get("mode") == "public":
        summary["projected_score_if_all_hidden_checks_pass"] = rep.get(
            "projected_score_if_all_hidden_checks_pass")
    summary["seconds"] = rep.get("seconds")
    print(json.dumps(summary, indent=2))
    return 0 if rep["status"] != "error" else 2


def _cmd_baseline(a) -> int:
    from pathlib import Path

    from .design import load_design
    from .pipeline import default_cache
    from .ppa import baseline_ppa, cache_dir
    from .util import cpu_jobs
    d = load_design(a.design)
    cache = cache_dir(d, Path(a.cache).resolve() if a.cache else default_cache())
    logs = cache / "logs"
    logs.mkdir(exist_ok=True)
    res = baseline_ppa(d, cache, cpu_jobs(a.jobs), 3600, logs)
    print(json.dumps({"ok": res.get("ok"), "final": res.get("final"),
                      "step_seconds": res.get("step_seconds"), "cache": str(cache)}, indent=2))
    if a.mode == "server":
        from pgflow_server.hidden import prepare_hidden_baseline
        print(json.dumps(prepare_hidden_baseline(d, cache, cpu_jobs(a.jobs)), indent=2))
    return 0 if res.get("ok") else 1


def _cmd_sweep(a) -> int:
    try:
        from pgflow_server.sweep import run_sweep
    except ImportError:
        print("sweep is not available in this image", file=sys.stderr)
        return 2
    res = run_sweep(a.design, a.variant, a.out,
                    [float(x) for x in a.clock_scales.split(",") if x],
                    [float(x) for x in a.utils.split(",") if x], jobs=a.jobs,
                    keep_intermediates=a.keep_intermediates)
    print(json.dumps(res, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pgflow", description="Proving Ground flow runner")
    p.add_argument("--version", action="version", version=f"pgflow {__version__}")
    sp = p.add_subparsers(dest="cmd", required=True)

    r = sp.add_parser("run", help="run the Flow on one submission")
    r.add_argument("--design", required=True, help="design directory (contains design.yaml)")
    r.add_argument("--submission", required=True, help="submission archive (.tar.gz or .zip)")
    r.add_argument("--out", required=True, help="output directory")
    r.add_argument("--mode", choices=["public", "server"], default="public")
    r.add_argument("--jobs", type=int, default=None, help="parallel jobs (default: all CPUs)")
    r.add_argument("--cache", default=None, help="baseline cache directory "
                   "(default $PGFLOW_CACHE or ~/.cache/pgflow)")
    r.add_argument("--keep-work", action="store_true", help="keep OUT/work for debugging")
    r.add_argument("--skip", action="append", default=[],
                   help="stages to skip: mutation, ppa, hidden (comma list, or repeat the flag)")
    r.set_defaults(fn=_cmd_run)

    b = sp.add_parser("baseline", help="run and cache the baseline PPA for a design")
    b.add_argument("--design", required=True)
    b.add_argument("--jobs", type=int, default=None)
    b.add_argument("--cache", default=None)
    b.add_argument("--mode", choices=["public", "server"], default="public",
                   help="server: also run the hidden checks on the reference")
    b.set_defaults(fn=_cmd_baseline)

    import importlib.util
    if importlib.util.find_spec("pgflow_server") is None:
        a = p.parse_args(argv)
        return a.fn(a)
    s = sp.add_parser("sweep", help="re-run place and route of a variant over a parameter grid")
    s.add_argument("--design", required=True)
    s.add_argument("--variant", required=True)
    s.add_argument("--clock-scales", default="0.8,1.2,1.5")
    s.add_argument("--utils", default="0.35,0.5,0.65")
    s.add_argument("--out", required=True)
    s.add_argument("--jobs", type=int, default=None)
    s.add_argument("--keep-intermediates", action="store_true")
    s.set_defaults(fn=_cmd_sweep)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
