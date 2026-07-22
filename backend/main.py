"""
KSP Crime Intelligence Platform — API.

Endpoints (grouped):

  Meta / basics
    GET  /health, /meta, /stats

  Geospatial
    GET  /geo/points, /geo/heatmap, /geo/district-summary

  Anomaly / trend
    GET  /anomalies, /trends/emerging

  Prediction (crime density forecast)
    GET  /predict/density-map
    GET  /predict/unit/{unit_id}/head/{head_id}
    GET  /predict/district/{district_id}/head/{head_id}

  Deep criminal network
    GET  /offenders/top
    GET  /network/offender/{pid}
    GET  /network/communities
    GET  /network/central-figures
    GET  /network/mo-similarity

  Cross-border crimes
    GET  /cross-border/summary
    GET  /cross-border/movements
    GET  /cross-border/offenders

  Law & Order
    GET  /law-order/court-pendency
    GET  /law-order/io-workload
    GET  /law-order/chargesheet-rate
    GET  /law-order/gravity-mix
    GET  /law-order/status-funnel

  LLM assistant
    POST /assistant/ask   {"question": "..."}
"""
from __future__ import annotations

import math
import os
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import prediction, network_analysis, llm, auth

DB_PATH = Path(os.environ.get("KSP_DB", "data/ksp.db")).resolve()
AUTH_DB_PATH = Path(os.environ.get("KSP_AUTH_DB", "/tmp/ksp_auth.db")).resolve()
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
KARNATAKA_STATE_ID = 29

app = FastAPI(title="KSP Crime Intelligence Platform", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
)


# Auth tables live in a separate SQLite DB so the crime DB can be regenerated
# freely without wiping user accounts. ensure_schema is idempotent, so it's
# safe to run at every module import.
AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
auth.ensure_schema(str(AUTH_DB_PATH))


# --------------------------------------------------------------------------
# Auth dependency
# --------------------------------------------------------------------------
def get_user(request: Request) -> dict:
    sid = request.cookies.get(auth.SESSION_COOKIE)
    session = auth.load_session(str(AUTH_DB_PATH), sid)
    if not session:
        raise HTTPException(401, "not authenticated")
    return session


def optional_user(request: Request) -> dict | None:
    sid = request.cookies.get(auth.SESSION_COOKIE)
    return auth.load_session(str(AUTH_DB_PATH), sid)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _filter_sql(from_, to, district, head, subhead, class_):
    parts, args = [], []
    if from_:      parts.append("cm.IncidentFromDate >= ?"); args.append(f"{from_}T00:00:00")
    if to:         parts.append("cm.IncidentFromDate <= ?"); args.append(f"{to}T23:59:59")
    if district:   parts.append("u.DistrictID = ?");         args.append(district)
    if head:       parts.append("cm.CrimeMajorHeadID = ?");  args.append(head)
    if subhead:    parts.append("cm.CrimeMinorHeadID = ?");  args.append(subhead)
    if class_:     parts.append("ch.CrimeGroupName = ?");    args.append(class_)
    where = ("WHERE " + " AND ".join(parts)) if parts else ""
    return where, args


# ---------- meta / health ------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True, "db": str(DB_PATH), "exists": DB_PATH.exists()}


@app.get("/meta")
def meta():
    with db() as c:
        districts = [dict(r) for r in c.execute(
            "SELECT DistrictID AS id, DistrictName AS name, Zone AS zone, HqLat AS hq_lat, "
            "HqLng AS hq_lng, Population AS population, UrbanPct AS urban_pct "
            "FROM District WHERE StateID = ? ORDER BY name",
            (KARNATAKA_STATE_ID,),
        )]
        heads = [dict(r) for r in c.execute(
            "SELECT CrimeHeadID AS id, CrimeGroupName AS name FROM CrimeHead ORDER BY name"
        )]
        subheads = [dict(r) for r in c.execute(
            "SELECT sh.CrimeSubHeadID AS id, sh.CrimeHeadName AS name, "
            "       sh.CrimeHeadID AS head_id, ch.CrimeGroupName AS head_name "
            "FROM CrimeSubHead sh JOIN CrimeHead ch ON ch.CrimeHeadID = sh.CrimeHeadID "
            "ORDER BY head_name, sh.SeqID"
        )]
        rng = c.execute("SELECT MIN(IncidentFromDate) AS mn, MAX(IncidentFromDate) AS mx FROM CaseMaster").fetchone()
    return {
        "districts": districts, "crime_heads": heads, "crime_subheads": subheads,
        "date_range": {"min": rng["mn"], "max": rng["mx"]},
    }


