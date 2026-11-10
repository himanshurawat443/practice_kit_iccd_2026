"""Design directory loader (layout contract shared with the design packages)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

DEFAULT_BUILD_ARGS = ["-Wno-fatal", "--x-assign", "unique", "--x-initial", "unique"]


def read_filelist(path: Path) -> list[str]:
    """Return filelist entries (relative paths), skipping comments and blanks."""
    out = []
    for raw in path.read_text().splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


@dataclasses.dataclass
class Design:
    root: Path
    id: str
    top: str
    clock_port: str
    reset_port: str
    clock_period_ns: float
    filelist_rel: str
    sim: str
    lines: int
    raw: dict

    # ------------------------------------------------------------------ paths
    @property
    def filelist_path(self) -> Path:
        return self.root / self.filelist_rel

    @property
    def rtl_files(self) -> list[str]:
        """Filelist entries, relative to the design root (e.g. rtl/a.sv)."""
        return read_filelist(self.filelist_path)

    @property
    def constraint(self) -> Path:
        return self.root / "flow" / "constraint.sdc"

    @property
    def public_tb(self) -> Path:
        return self.root / "tb" / "public"

    @property
    def hidden(self) -> Path:
        return self.root / "hidden"

    # --------------------------------------------------------------- options
    @property
    def build_args(self) -> list[str]:
        sim = self.raw.get("sim_options") or {}
        if isinstance(self.raw.get("sim"), dict):
            sim = self.raw["sim"]
        args = sim.get("build_args") if isinstance(sim, dict) else None
        return list(args) if args else list(DEFAULT_BUILD_ARGS)

    @property
    def timescale(self) -> tuple[str, str] | None:
        ts = self.raw.get("timescale")
        if isinstance(ts, (list, tuple)) and len(ts) == 2:
            return (str(ts[0]), str(ts[1]))
        return None

    @property
    def ppa_options(self) -> dict:
        """ORFS variables for this design (core utilization, density...)."""
        opts = {"CORE_UTILIZATION": 40, "PLACE_DENSITY_LB_ADDON": 0.20}
        opts.update(self.raw.get("ppa") or self.raw.get("orfs") or {})
        return {str(k).upper(): v for k, v in opts.items()}


def load_design(root: str | Path) -> Design:
    root = Path(root).resolve()
    cfg_path = root / "design.yaml"
    if not cfg_path.is_file():
        raise FileNotFoundError(f"{cfg_path} not found")
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    sim = raw.get("sim", "verilator")
    if isinstance(sim, dict):
        sim_name = sim.get("simulator", sim.get("name", "verilator"))
    else:
        sim_name = str(sim)
    filelist = raw.get("filelist", "rtl/filelist.f")
    d = Design(
        root=root,
        id=str(raw.get("id", root.name)),
        top=str(raw["top"]),
        clock_port=str(raw.get("clock_port", "clk")),
        reset_port=str(raw.get("reset_port", "rst_n")),
        clock_period_ns=float(raw.get("clock_period_ns", 10.0)),
        filelist_rel=str(filelist),
        sim=sim_name,
        lines=int(raw.get("lines", 0) or 0),
        raw=raw,
    )
    missing = [p for p in d.rtl_files if not (root / p).is_file()]
    if missing:
        raise FileNotFoundError(f"filelist entries missing in {root}: {missing}")
    return d
