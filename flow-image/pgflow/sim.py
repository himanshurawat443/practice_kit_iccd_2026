"""Verilator + cocotb build and run helpers used by every simulation stage."""

from __future__ import annotations

import dataclasses
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence

from .util import CmdResult, run_cmd, sandbox_dir, write_json

ASSERT_RE = re.compile(r"(Assertion failed|%Error: .*(assert|Assert)|\[\d+\] %Error)", re.I)


@dataclasses.dataclass
class TestOutcome:
    name: str
    module: str
    passed: bool
    message: str = ""
    seconds: float = 0.0


@dataclasses.dataclass
class RunOutcome:
    cmd: CmdResult
    tests: list[TestOutcome]
    assertion_fired: bool

    @property
    def all_passed(self) -> bool:
        return (self.cmd.ok and bool(self.tests)
                and all(t.passed for t in self.tests) and not self.assertion_fired)

    def failed_names(self) -> list[str]:
        return [t.name for t in self.tests if not t.passed]

    def to_dict(self) -> dict:
        return {
            "returncode": self.cmd.returncode,
            "timed_out": self.cmd.timed_out,
            "seconds": round(self.cmd.seconds, 2),
            "assertion_fired": self.assertion_fired,
            "tests": [dataclasses.asdict(t) for t in self.tests],
        }


def discover_test_modules(test_dir: Path) -> list[str]:
    return sorted(p.stem for p in Path(test_dir).glob("test_*.py") if p.is_file())


def build(*, sources: Sequence[str], top: str, build_dir: Path, build_args: Sequence[str],
          includes: Sequence[str] = (), timescale=None, timeout: float, log: Path,
          jobs: int = 4, sandbox: bool = False) -> CmdResult:
    build_dir = sandbox_dir(build_dir, sandbox)
    spec = {
        "action": "build", "sources": list(sources), "top": top,
        "build_dir": str(build_dir), "build_args": list(build_args),
        "includes": list(includes), "timescale": timescale,
    }
    spec_path = build_dir.parent / f"{build_dir.name}.build.json"
    write_json(spec_path, spec)
    env = {"MAKEFLAGS": f"-j{jobs}", "COCOTB_MAX_PARALLEL_BUILD_JOBS": str(jobs)}
    return run_cmd([sys.executable, "-m", "pgflow.simrun", str(spec_path)],
                   cwd=build_dir.parent, timeout=timeout, env=env, log=log, sandbox=sandbox)


def parse_results(xml_path: Path) -> list[TestOutcome]:
    out: list[TestOutcome] = []
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        return out
    for tc in root.iter("testcase"):
        fail = tc.find("failure")
        err = tc.find("error")
        skipped = tc.find("skipped")
        msg = ""
        node = fail if fail is not None else err
        if node is not None:
            msg = (node.get("message") or node.text or "").strip()[:500]
        try:
            secs = float(tc.get("time", "0") or 0)
        except ValueError:
            secs = 0.0
        out.append(TestOutcome(
            name=tc.get("name", "?"), module=tc.get("classname", ""),
            passed=(node is None and skipped is None), message=msg, seconds=secs))
    return out


def run_tests(*, modules: Sequence[str], top: str, build_dir: Path, test_dir: Path,
              pythonpath: Sequence[str], timeout: float, log: Path, plusargs: Sequence[str] = (),
              seed: int | None = None, timescale=None, sandbox: bool = False,
              env: dict | None = None) -> RunOutcome:
    test_dir = sandbox_dir(test_dir, sandbox)
    results_xml = test_dir / "results.xml"
    if results_xml.exists():
        results_xml.unlink()
    spec = {
        "action": "test", "modules": list(modules), "top": top,
        "build_dir": str(build_dir), "test_dir": str(test_dir),
        "results_xml": str(results_xml), "pythonpath": list(pythonpath),
        "plusargs": list(plusargs), "seed": seed, "timescale": timescale,
        "env": dict(env or {}),
    }
    spec_path = test_dir / "run.json"
    write_json(spec_path, spec)
    res = run_cmd([sys.executable, "-m", "pgflow.simrun", str(spec_path)],
                  cwd=test_dir, timeout=timeout, log=log, sandbox=sandbox,
                  env={"PYTHONDONTWRITEBYTECODE": "1"})
    tests = parse_results(results_xml)
    fired = False
    try:
        text = Path(log).read_text(errors="replace")
        fired = bool(ASSERT_RE.search(text))
    except OSError:
        pass
    return RunOutcome(res, tests, fired)


def env_pythonpath(*dirs: os.PathLike | str) -> list[str]:
    return [str(Path(d).resolve()) for d in dirs if d and Path(d).is_dir()]
