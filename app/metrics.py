"""Derived metrics from a decoded activity. Pure functions on numpy arrays.

Definitions (so an AI agent or a human can audit them):
- moving: a sample interval counts as moving when the gap to the previous sample is <= 10 s
  and speed >= 0.5 m/s. moving_s is the sum of those intervals.
- grade: smoothed altitude difference over a ~40 m distance window.
- GAP (grade-adjusted pace): each moving metre is weighted by the Minetti (2002) energy
  cost of running at that grade relative to flat ground, clamped to +/-45 %.
- VAM: metres climbed per hour while moving on grade > 5 %.
- HR zones: % of HRmax, floors configurable (default 50/60/70/80/90).
- TRIMP: Edwards, minutes in zone x zone number (1..5).
- decoupling: efficiency factor (GAP speed / HR) of the first moving half vs the second;
  positive means HR drifted up for the same effort. < 5 % is usually read as good durability.
"""
import numpy as np

MOVING_SPEED = 0.5
MAX_GAP_S = 10.0
CLIMB_GRADE = 0.05


def minetti(g):
    g = np.clip(g, -0.45, 0.45)
    return 155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3 + 46.3 * g**2 + 19.5 * g + 3.6


C0 = float(minetti(0.0))


def _nan_ma(x: np.ndarray, half: int) -> np.ndarray:
    """Centred moving average ignoring NaN."""
    valid = ~np.isnan(x)
    v = np.where(valid, x, 0.0)
    cs = np.concatenate([[0.0], np.cumsum(v)])
    cn = np.concatenate([[0], np.cumsum(valid)])
    n = len(x)
    lo = np.clip(np.arange(n) - half, 0, n)
    hi = np.clip(np.arange(n) + half + 1, 0, n)
    cnt = cn[hi] - cn[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (cs[hi] - cs[lo]) / cnt
    out[cnt == 0] = np.nan
    return out


def gain_loss(alt: np.ndarray, threshold: float = 3.0) -> tuple[float, float]:
    up = down = 0.0
    ref = None
    for a in alt:
        if np.isnan(a):
            continue
        if ref is None:
            ref = a
            continue
        d = a - ref
        if d >= threshold:
            up += d
            ref = a
        elif d <= -threshold:
            down -= d
            ref = a
    return up, down


def compute(p) -> dict:
    """p: fit_parser.Parsed. Returns per-sample derived arrays plus summary metrics."""
    n = len(p.t)
    alt = _nan_ma(p.altitude, 7)
    d = p.distance

    # grade over ~40 m: compare points 20 m behind and ahead
    lo = np.searchsorted(d, d - 20, side="left")
    hi = np.clip(np.searchsorted(d, d + 20, side="right") - 1, 0, n - 1)
    dd = d[hi] - d[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        grade = (alt[hi] - alt[lo]) / dd
    grade[(dd < 10) | np.isnan(grade)] = 0.0
    grade = np.clip(grade, -0.45, 0.45)

    step = np.diff(p.t, prepend=p.t[0])
    ddist = np.diff(d, prepend=d[0])
    with np.errstate(invalid="ignore", divide="ignore"):
        v = np.where(np.isnan(p.speed), ddist / np.where(step > 0, step, np.nan), p.speed)
    moving = (step > 0) & (step <= MAX_GAP_S) & (np.nan_to_num(v) >= MOVING_SPEED)
    moving[0] = False
    dt = np.where(moving, step, 0.0)
    w = minetti(grade) / C0
    gap_d = np.where(moving, ddist * w, 0.0)

    dalt = np.diff(alt, prepend=alt[0])
    climbing = moving & (grade > CLIMB_GRADE) & ~np.isnan(dalt)
    climb_s = float(dt[climbing].sum())
    climb_m = float(np.clip(dalt[climbing], 0, None).sum())

    moving_s = float(dt.sum())
    total_d = float(d[-1] - d[0])
    gap_total = float(gap_d.sum())

    # HR histogram over recorded time (not only moving): load accrues while stopped too
    hr_hist: dict[str, float] = {}
    rec = (step > 0) & (step <= MAX_GAP_S) & ~np.isnan(p.hr)
    for bpm, s in zip(np.round(p.hr[rec]).astype(int), step[rec]):
        hr_hist[str(bpm)] = hr_hist.get(str(bpm), 0.0) + float(s)
    hr_hist = {k: round(v, 1) for k, v in hr_hist.items()}

    # aerobic decoupling on moving samples with HR, needs >= 20 min
    decoupling = None
    m = moving & ~np.isnan(p.hr)
    if dt[m].sum() >= 1200:
        cum = np.cumsum(np.where(m, dt, 0.0))
        half = cum[-1] / 2
        first, second = m & (cum <= half), m & (cum > half)
        def ef(mask):
            hr_avg = np.average(p.hr[mask], weights=dt[mask])
            return (gap_d[mask].sum() / dt[mask].sum()) / hr_avg
        e1, e2 = ef(first), ef(second)
        if e1 > 0:
            decoupling = round(float((e1 - e2) / e1 * 100), 1)

    up, down = gain_loss(alt)
    return {
        "alt_smooth": alt, "grade": grade, "moving_mask": moving, "dt": dt, "gap_d": gap_d,
        "moving_s": moving_s,
        "distance_m": total_d,
        "gap_distance_m": gap_total,
        "avg_pace_s_km": moving_s / total_d * 1000 if total_d > 50 and moving_s else None,
        "gap_pace_s_km": moving_s / gap_total * 1000 if gap_total > 50 and moving_s else None,
        "climb_s": climb_s, "climb_m": climb_m,
        "vam_m_h": climb_m / climb_s * 3600 if climb_s >= 120 else None,
        "ascent_calc": up, "descent_calc": down,
        "hr_hist": hr_hist,
        "decoupling_pct": decoupling,
    }


def splits_per_km(p, m: dict) -> list[dict]:
    d = p.distance
    if d[-1] < 500:
        return []
    out = []
    edges = list(np.arange(1000, d[-1], 1000)) + [d[-1]]
    start = 0
    for km, edge in enumerate(edges, start=1):
        end = int(np.searchsorted(d, edge, side="left"))
        end = min(max(end, start + 1), len(d) - 1)
        sl = slice(start + 1, end + 1)
        dist = float(d[end] - d[start])
        if dist < 50:
            break
        mov = float(m["dt"][sl].sum())
        gap = float(m["gap_d"][sl].sum())
        hrm = ~np.isnan(p.hr[sl]) & (m["dt"][sl] > 0)
        dalt = np.diff(m["alt_smooth"][start:end + 1])
        dalt = dalt[~np.isnan(dalt)]
        out.append({
            "km": km, "distance_m": round(dist, 1), "moving_s": round(mov),
            "pace_s_km": round(mov / dist * 1000, 1) if mov else None,
            "gap_s_km": round(mov / gap * 1000, 1) if gap > 0 and mov else None,
            "hr_avg": int(round(np.average(p.hr[sl][hrm], weights=m["dt"][sl][hrm]))) if hrm.any() else None,
            "ascent_m": round(float(dalt[dalt > 0].sum()), 1),
            "descent_m": round(abs(float(dalt[dalt < 0].sum())), 1),
        })
        start = end
    return out


def zone_seconds(hr_hist: dict, hr_max: int, floors: list[float]) -> list[int]:
    z = [0.0] * len(floors)
    for bpm, sec in (hr_hist or {}).items():
        f = int(bpm) / hr_max
        for i in range(len(floors) - 1, -1, -1):
            if f >= floors[i]:
                z[i] += sec
                break
    return [int(round(x)) for x in z]


def edwards_trimp(zone_s: list[int]) -> float:
    return round(sum(s / 60 * (i + 1) for i, s in enumerate(zone_s)), 1)
