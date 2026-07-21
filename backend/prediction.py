"""
Crime-density prediction — Holt-Winters exponential smoothing.

Design:
  * Aggregates weekly counts per (Unit, CrimeHead) from the weekly_counts
    materialized table populated at ingest.
  * Fits Holt-Winters (level + trend + additive seasonality, weekly period=52
    when enough history is available, else no seasonality) with numpy only.
  * Emits point forecasts + 90% prediction interval for the next N weeks.
  * Aggregation helpers roll station-level forecasts up to a district-level
    "predicted density" for the map.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

# Numpy is optional — if unavailable we fall back to a pure-Python impl.
try:
    import numpy as np  # type: ignore
    _HAS_NP = True
except Exception:
    _HAS_NP = False


@dataclass
class Forecast:
    horizon_weeks: int
    weeks: list[str]         # ISO date (Monday) of each forecast week
    point: list[float]
    lower: list[float]
    upper: list[float]
    fitted: list[float]      # in-sample fit (for chart overlay)
    history_weeks: list[str]
    history_values: list[int]
    method: str


def _weekly_series(conn: sqlite3.Connection, unit_id: int | None, head_id: int | None,
                   district_id: int | None = None) -> tuple[list[str], list[int]]:
    """Return dense weekly counts (fill missing weeks with 0)."""
    filters = []
    args: list = []
    if unit_id is not None:
        filters.append("wc.unit_id = ?"); args.append(unit_id)
    if head_id is not None:
        filters.append("wc.head_id = ?"); args.append(head_id)
    if district_id is not None:
        filters.append("u.DistrictID = ?"); args.append(district_id)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    rows = conn.execute(
        f"""SELECT wc.week_start, SUM(wc.count) AS c
        FROM weekly_counts wc JOIN Unit u ON u.UnitID = wc.unit_id
        {where}
        GROUP BY wc.week_start ORDER BY wc.week_start""",
        args,
    ).fetchall()
    if not rows:
        return [], []
    start = date.fromisoformat(rows[0][0])
    end = date.fromisoformat(rows[-1][0])
    counts = {r[0]: r[1] for r in rows}
    weeks: list[str] = []
    vals: list[int] = []
    cur = start
    while cur <= end:
        s = cur.isoformat()
        weeks.append(s)
        vals.append(int(counts.get(s, 0)))
        cur += timedelta(days=7)
    return weeks, vals


def _holt_winters(series: list[float], horizon: int, seasonal_period: int = 52) -> Forecast:
    n = len(series)
    if n < 8:
        # Not enough data — fall back to trailing-mean forecast.
        base = sum(series) / max(1, n)
        std = _stddev(series)
        return Forecast(
            horizon_weeks=horizon,
            weeks=[], point=[base] * horizon,
            lower=[max(0.0, base - 1.65 * std)] * horizon,
            upper=[base + 1.65 * std] * horizon,
            fitted=list(series),
            history_weeks=[], history_values=[int(x) for x in series],
            method="mean-fallback",
        )

    alpha, beta_ = 0.35, 0.10
    gamma = 0.20 if n >= 2 * seasonal_period else 0.0
    use_season = gamma > 0

    level = series[0]
    trend = (series[min(seasonal_period, n - 1)] - series[0]) / max(1, min(seasonal_period, n - 1))
    if use_season:
        # Initialize seasonal components as detrended residuals from the first cycle.
        first_cycle = series[:seasonal_period]
        avg = sum(first_cycle) / seasonal_period
        seasonals = [x - avg for x in first_cycle]
    else:
        seasonals = [0.0] * max(seasonal_period, 1)

    fitted: list[float] = []
    residuals: list[float] = []
    for t in range(n):
        seas = seasonals[t % seasonal_period] if use_season else 0.0
        est = level + trend + seas
        fitted.append(est)
        residuals.append(series[t] - est)
        prev_level = level
        level = alpha * (series[t] - seas) + (1 - alpha) * (level + trend)
        trend = beta_ * (level - prev_level) + (1 - beta_) * trend
        if use_season:
            seasonals[t % seasonal_period] = gamma * (series[t] - level) + (1 - gamma) * seas

    sigma = _stddev(residuals)
    forecasts: list[float] = []
    for h in range(1, horizon + 1):
        seas = seasonals[(n + h - 1) % seasonal_period] if use_season else 0.0
        forecasts.append(max(0.0, level + h * trend + seas))
    # 90% interval widens with horizon.
    lower = [max(0.0, f - 1.65 * sigma * math.sqrt(h)) for h, f in enumerate(forecasts, start=1)]
    upper = [f + 1.65 * sigma * math.sqrt(h) for h, f in enumerate(forecasts, start=1)]

    return Forecast(
        horizon_weeks=horizon,
        weeks=[], point=forecasts, lower=lower, upper=upper, fitted=fitted,
        history_weeks=[], history_values=[int(x) for x in series],
        method="holt-winters" + ("-seasonal" if use_season else "-nonseasonal"),
    )


def _stddev(x: Iterable[float]) -> float:
    xs = list(x)
    if not xs: return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((v - m) ** 2 for v in xs) / max(1, len(xs) - 1))


def forecast_unit_head(conn: sqlite3.Connection, unit_id: int, head_id: int,
                       horizon: int = 8) -> Forecast:
    weeks, vals = _weekly_series(conn, unit_id=unit_id, head_id=head_id)
    f = _holt_winters([float(v) for v in vals], horizon)
    f.history_weeks = weeks
    if weeks:
        last = date.fromisoformat(weeks[-1])
        f.weeks = [(last + timedelta(days=7 * (i + 1))).isoformat() for i in range(horizon)]
    return f


def forecast_district_head(conn: sqlite3.Connection, district_id: int, head_id: int,
                           horizon: int = 8) -> Forecast:
    weeks, vals = _weekly_series(conn, unit_id=None, head_id=head_id, district_id=district_id)
    f = _holt_winters([float(v) for v in vals], horizon)
    f.history_weeks = weeks
    if weeks:
        last = date.fromisoformat(weeks[-1])
        f.weeks = [(last + timedelta(days=7 * (i + 1))).isoformat() for i in range(horizon)]
    return f


def predicted_density_map(conn: sqlite3.Connection, head_id: int | None,
                          horizon: int = 4) -> list[dict]:
    """
    Return one row per Karnataka police station with the predicted count over
    the next `horizon` weeks. Used to render a 'future crime density' heatmap.
    """
    # Pull all (unit, head) series in bulk, then run the forecaster.
    if head_id is None:
        head_filter = ""
        args: list = []
    else:
        head_filter = "AND wc.head_id = ?"
        args = [head_id]
    rows = conn.execute(
        f"""SELECT wc.unit_id, wc.week_start, SUM(wc.count) c
        FROM weekly_counts wc
        WHERE 1=1 {head_filter}
        GROUP BY wc.unit_id, wc.week_start ORDER BY wc.unit_id, wc.week_start""",
        args,
    ).fetchall()
    # Group by unit.
    per_unit: dict[int, list[tuple[str, int]]] = {}
    for uid, ws, c in rows:
        per_unit.setdefault(uid, []).append((ws, c))

    # Densify each unit's series to a common weekly grid.
    if not per_unit:
        return []
    all_weeks = sorted({r[1] for r in rows})
    start = date.fromisoformat(all_weeks[0])
    end   = date.fromisoformat(all_weeks[-1])

    def dense(u: int) -> list[float]:
        counts = dict(per_unit[u])
        out = []
        cur = start
        while cur <= end:
            out.append(float(counts.get(cur.isoformat(), 0)))
            cur += timedelta(days=7)
        return out

    unit_geo = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT UnitID, Lat, Lng FROM Unit WHERE StateID = 29 AND Lat IS NOT NULL"
    ).fetchall()}

    results = []
    for uid, series in per_unit.items():
        if uid not in unit_geo: continue
        vals = dense(uid)
        if sum(vals) < 5:      # not enough signal to bother
            continue
        f = _holt_winters(vals, horizon)
        pred = sum(f.point)
        lat, lng = unit_geo[uid]
        results.append({
            "unit_id": uid, "lat": lat, "lng": lng,
            "recent_actual": int(sum(vals[-horizon:])) if len(vals) >= horizon else int(sum(vals)),
            "predicted_next": round(pred, 1),
            "delta_pct": round((pred - sum(vals[-horizon:])) / max(1, sum(vals[-horizon:])) * 100, 1)
                          if len(vals) >= horizon else None,
        })
    return results
