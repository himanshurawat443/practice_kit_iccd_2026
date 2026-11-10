"""Subprocess entry point for cocotb builds and test runs.

Invoked as `python3 -m pgflow.simrun SPEC.json` so that builds and test runs
can be time-limited, killed as a process group, and (on the server) run under
a non-privileged account.
"""

from __future__ import annotations

import json
import os
import sys


def main(argv: list[str]) -> int:
    spec = json.loads(open(argv[1]).read())
    from cocotb_tools.runner import get_runner

    runner = get_runner("verilator")
    if spec["action"] == "build":
        runner.build(
            sources=spec["sources"],
            includes=spec.get("includes", []),
            hdl_toplevel=spec["top"],
            build_args=spec.get("build_args", []),
            build_dir=spec["build_dir"],
            timescale=tuple(spec["timescale"]) if spec.get("timescale") else None,
            always=True,
        )
        return 0
    if spec["action"] == "test":
        pp = os.pathsep.join(spec.get("pythonpath", []))
        env = {"PYTHONPATH": pp + (os.pathsep + os.environ["PYTHONPATH"]
                                   if os.environ.get("PYTHONPATH") else "")}
        env.update(spec.get("env", {}))
        os.environ.update(env)
        sys.path[:0] = spec.get("pythonpath", [])
        runner.test(
            test_module=spec["modules"],
            hdl_toplevel=spec["top"],
            hdl_toplevel_lang="verilog",
            build_dir=spec["build_dir"],
            test_dir=spec["test_dir"],
            results_xml=spec["results_xml"],
            plusargs=spec.get("plusargs", []),
            seed=spec.get("seed"),
            extra_env=env,
            timescale=tuple(spec["timescale"]) if spec.get("timescale") else None,
        )
        return 0
    print(f"unknown action {spec['action']}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