# ---------- stats --------------------------------------------------------------------
@app.get("/stats")
def stats(
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    district: int | None = None,
    head: int | None = None,
    subhead: int | None = None,
    class_: str | None = Query(None, alias="class"),
):
    where, args = _filter_sql(from_, to, district, head, subhead, class_)
    base = ("FROM CaseMaster cm "
            "JOIN Unit u ON u.UnitID = cm.PoliceStationID "
            "JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID ")
    with db() as c:
        total = c.execute(f"SELECT COUNT(*) {base}{where}", args).fetchone()[0]

        by_class = [dict(r) for r in c.execute(
            f"SELECT ch.CrimeGroupName AS class, COUNT(*) AS count {base}{where} "
            f"GROUP BY ch.CrimeGroupName ORDER BY count DESC", args
        )]
        by_category = [dict(r) for r in c.execute(
            f"SELECT sh.CrimeHeadName AS label, ch.CrimeGroupName AS class, COUNT(*) AS count "
            f"{base} JOIN CrimeSubHead sh ON sh.CrimeSubHeadID = cm.CrimeMinorHeadID {where} "
            f"GROUP BY sh.CrimeHeadName ORDER BY count DESC", args
        )]
        by_hour = [dict(r) for r in c.execute(
            f"SELECT CAST(strftime('%H', cm.IncidentFromDate) AS INTEGER) AS hour, COUNT(*) AS count "
            f"{base}{where} GROUP BY hour ORDER BY hour", args
        )]
        by_month = [dict(r) for r in c.execute(
            f"SELECT strftime('%Y-%m', cm.IncidentFromDate) AS month, COUNT(*) AS count "
            f"{base}{where} GROUP BY month ORDER BY month", args
        )]
        by_status = [dict(r) for r in c.execute(
            f"SELECT s.CaseStatusName AS status, COUNT(*) AS count "
            f"{base} JOIN CaseStatusMaster s ON s.CaseStatusID = cm.CaseStatusID {where} "
            f"GROUP BY s.CaseStatusName", args
        )]
        by_gravity = [dict(r) for r in c.execute(
            f"SELECT g.LookupValue AS gravity, COUNT(*) AS count "
            f"{base} JOIN GravityOffence g ON g.GravityOffenceID = cm.GravityOffenceID {where} "
            f"GROUP BY g.LookupValue ORDER BY count DESC", args
        )]

    return {
        "total": total, "by_class": by_class, "by_category": by_category,
        "by_hour": by_hour, "by_month": by_month, "by_status": by_status,
        "by_gravity": by_gravity,
    }


# ---------- geospatial ---------------------------------------------------------------
@app.get("/geo/points")
def geo_points(
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    district: int | None = None,
    head: int | None = None,
    subhead: int | None = None,
    class_: str | None = Query(None, alias="class"),
    limit: int = Query(3000, ge=100, le=10000),
):
    where, args = _filter_sql(from_, to, district, head, subhead, class_)
    with db() as c:
        rows = c.execute(
            f"SELECT cm.CaseMasterID AS id, cm.CrimeNo AS fir_number, cm.latitude AS lat, cm.longitude AS lng, "
            f"       cm.IncidentFromDate AS occurred_at, sh.CrimeHeadName AS category, "
            f"       ch.CrimeGroupName AS class, g.LookupValue AS gravity, cm.modus_operandi AS mo, "
            f"       s.CaseStatusName AS status "
            f"FROM CaseMaster cm "
            f"JOIN Unit u ON u.UnitID = cm.PoliceStationID "
            f"JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID "
            f"JOIN CrimeSubHead sh ON sh.CrimeSubHeadID = cm.CrimeMinorHeadID "
            f"LEFT JOIN GravityOffence g ON g.GravityOffenceID = cm.GravityOffenceID "
            f"LEFT JOIN CaseStatusMaster s ON s.CaseStatusID = cm.CaseStatusID "
            f"{where} ORDER BY cm.IncidentFromDate DESC LIMIT ?",
            args + [limit],
        ).fetchall()
    return {"points": [dict(r) for r in rows]}


