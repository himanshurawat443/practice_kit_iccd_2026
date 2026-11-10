"""Scoring: score = 100 x C x (0.5 V + 0.5 P). See SCORING.md."""

from __future__ import annotations

TIMING_FAIL_CAP = 0.3
W_AREA, W_TIMING, W_POWER = 0.4, 0.4, 0.2


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def rel_improvement(base: float, new: float) -> float:
    """(base - new) / base, clipped to [-1, 1]. Lower is better for all inputs."""
    if base is None or new is None or base <= 0:
        return 0.0
    return clip((base - new) / base, -1.0, 1.0)


def achievable_period(clock_period_ns: float, wns_ns: float) -> float:
    """Clock period the design could meet: T - WNS (WNS signed, ns)."""
    return clock_period_ns - wns_ns


def ppa_score(base: dict, sub: dict, clock_period_ns: float) -> dict:
    """P in [0, 1]; 0.5 at the baseline.

    base / sub: {'area_um2', 'wns_ns', 'power_w'} after routing.
    """
    a = rel_improvement(base["area_um2"], sub["area_um2"])
    t_base = achievable_period(clock_period_ns, base["wns_ns"])
    t_sub = achievable_period(clock_period_ns, sub["wns_ns"])
    t = rel_improvement(t_base, t_sub)
    p = rel_improvement(base["power_w"], sub["power_w"])
    raw = 0.5 + 0.5 * (W_AREA * a + W_TIMING * t + W_POWER * p)
    P = clip(raw, 0.0, 1.0)
    timing_met = sub["wns_ns"] is not None and sub["wns_ns"] >= 0.0
    capped = False
    if not timing_met and P > TIMING_FAIL_CAP:
        P = TIMING_FAIL_CAP
        capped = True
    return {"P": round(P, 4), "a": round(a, 4), "t": round(t, 4), "p": round(p, 4),
            "achievable_period_ns": {"baseline": round(t_base, 4), "submission": round(t_sub, 4)},
            "timing_met": timing_met, "capped": capped}


def total_score(C: float, V: float, P: float) -> float:
    return round(100.0 * clip(C, 0, 1) * (0.5 * clip(V, 0, 1) + 0.5 * clip(P, 0, 1)), 2)