@app.get("/geo/heatmap")
def geo_heatmap(
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    district: int | None = None,
    head: int | None = None,
    subhead: int | None = None,
    class_: str | None = Query(None, alias="class"),
    cell_km: float = Query(2.0, ge=0.5, le=10.0),
):
    where, args = _filter_sql(from_, to, district, head, subhead, class_)
    deg = cell_km / 111.0
    with db() as c:
        rows = c.execute(
            f"SELECT ROUND(cm.latitude/?, 0)*? AS glat, ROUND(cm.longitude/?, 0)*? AS glng, COUNT(*) AS c "
            f"FROM CaseMaster cm JOIN Unit u ON u.UnitID = cm.PoliceStationID "
            f"JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID {where} "
            f"GROUP BY glat, glng HAVING c >= 2",
            [deg, deg, deg, deg] + args,
        ).fetchall()
    if not rows:
        return {"cells": [], "cell_km": cell_km, "max": 0}
    mx = max(r["c"] for r in rows)
    return {
        "cells": [{"lat": r["glat"], "lng": r["glng"], "count": r["c"], "intensity": r["c"]/mx} for r in rows],
        "cell_km": cell_km, "max": mx,
    }


@app.get("/geo/district-summary")
def geo_district_summary(
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    head: int | None = None,
    class_: str | None = Query(None, alias="class"),
):
    where, args = _filter_sql(from_, to, None, head, None, class_)
    with db() as c:
        rows = c.execute(
            f"SELECT d.DistrictID AS id, d.DistrictName AS name, d.Zone AS zone, "
            f"       d.HqLat AS hq_lat, d.HqLng AS hq_lng, d.Population AS population, "
            f"       COUNT(cm.CaseMasterID) AS count, "
            f"       ROUND(COUNT(cm.CaseMasterID) * 100000.0 / NULLIF(d.Population,0), 2) AS per_lakh "
            f"FROM District d "
            f"LEFT JOIN Unit u ON u.DistrictID = d.DistrictID "
            f"LEFT JOIN CaseMaster cm ON cm.PoliceStationID = u.UnitID "
            f"LEFT JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID "
            f"WHERE d.StateID = ? {('AND ' + where[6:]) if where else ''} "
            f"GROUP BY d.DistrictID ORDER BY count DESC",
            [KARNATAKA_STATE_ID] + args,
        ).fetchall()
    return {"districts": [dict(r) for r in rows]}


# ---------- anomalies / trends -------------------------------------------------------
@app.get("/anomalies")
def anomalies(threshold: float = Query(2.0, ge=1.0, le=5.0)):
    with db() as c:
        max_dt = c.execute("SELECT MAX(IncidentFromDate) FROM CaseMaster").fetchone()[0]
        if not max_dt:
            return {"anomalies": []}
        max_d = datetime.fromisoformat(max_dt).date()
        ref_d = max_d.replace(day=1) if max_d.day >= 25 else (max_d.replace(day=1) - timedelta(days=1)).replace(day=1)
        ref_month = ref_d.strftime("%Y-%m")
        prior_start = (ref_d - timedelta(days=365)).replace(day=1)

        rows = c.execute(
            """SELECT u.DistrictID, cm.CrimeMinorHeadID, strftime('%Y-%m', cm.IncidentFromDate) AS ym, COUNT(*) AS c
            FROM CaseMaster cm JOIN Unit u ON u.UnitID = cm.PoliceStationID
            WHERE cm.IncidentFromDate >= ? AND cm.IncidentFromDate < ? AND cm.CrimeMinorHeadID IS NOT NULL
            GROUP BY u.DistrictID, cm.CrimeMinorHeadID, ym""",
            (prior_start.isoformat(), ref_d.isoformat()),
        ).fetchall()
        totals = defaultdict(list)
        for r in rows:
            totals[(r["DistrictID"], r["CrimeMinorHeadID"])].append(r["c"])
        months_in_window = max(1, (ref_d.year - prior_start.year) * 12 + (ref_d.month - prior_start.month))

        recent = c.execute(
            """SELECT u.DistrictID, cm.CrimeMinorHeadID, COUNT(*) AS c
            FROM CaseMaster cm JOIN Unit u ON u.UnitID = cm.PoliceStationID
            WHERE strftime('%Y-%m', cm.IncidentFromDate) = ? AND cm.CrimeMinorHeadID IS NOT NULL
            GROUP BY u.DistrictID, cm.CrimeMinorHeadID""",
            (ref_month,),
        ).fetchall()
        recent_map = {(r["DistrictID"], r["CrimeMinorHeadID"]): r["c"] for r in recent}

        meta_rows = c.execute("""
            SELECT d.DistrictID AS did, d.DistrictName AS dname, d.HqLat, d.HqLng,
                   sh.CrimeSubHeadID AS sid, sh.CrimeHeadName AS sname, ch.CrimeGroupName AS class
            FROM District d CROSS JOIN CrimeSubHead sh
            JOIN CrimeHead ch ON ch.CrimeHeadID = sh.CrimeHeadID
            WHERE d.StateID = ?
        """, (KARNATAKA_STATE_ID,)).fetchall()

    out = []
    for m in meta_rows:
        did, sid = m["did"], m["sid"]
        history = totals.get((did, sid), [])
        observed = recent_map.get((did, sid), 0)
        expected = sum(history) / months_in_window if history else 0
        if expected < 2: continue
        z = (observed - expected) / math.sqrt(expected)
        if z >= threshold:
            out.append({
                "district": m["dname"], "district_id": did,
                "category": m["sname"], "category_id": sid, "class": m["class"],
                "lat": m["HqLat"], "lng": m["HqLng"],
                "observed": observed, "expected": round(expected, 1),
                "z_score": round(z, 2), "month": ref_month,
            })
    out.sort(key=lambda x: -x["z_score"])
    return {"anomalies": out, "reference_month": ref_month, "threshold": threshold}


@app.get("/trends/emerging")
def trends_emerging(window_days: int = 90):
    with db() as c:
        end_str = c.execute("SELECT MAX(IncidentFromDate) FROM CaseMaster").fetchone()[0]
        if not end_str: return {"trends": []}
        end_dt = datetime.fromisoformat(end_str)
        recent_start = end_dt - timedelta(days=window_days)
        prior_start  = end_dt - timedelta(days=window_days * 2)
        rows = c.execute(
            """SELECT sh.CrimeSubHeadID AS id, sh.CrimeHeadName AS label, ch.CrimeGroupName AS class,
                      SUM(CASE WHEN cm.IncidentFromDate >= ? THEN 1 ELSE 0 END) AS recent,
                      SUM(CASE WHEN cm.IncidentFromDate >= ? AND cm.IncidentFromDate < ? THEN 1 ELSE 0 END) AS prior
               FROM CrimeSubHead sh JOIN CrimeHead ch ON ch.CrimeHeadID = sh.CrimeHeadID
               LEFT JOIN CaseMaster cm ON cm.CrimeMinorHeadID = sh.CrimeSubHeadID
               GROUP BY sh.CrimeSubHeadID""",
            (recent_start.isoformat(), prior_start.isoformat(), recent_start.isoformat()),
        ).fetchall()
    trends = []
    for r in rows:
        recent = r["recent"] or 0; prior = r["prior"] or 0
        if prior < 5 and recent < 5: continue
        growth = ((recent - prior) / prior * 100) if prior > 0 else float("inf")
        trends.append({
            "category_id": r["id"], "category": r["label"], "class": r["class"],
            "recent": recent, "prior": prior,
            "growth_pct": None if math.isinf(growth) else round(growth, 1),
        })
    trends.sort(key=lambda t: (t["growth_pct"] is None, -(t["growth_pct"] or 0)))
    return {"trends": trends[:20], "window_days": window_days}


# ---------- prediction ---------------------------------------------------------------
@app.get("/predict/density-map")
def predict_density_map(head: int | None = None, horizon: int = Query(4, ge=1, le=12)):
    with db() as c:
        rows = prediction.predicted_density_map(c, head, horizon=horizon)
    if not rows:
        return {"cells": [], "max": 0, "horizon_weeks": horizon}
    mx = max(r["predicted_next"] for r in rows) or 1
    for r in rows:
        r["intensity"] = round(r["predicted_next"] / mx, 3)
    return {"cells": rows, "max": mx, "horizon_weeks": horizon}


@app.get("/predict/unit/{unit_id}/head/{head_id}")
def predict_unit_head(unit_id: int, head_id: int, horizon: int = 8):
    with db() as c:
        f = prediction.forecast_unit_head(c, unit_id, head_id, horizon=horizon)
    return f.__dict__


@app.get("/predict/district/{district_id}/head/{head_id}")
def predict_district_head(district_id: int, head_id: int, horizon: int = 8):
    with db() as c:
        f = prediction.forecast_district_head(c, district_id, head_id, horizon=horizon)
    return f.__dict__


# ---------- offenders / network ------------------------------------------------------
@app.get("/offenders/top")
def offenders_top(limit: int = 30):
    with db() as c:
        rows = c.execute(
            """SELECT a.person_link_id AS id, a.AccusedName AS full_name,
                      COUNT(DISTINCT a.CaseMasterID) AS incidents,
                      COUNT(DISTINCT cm.CrimeMinorHeadID) AS distinct_categories,
                      MIN(cm.IncidentFromDate) AS first_incident,
                      MAX(cm.IncidentFromDate) AS latest_incident,
                      d.DistrictName AS district
               FROM Accused a
               JOIN CaseMaster cm ON cm.CaseMasterID = a.CaseMasterID
               LEFT JOIN District d ON d.DistrictID = a.home_district_id
               WHERE a.person_link_id IS NOT NULL
               GROUP BY a.person_link_id, a.AccusedName
               HAVING incidents >= 3
               ORDER BY incidents DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return {"offenders": [dict(r) for r in rows]}


@app.get("/network/offender/{pid}")
def network_offender(pid: int, depth: int = Query(2, ge=1, le=3)):
    with db() as c:
        person_row = c.execute(
            """SELECT person_link_id AS id, AccusedName AS full_name,
                      MAX(AgeYear) AS age, MAX(GenderID) AS gender, MAX(home_district_id) AS district_id
               FROM Accused WHERE person_link_id = ?""",
            (pid,),
        ).fetchone()
        if not person_row or person_row["id"] is None:
            raise HTTPException(404, "person not found")

        firs = c.execute(
            """SELECT cm.CaseMasterID AS id, cm.CrimeNo AS fir_number,
                      cm.IncidentFromDate AS occurred_at, cm.latitude AS lat, cm.longitude AS lng,
                      cm.modus_operandi AS mo, sh.CrimeHeadName AS category, ch.CrimeGroupName AS class,
                      u.UnitName AS station
               FROM Accused a
               JOIN CaseMaster cm ON cm.CaseMasterID = a.CaseMasterID
               JOIN CrimeSubHead sh ON sh.CrimeSubHeadID = cm.CrimeMinorHeadID
               JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID
               JOIN Unit u ON u.UnitID = cm.PoliceStationID
               WHERE a.person_link_id = ?
               ORDER BY cm.IncidentFromDate DESC""",
            (pid,),
        ).fetchall()
        case_ids = [r["id"] for r in firs]

        co_offenders: dict[int, dict] = {}
        victims: dict[int, dict] = {}
        if case_ids:
            placeholder = ",".join("?" * len(case_ids))
            for r in c.execute(
                f"""SELECT a.person_link_id AS id, a.AccusedName AS name, COUNT(*) AS shared_firs
                    FROM Accused a
                    WHERE a.CaseMasterID IN ({placeholder}) AND a.person_link_id IS NOT NULL AND a.person_link_id != ?
                    GROUP BY a.person_link_id, a.AccusedName
                    ORDER BY shared_firs DESC LIMIT 60""",
                case_ids + [pid],
            ):
                co_offenders[r["id"]] = dict(r)
            for r in c.execute(
                f"""SELECT v.VictimName AS name, COUNT(*) AS incidents
                    FROM Victim v WHERE v.CaseMasterID IN ({placeholder})
                    GROUP BY v.VictimName ORDER BY incidents DESC LIMIT 40""",
                case_ids,
            ):
                victims[r["name"]] = dict(r)

    nodes = [{
        "id": f"p{pid}", "label": person_row["full_name"],
        "title": f"{person_row['full_name']} · id {pid} · {len(firs)} FIRs",
        "group": "offender_primary",
    }]
    edges = []
    for f in firs[:60]:  # cap per-view
        nid = f"f{f['id']}"
        nodes.append({
            "id": nid, "label": f["fir_number"][-9:],
            "title": f"{f['category']} · {f['occurred_at'][:16]} · {f['station']}",
            "group": f["class"].lower().replace(" ", "_"),
            "shape": "square",
        })
        edges.append({"from": f"p{pid}", "to": nid, "label": "accused"})
    for co_id, r in co_offenders.items():
        nid = f"p{co_id}"
        nodes.append({
            "id": nid, "label": r["name"],
            "title": f"{r['name']} — {r['shared_firs']} shared FIR(s)",
            "group": "offender",
        })
        edges.append({"from": f"p{pid}", "to": nid, "label": f"co-accused × {r['shared_firs']}", "dashes": True})
    for vname, r in victims.items():
        nid = f"v{hash(vname) & 0xFFFFFFFF}"
        nodes.append({
            "id": nid, "label": vname,
            "title": f"victim in {r['incidents']} FIR(s)",
            "group": "victim", "shape": "triangle",
        })
    return {
        "person": {**dict(person_row), "id": pid},
        "firs": [dict(r) for r in firs],
        "graph": {"nodes": nodes, "edges": edges},
        "summary": {
            "total_firs": len(firs), "distinct_co_offenders": len(co_offenders),
            "distinct_victims": len(victims),
        },
    }


@app.get("/network/communities")
def network_communities(top_n: int = Query(200, ge=20, le=1000), min_weight: int = 1):
    with db() as c:
        return network_analysis.analyze_communities(c, str(DB_PATH), min_weight=min_weight, top_n=top_n)


@app.get("/network/central-figures")
def network_central_figures(limit: int = 25):
    with db() as c:
        return {"figures": network_analysis.central_figures(c, str(DB_PATH), limit=limit)}


@app.get("/network/mo-similarity")
def network_mo_similarity(top_n: int = 50):
    with db() as c:
        return network_analysis.mo_similarity(c, str(DB_PATH), top_n=top_n)


# ---------- cross-border -------------------------------------------------------------
@app.get("/cross-border/summary")
def cross_border_summary():
    with db() as c:
        by_state = [dict(r) for r in c.execute(
            """SELECT s.StateName AS state, s.StateID AS state_id, COUNT(*) AS arrests
               FROM ArrestSurrender ars JOIN State s ON s.StateID = ars.ArrestSurrenderStateId
               WHERE ars.ArrestSurrenderStateId != ?
               GROUP BY s.StateID ORDER BY arrests DESC""",
            (KARNATAKA_STATE_ID,),
        )]
        by_district = [dict(r) for r in c.execute(
            """SELECT s.StateName AS state, d.DistrictName AS district,
                      d.HqLat AS lat, d.HqLng AS lng, COUNT(*) AS arrests
               FROM ArrestSurrender ars
               JOIN State s ON s.StateID = ars.ArrestSurrenderStateId
               LEFT JOIN District d ON d.DistrictID = ars.ArrestSurrenderDistrictId
               WHERE ars.ArrestSurrenderStateId != ?
               GROUP BY d.DistrictID ORDER BY arrests DESC LIMIT 50""",
            (KARNATAKA_STATE_ID,),
        )]
        by_class = [dict(r) for r in c.execute(
            """SELECT ch.CrimeGroupName AS class, COUNT(*) AS arrests
               FROM ArrestSurrender ars
               JOIN CaseMaster cm ON cm.CaseMasterID = ars.CaseMasterID
               JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID
               WHERE ars.ArrestSurrenderStateId != ?
               GROUP BY ch.CrimeGroupName ORDER BY arrests DESC""",
            (KARNATAKA_STATE_ID,),
        )]
        total = c.execute(
            "SELECT COUNT(*) FROM ArrestSurrender WHERE ArrestSurrenderStateId != ?",
            (KARNATAKA_STATE_ID,),
        ).fetchone()[0]
    return {"total_cross_border_arrests": total, "by_state": by_state,
            "by_district": by_district, "by_class": by_class}


@app.get("/cross-border/movements")
def cross_border_movements(limit: int = 200):
    """Home-district → arrest-district movement pairs (offenders arrested outside their home district)."""
    with db() as c:
        rows = c.execute(
            """SELECT hd.DistrictName AS home_district, hd.HqLat AS home_lat, hd.HqLng AS home_lng,
                      ad.DistrictName AS arrest_district, ad.HqLat AS arrest_lat, ad.HqLng AS arrest_lng,
                      s.StateName AS arrest_state, COUNT(*) AS movements
               FROM ArrestSurrender ars
               JOIN Accused a ON a.AccusedMasterID = ars.AccusedMasterID
               LEFT JOIN District hd ON hd.DistrictID = a.home_district_id
               LEFT JOIN District ad ON ad.DistrictID = ars.ArrestSurrenderDistrictId
               LEFT JOIN State s ON s.StateID = ars.ArrestSurrenderStateId
               WHERE ars.ArrestSurrenderStateId != ?
                  OR ars.ArrestSurrenderDistrictId != a.home_district_id
               GROUP BY hd.DistrictID, ad.DistrictID
               HAVING movements >= 2
               ORDER BY movements DESC LIMIT ?""",
            (KARNATAKA_STATE_ID, limit),
        ).fetchall()
    return {"movements": [dict(r) for r in rows if r["home_lat"] and r["arrest_lat"]]}


@app.get("/cross-border/offenders")
def cross_border_offenders(limit: int = 30):
    with db() as c:
        rows = c.execute(
            """SELECT a.person_link_id AS id, a.AccusedName AS name,
                      COUNT(DISTINCT ars.ArrestSurrenderStateId) AS distinct_arrest_states,
                      COUNT(*) AS total_arrests,
                      GROUP_CONCAT(DISTINCT s.StateName) AS states
               FROM Accused a
               JOIN ArrestSurrender ars ON ars.AccusedMasterID = a.AccusedMasterID
               JOIN State s ON s.StateID = ars.ArrestSurrenderStateId
               WHERE ars.ArrestSurrenderStateId != ?
               GROUP BY a.person_link_id
               ORDER BY total_arrests DESC LIMIT ?""",
            (KARNATAKA_STATE_ID, limit),
        ).fetchall()
    return {"offenders": [dict(r) for r in rows]}


# ---------- law & order --------------------------------------------------------------
@app.get("/law-order/court-pendency")
def law_court_pendency():
    with db() as c:
        rows = c.execute(
            """SELECT c.CourtID AS court_id, c.CourtName AS court, d.DistrictName AS district,
                      SUM(CASE WHEN st.CaseStatusName IN
                           ('Pending Trial','PendingBeforeCourt','ChargeSheeted') THEN 1 ELSE 0 END) AS pending,
                      COUNT(*) AS total
               FROM CaseMaster cm
               JOIN Court c ON c.CourtID = cm.CourtID
               JOIN CaseStatusMaster st ON st.CaseStatusID = cm.CaseStatusID
               JOIN District d ON d.DistrictID = c.DistrictID
               GROUP BY c.CourtID
               ORDER BY pending DESC LIMIT 40"""
        ).fetchall()
    return {"courts": [dict(r) for r in rows]}


@app.get("/law-order/io-workload")
def law_io_workload():
    with db() as c:
        rows = c.execute(
            """SELECT e.EmployeeID AS io_id,
                      (COALESCE(e.FirstName,'') || ' ' || COALESCE(e.LastName,'')) AS io_name,
                      u.UnitName AS station, d.DistrictName AS district,
                      COUNT(cm.CaseMasterID) AS caseload,
                      SUM(CASE WHEN st.CaseStatusName = 'UnderInvestigation' THEN 1 ELSE 0 END) AS open_cases
               FROM Employee e
               JOIN Designation dg ON dg.DesignationID = e.DesignationID
               LEFT JOIN CaseMaster cm ON cm.PolicePersonID = e.EmployeeID
               LEFT JOIN CaseStatusMaster st ON st.CaseStatusID = cm.CaseStatusID
               LEFT JOIN Unit u ON u.UnitID = e.UnitID
               LEFT JOIN District d ON d.DistrictID = e.DistrictID
               WHERE dg.DesignationName = 'Investigating Officer'
               GROUP BY e.EmployeeID
               ORDER BY caseload DESC LIMIT 40"""
        ).fetchall()
    return {"officers": [dict(r) for r in rows]}


@app.get("/law-order/chargesheet-rate")
def law_chargesheet_rate():
    with db() as c:
        rows = c.execute(
            """SELECT d.DistrictName AS district,
                      COUNT(cm.CaseMasterID) AS total,
                      SUM(CASE WHEN cs.cstype = 'A' THEN 1 ELSE 0 END) AS chargesheeted,
                      SUM(CASE WHEN cs.cstype = 'B' THEN 1 ELSE 0 END) AS false_cases,
                      SUM(CASE WHEN cs.cstype = 'C' THEN 1 ELSE 0 END) AS undetected,
                      ROUND(100.0 * SUM(CASE WHEN cs.cstype = 'A' THEN 1 ELSE 0 END) / NULLIF(COUNT(cm.CaseMasterID),0), 1) AS pct
               FROM CaseMaster cm
               JOIN Unit u ON u.UnitID = cm.PoliceStationID
               JOIN District d ON d.DistrictID = u.DistrictID
               LEFT JOIN ChargesheetDetails cs ON cs.CaseMasterID = cm.CaseMasterID
               GROUP BY d.DistrictID ORDER BY pct DESC"""
        ).fetchall()
    return {"districts": [dict(r) for r in rows]}


@app.get("/law-order/gravity-mix")
def law_gravity_mix():
    with db() as c:
        rows = c.execute(
            """SELECT d.DistrictName AS district, g.LookupValue AS gravity, COUNT(*) AS c
               FROM CaseMaster cm
               JOIN Unit u ON u.UnitID = cm.PoliceStationID
               JOIN District d ON d.DistrictID = u.DistrictID
               JOIN GravityOffence g ON g.GravityOffenceID = cm.GravityOffenceID
               WHERE d.StateID = ?
               GROUP BY d.DistrictID, g.GravityOffenceID
               ORDER BY d.DistrictName, g.LookupValue""",
            (KARNATAKA_STATE_ID,),
        ).fetchall()
    return {"rows": [dict(r) for r in rows]}


@app.get("/law-order/status-funnel")
def law_status_funnel():
    with db() as c:
        rows = c.execute(
            """SELECT st.CaseStatusName AS status, COUNT(*) AS c
               FROM CaseMaster cm JOIN CaseStatusMaster st ON st.CaseStatusID = cm.CaseStatusID
               GROUP BY st.CaseStatusID ORDER BY c DESC"""
        ).fetchall()
    return {"status": [dict(r) for r in rows]}


# ---------- LLM assistant ------------------------------------------------------------
class AskBody(BaseModel):
    question: str
    history: list[dict] | None = None    # [{role: 'user'|'assistant', content: '...'}]
    mode:    str | None = None           # 'auto' | 'analytics' | 'knowledge'


@app.post("/assistant/ask")
def assistant_ask(body: AskBody, user: dict = Depends(get_user)):
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "question required")
    if len(q) > 2000:
        raise HTTPException(400, "question too long (2000 char max)")
    with db() as c:
        r = llm.ask(c, q, history=body.history or [], mode=body.mode or "auto")
    return r.__dict__


@app.get("/assistant/health")
def assistant_health():
    return {"backend": llm._configured_backend()}


# ---------- Authentication -----------------------------------------------------------
@app.post("/auth/register")
def auth_register(body: auth.RegisterBody, response: Response, request: Request):
    user = auth.register_user(str(AUTH_DB_PATH), body)
    sid = auth.create_session(
        str(AUTH_DB_PATH), user["user_id"],
        ip=(request.client.host if request.client else None),
        ua=request.headers.get("user-agent"),
    )
    auth.set_session_cookie(response, sid)
    return {"user": user}


@app.post("/auth/login")
def auth_login(body: auth.LoginBody, response: Response, request: Request):
    user, sid = auth.login_user(
        str(AUTH_DB_PATH), body,
        ip=(request.client.host if request.client else None),
        ua=request.headers.get("user-agent"),
    )
    auth.set_session_cookie(response, sid)
    return {"user": user}


@app.post("/auth/logout")
def auth_logout(response: Response, request: Request):
    sid = request.cookies.get(auth.SESSION_COOKIE)
    if sid:
        auth.destroy_session(str(AUTH_DB_PATH), sid)
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/auth/me")
def auth_me(user: dict = Depends(get_user)):
    return {"user": user}


# ---------- Route protection middleware ----------------------------------------------
# Endpoints that require an authenticated session. Everything under /api/* is
# auto-protected via the middleware below; the *original* endpoints are still
# reachable at their bare paths for backward compatibility but ALSO require auth.
PUBLIC_PATHS = {
    "/", "/app", "/login", "/register",
    "/health", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect",
    "/favicon.ico",
}
PUBLIC_PREFIXES = ("/static/", "/auth/",)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return await call_next(request)
    # Everything else requires a valid session.
    sid = request.cookies.get(auth.SESSION_COOKIE)
    session = auth.load_session(str(AUTH_DB_PATH), sid)
    if not session:
        # For API-style paths, return 401 JSON; for HTML pages, redirect.
        wants_html = "text/html" in (request.headers.get("accept") or "")
        if wants_html:
            return RedirectResponse(url=f"/login?next={path}")
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    return await call_next(request)


# ---------- static frontend ----------------------------------------------------------
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "landing.html")

    @app.get("/app")
    def dashboard(user: dict = Depends(get_user)):
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/login")
    def login_page():
        return FileResponse(FRONTEND_DIR / "login.html")

    @app.get("/register")
    def register_page():
        return FileResponse(FRONTEND_DIR / "register.html")
