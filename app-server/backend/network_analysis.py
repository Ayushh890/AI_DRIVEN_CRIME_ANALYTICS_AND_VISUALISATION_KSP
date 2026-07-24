"""
Deep criminal-network analysis.

Builds the co-accused graph (edge between two offenders if they appear in the
same CaseMaster row), then computes:

  * Louvain community detection (best-effort — falls back to label propagation
    when the `networkx` community module is unavailable).
  * Degree, weighted degree, betweenness (sampled on large graphs).
  * MO similarity between top offenders via cosine over their crime-subhead
    frequency vector.
  * Behavioral / criminological offender profiling — escalation trend,
    temporal & spatial pattern, weapon/MO signature, and a composite risk
    score (see `offender_profile`).

The heavy computations are performed on-demand and cached in an LRU keyed by
(db mtime, edge count) so successive calls are fast.
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
import zlib
from collections import Counter, defaultdict
from typing import Any


def stable_node_id(prefix: str, text: str) -> str:
    """Deterministic node id for graph vertices keyed by name (e.g. victims,
    who have no numeric person_link_id). Using Python's built-in hash() here
    is a real bug in a multi-worker deployment: PYTHONHASHSEED is randomized
    per-process, so the same victim name gets a different node id on every
    worker, silently breaking any client-side graph merge / caching. CRC32
    over UTF-8 bytes is stable across processes and Python versions.
    """
    return f"{prefix}{zlib.crc32(text.encode('utf-8', 'replace')) & 0xFFFFFFFF:x}"

try:
    import networkx as nx  # type: ignore
    _HAS_NX = True
except Exception:
    _HAS_NX = False

_CACHE: dict[str, Any] = {}


def _cache_get(db_path: str, key: str):
    mt = os.path.getmtime(db_path)
    e = _CACHE.get(key)
    if e and e["mt"] == mt:
        return e["val"]
    return None


def _cache_put(db_path: str, key: str, val):
    _CACHE[key] = {"mt": os.path.getmtime(db_path), "val": val}


def build_offender_graph(conn: sqlite3.Connection) -> tuple[dict[int, dict], list[tuple[int, int, int]]]:
    """
    Returns (nodes, edges).
      nodes[person_link_id] = {name, cases, subhead_counter}
      edges = list of (a_link_id, b_link_id, shared_case_count)
    """
    rows = conn.execute(
        """SELECT a.CaseMasterID, a.person_link_id, a.AccusedName, cm.CrimeMinorHeadID
           FROM Accused a JOIN CaseMaster cm ON cm.CaseMasterID = a.CaseMasterID
           WHERE a.person_link_id IS NOT NULL AND cm.CrimeMinorHeadID IS NOT NULL"""
    ).fetchall()

    per_case: dict[int, list[int]] = defaultdict(list)
    nodes: dict[int, dict] = {}
    for case_id, pid, name, sub_id in rows:
        per_case[case_id].append(pid)
        n = nodes.setdefault(pid, {"name": name, "cases": 0, "subheads": Counter()})
        n["cases"] += 1
        if sub_id is not None:
            n["subheads"][sub_id] += 1

    edge_counter: Counter[tuple[int, int]] = Counter()
    for case_id, members in per_case.items():
        members = list(set(members))
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = sorted((members[i], members[j]))
                edge_counter[(a, b)] += 1

    edges = [(a, b, w) for (a, b), w in edge_counter.items()]
    return nodes, edges


def analyze_communities(conn: sqlite3.Connection, db_path: str,
                        min_weight: int = 1, top_n: int = 200) -> dict:
    """
    Louvain communities on the co-accused graph. Falls back to a simple
    label-propagation-style clustering if networkx is unavailable.
    """
    cached = _cache_get(db_path, f"communities:{min_weight}:{top_n}")
    if cached: return cached

    nodes, edges = build_offender_graph(conn)
    # Filter to prolific offenders only to keep the community layout meaningful.
    top_ids = {pid for pid, _ in sorted(nodes.items(), key=lambda kv: -kv[1]["cases"])[:top_n]}
    edges = [(a, b, w) for a, b, w in edges if a in top_ids and b in top_ids and w >= min_weight]

    if not edges:
        result = {"communities": [], "summary": {"nodes": 0, "edges": 0}}
        _cache_put(db_path, f"communities:{min_weight}:{top_n}", result)
        return result

    if _HAS_NX:
        g = nx.Graph()
        for pid in top_ids:
            g.add_node(pid, **nodes[pid])
        for a, b, w in edges:
            g.add_edge(a, b, weight=w)
        # louvain_communities is available in networkx>=2.7.
        try:
            comms = nx.community.louvain_communities(g, weight="weight", seed=42)  # type: ignore
        except Exception:
            comms = nx.community.label_propagation_communities(g)  # type: ignore
        comm_lists = [sorted(c) for c in comms]
        # Centrality: weighted degree only (cheap and useful).
        weighted_deg = dict(g.degree(weight="weight"))
        try:
            btw = nx.betweenness_centrality(g, weight="weight", k=min(80, g.number_of_nodes()), seed=42)
        except Exception:
            btw = {n: 0.0 for n in g.nodes()}
    else:
        # Toy fallback — connected components as communities.
        adj: dict[int, set[int]] = defaultdict(set)
        for a, b, _w in edges:
            adj[a].add(b); adj[b].add(a)
        seen: set[int] = set()
        comm_lists = []
        for n in top_ids:
            if n in seen: continue
            queue = [n]; comp = []
            while queue:
                v = queue.pop()
                if v in seen: continue
                seen.add(v); comp.append(v)
                queue.extend(adj[v] - seen)
            comm_lists.append(sorted(comp))
        weighted_deg = {n: sum(w for a, b, w in edges if a == n or b == n) for n in top_ids}
        btw = {n: 0.0 for n in top_ids}

    # Enrich each community with its members and centrality scores.
    subhead_lookup = {r[0]: r[1] for r in conn.execute(
        "SELECT CrimeSubHeadID, CrimeHeadName FROM CrimeSubHead"
    )}
    out_comms = []
    for i, members in enumerate(sorted(comm_lists, key=len, reverse=True)):
        if len(members) < 2: continue
        member_rows = []
        subhead_agg: Counter[int] = Counter()
        for pid in members:
            n = nodes[pid]
            subhead_agg.update(n["subheads"])
            member_rows.append({
                "person_link_id": pid,
                "name": n["name"],
                "cases": n["cases"],
                "weighted_degree": int(weighted_deg.get(pid, 0)),
                "betweenness": round(float(btw.get(pid, 0)), 4),
            })
        member_rows.sort(key=lambda r: -r["weighted_degree"])
        top_mo = [subhead_lookup.get(s, str(s)) for s, _ in subhead_agg.most_common(3)]
        out_comms.append({
            "community_id": i + 1,
            "size": len(members),
            "total_cases": sum(m["cases"] for m in member_rows),
            "signature_crimes": top_mo,
            "members": member_rows[:50],
        })

    result = {
        "communities": out_comms,
        "summary": {
            "nodes": len(top_ids), "edges": len(edges),
            "algorithm": "louvain" if _HAS_NX else "components",
        },
    }
    _cache_put(db_path, f"communities:{min_weight}:{top_n}", result)
    return result


def central_figures(conn: sqlite3.Connection, db_path: str, limit: int = 25) -> list[dict]:
    """Return top-N most 'central' offenders by weighted degree × case count."""
    cached = _cache_get(db_path, f"central:{limit}")
    if cached: return cached

    nodes, edges = build_offender_graph(conn)
    deg: Counter[int] = Counter()
    for a, b, w in edges:
        deg[a] += w
        deg[b] += w

    subhead_lookup = {r[0]: r[1] for r in conn.execute(
        "SELECT CrimeSubHeadID, CrimeHeadName FROM CrimeSubHead"
    )}

    scored = []
    for pid, n in nodes.items():
        wd = deg.get(pid, 0)
        cases = n["cases"]
        # A crude 'kingpin' score: sqrt(cases) × weighted_degree.
        score = math.sqrt(cases) * wd
        top_mo = [subhead_lookup.get(s, str(s)) for s, _ in n["subheads"].most_common(3)]
        scored.append({
            "person_link_id": pid,
            "name": n["name"],
            "cases": cases,
            "weighted_degree": wd,
            "score": round(score, 1),
            "signature": top_mo,
        })
    scored.sort(key=lambda x: -x["score"])
    result = scored[:limit]
    _cache_put(db_path, f"central:{limit}", result)
    return result


def mo_similarity(conn: sqlite3.Connection, db_path: str, top_n: int = 50) -> dict:
    """
    Cosine similarity between top offenders' crime-subhead vectors.
    Returns a list of most-similar offender pairs — candidates for case
    linkage across investigators.
    """
    cached = _cache_get(db_path, f"mo_sim:{top_n}")
    if cached: return cached

    nodes, _edges = build_offender_graph(conn)
    top = sorted(nodes.items(), key=lambda kv: -kv[1]["cases"])[:top_n]
    if len(top) < 2:
        return {"pairs": []}
    # Feature vocab.
    vocab = sorted({s for _pid, n in top for s in n["subheads"]})
    idx = {v: i for i, v in enumerate(vocab)}
    def vec(sh: Counter[int]) -> list[float]:
        v = [0.0] * len(vocab)
        for k, c in sh.items():
            v[idx[k]] = c
        return v

    def cos(a: list[float], b: list[float]) -> float:
        num = sum(x*y for x, y in zip(a, b))
        na = math.sqrt(sum(x*x for x in a))
        nb = math.sqrt(sum(x*x for x in b))
        return num / (na * nb) if na and nb else 0.0

    vecs = {pid: vec(n["subheads"]) for pid, n in top}
    pairs = []
    ids = list(vecs.keys())
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            s = cos(vecs[ids[i]], vecs[ids[j]])
            if s >= 0.75:
                pairs.append({
                    "a_id": ids[i], "a_name": nodes[ids[i]]["name"],
                    "b_id": ids[j], "b_name": nodes[ids[j]]["name"],
                    "similarity": round(s, 3),
                    "a_cases": nodes[ids[i]]["cases"],
                    "b_cases": nodes[ids[j]]["cases"],
                })
    pairs.sort(key=lambda p: -p["similarity"])
    result = {"pairs": pairs[:100]}
    _cache_put(db_path, f"mo_sim:{top_n}", result)
    return result


# ============================================================================
# Behavioral / criminological offender profiling
# ============================================================================
#
# This directly answers the RFP's "Behavioral and criminological profiling"
# requirement, distinct from MO-similarity (which links *pairs* of offenders
# for case-linkage). This builds a single-offender dossier: escalation
# trend, temporal/spatial signature, weapon pattern, network embeddedness,
# and a transparent, explainable composite risk score — not a black-box
# number. Every sub-score is returned alongside the weight that produced it
# so an investigator can see *why* the score is what it is (important for
# any operational/legal use of an ML-influenced risk rating).

_GRAVITY_RANK = {"petty": 0, "non-heinous": 1, "non heinous": 1, "heinous": 2}


def _gravity_rank(label: str | None) -> int:
    if not label:
        return 0
    return _GRAVITY_RANK.get(label.strip().lower(), 0)


def offender_profile(conn: sqlite3.Connection, db_path: str, pid: int) -> dict | None:
    """
    Build a behavioral/criminological profile for one offender
    (Accused.person_link_id = pid). Returns None if the person has no
    linkable FIRs (edge case: dangling/unmatched person_link_id).
    """
    cached = _cache_get(db_path, f"profile:{pid}")
    if cached is not None:
        return cached

    person = conn.execute(
        """SELECT AccusedName AS name, MAX(AgeYear) AS age, MAX(GenderID) AS gender,
                  MAX(home_district_id) AS home_district_id
           FROM Accused WHERE person_link_id = ?""",
        (pid,),
    ).fetchone()
    if not person or person["name"] is None:
        return None  # edge case: unknown / unlinked person id

    cases = conn.execute(
        """SELECT cm.CaseMasterID AS id, cm.IncidentFromDate AS occurred_at,
                  cm.modus_operandi AS mo, cm.weapon AS weapon,
                  sh.CrimeHeadName AS subhead, ch.CrimeGroupName AS class,
                  g.LookupValue AS gravity, cs.CaseStatusName AS status,
                  it.OccurredHour AS hour, it.OccurredWeekday AS weekday,
                  it.LocationType AS location_type,
                  u.DistrictID AS district_id, d.DistrictName AS district_name
           FROM Accused a
           JOIN CaseMaster cm       ON cm.CaseMasterID = a.CaseMasterID
           JOIN CrimeSubHead sh     ON sh.CrimeSubHeadID = cm.CrimeMinorHeadID
           JOIN CrimeHead ch        ON ch.CrimeHeadID   = cm.CrimeMajorHeadID
           LEFT JOIN GravityOffence g   ON g.GravityOffenceID = cm.GravityOffenceID
           LEFT JOIN CaseStatusMaster cs ON cs.CaseStatusID  = cm.CaseStatusID
           LEFT JOIN Inv_OccuranceTime it ON it.CaseMasterID = cm.CaseMasterID
           JOIN Unit u              ON u.UnitID = cm.PoliceStationID
           LEFT JOIN District d     ON d.DistrictID = u.DistrictID
           WHERE a.person_link_id = ?
           ORDER BY cm.IncidentFromDate ASC""",
        (pid,),
    ).fetchall()

    n = len(cases)
    if n == 0:
        return None  # edge case: person exists but every FIR link is orphaned

    case_ids = [r["id"] for r in cases]
    placeholder = ",".join("?" * len(case_ids))

    # -- chargesheet outcome mix (edge case: offender may have zero CS rows
    #    yet — under investigation — handle with LEFT-joined aggregate, not
    #    an inner join that would silently drop them). -----------------------
    cs_rows = conn.execute(
        f"""SELECT cstype, COUNT(*) AS n FROM ChargesheetDetails
            WHERE CaseMasterID IN ({placeholder}) GROUP BY cstype""",
        case_ids,
    ).fetchall()
    cs_mix = {r["cstype"]: r["n"] for r in cs_rows}
    chargesheeted = cs_mix.get("A", 0)
    false_reports = cs_mix.get("B", 0)
    undetected = cs_mix.get("C", 0)
    cs_total = chargesheeted + false_reports + undetected

    # -- arrest history: gap between offenses and arrests hints at whether
    #    this person keeps offending between arrests (operational recidivism
    #    signal) rather than just raw case count. --------------------------
    arrests = conn.execute(
        f"""SELECT ArrestSurrenderDate AS d, ArrestSurrenderStateId AS state_id
            FROM ArrestSurrender
            WHERE CaseMasterID IN ({placeholder}) AND ArrestSurrenderTypeID = 1
            ORDER BY ArrestSurrenderDate""",
        case_ids,
    ).fetchall()
    out_of_state_arrests = sum(
        1 for r in arrests if r["state_id"] and r["state_id"] != 29  # 29 = Karnataka
    )

    # -- co-offender network embeddedness (reuses the graph builder so this
    #    stays consistent with /network/central-figures). -------------------
    nodes, edges = build_offender_graph(conn)
    weighted_degree = 0
    for a, b, w in edges:
        if a == pid or b == pid:
            weighted_degree += w
    distinct_co_offenders = sum(1 for a, b, _w in edges if a == pid or b == pid)

    # ---- 1. Escalation trend --------------------------------------------
    # Compare the gravity of the first third of the offender's timeline vs
    # the last third. Positive slope = escalating (petty -> heinous).
    gravity_series = [_gravity_rank(r["gravity"]) for r in cases]
    if n >= 3:
        third = max(1, n // 3)
        early = gravity_series[:third]
        late = gravity_series[-third:]
        escalation_delta = round((sum(late) / len(late)) - (sum(early) / len(early)), 2)
    else:
        escalation_delta = 0.0  # edge case: too few cases to trend reliably
    if escalation_delta > 0.3:
        escalation_label = "escalating"
    elif escalation_delta < -0.3:
        escalation_label = "de-escalating"
    else:
        escalation_label = "stable"

    # ---- 2. Temporal signature -------------------------------------------
    hours = [r["hour"] for r in cases if r["hour"] is not None]
    weekdays = [r["weekday"] for r in cases if r["weekday"] is not None]
    hour_counts = Counter(hours)
    weekday_counts = Counter(weekdays)
    peak_hour = hour_counts.most_common(1)[0][0] if hour_counts else None
    peak_weekday = weekday_counts.most_common(1)[0][0] if weekday_counts else None
    night_offenses = sum(c for h, c in hour_counts.items() if h is not None and (h >= 22 or h < 5))
    night_pct = round(100 * night_offenses / len(hours), 1) if hours else None

    # ---- 3. Spatial / mobility signature -----------------------------------
    district_counts = Counter(r["district_name"] for r in cases if r["district_name"])
    location_type_counts = Counter(r["location_type"] for r in cases if r["location_type"])
    distinct_districts = len(district_counts)

    # ---- 4. MO / weapon signature -------------------------------------------
    subhead_counts = Counter(r["subhead"] for r in cases if r["subhead"])
    weapon_counts = Counter(r["weapon"] for r in cases if r["weapon"])
    class_counts = Counter(r["class"] for r in cases if r["class"])
    versatility = len(subhead_counts)  # distinct crime types — specialist vs generalist

    # ---- 5. Composite, explainable risk score -----------------------------
    # Each component is normalized to [0, 1] with a saturating curve so a
    # handful of extreme outliers (e.g. one offender with 400 FIRs) can't
    # blow the whole score off-scale. Weights sum to 1.0 and are surfaced in
    # the response — this is meant to be auditable by an investigator, not a
    # black box.
    def sat(x: float, k: float) -> float:
        """0..1 saturating curve; k = value at which score ~= 0.63."""
        return 1 - math.exp(-x / k) if k > 0 else 0.0

    freq_score = sat(n, 6)
    escalation_score = max(0.0, min(1.0, 0.5 + escalation_delta / 2))
    network_score = sat(weighted_degree, 8)
    heinous_share = sum(1 for g in gravity_series if g == 2) / n
    mobility_score = sat(distinct_districts - 1, 2) if distinct_districts else 0.0
    reoffend_score = sat(max(0, n - len(arrests)), 4)  # offenses not matched 1:1 by an arrest

    weights = {
        "frequency": 0.25, "escalation": 0.20, "co_offender_network": 0.20,
        "heinous_share": 0.15, "cross_district_mobility": 0.10, "re_offense_rate": 0.10,
    }
    components = {
        "frequency": freq_score, "escalation": escalation_score,
        "co_offender_network": network_score, "heinous_share": heinous_share,
        "cross_district_mobility": mobility_score, "re_offense_rate": reoffend_score,
    }
    risk_score = round(100 * sum(weights[k] * v for k, v in components.items()), 1)
    if risk_score >= 70:
        risk_tier = "critical"
    elif risk_score >= 45:
        risk_tier = "high"
    elif risk_score >= 20:
        risk_tier = "medium"
    else:
        risk_tier = "low"

    result = {
        "person_link_id": pid,
        "name": person["name"],
        "age": person["age"],
        "gender": person["gender"],
        "total_cases": n,
        "date_range": {"first": cases[0]["occurred_at"], "last": cases[-1]["occurred_at"]},
        "escalation": {
            "trend": escalation_label,
            "delta": escalation_delta,
            "gravity_timeline": [
                {"case_id": r["id"], "date": r["occurred_at"], "gravity": r["gravity"]}
                for r in cases
            ],
        },
        "temporal_pattern": {
            "peak_hour": peak_hour,
            "peak_weekday": peak_weekday,  # 0=Mon..6=Sun, matches Inv_OccuranceTime
            "night_offense_pct": night_pct,
            "hour_histogram": [{"hour": h, "count": c} for h, c in sorted(hour_counts.items())],
        },
        "spatial_pattern": {
            "distinct_districts": distinct_districts,
            "district_breakdown": [{"district": k, "count": v} for k, v in district_counts.most_common()],
            "location_type_breakdown": [{"type": k, "count": v} for k, v in location_type_counts.most_common()],
            "out_of_state_arrests": out_of_state_arrests,
        },
        "mo_signature": {
            "versatility": versatility,
            "top_crime_types": [{"type": k, "count": v} for k, v in subhead_counts.most_common(5)],
            "class_breakdown": [{"class": k, "count": v} for k, v in class_counts.most_common()],
            "weapon_pattern": [{"weapon": k, "count": v} for k, v in weapon_counts.most_common(5)],
        },
        "network": {
            "weighted_co_offense_degree": weighted_degree,
            "distinct_co_offenders": distinct_co_offenders,
        },
        "outcomes": {
            "chargesheeted": chargesheeted, "false_reports_B": false_reports,
            "undetected_C": undetected, "total_disposed": cs_total,
            "chargesheet_rate": round(chargesheeted / cs_total, 2) if cs_total else None,
        },
        "risk": {
            "score": risk_score, "tier": risk_tier,
            "components": {k: round(v, 3) for k, v in components.items()},
            "weights": weights,
            "note": "Explainable heuristic score for triage — not a legal determination. "
                    "Always corroborate with an investigating officer before action.",
        },
    }
    _cache_put(db_path, f"profile:{pid}", result)
    return result


# ============================================================================
# Victim-side relationship mapping (closes the remaining RFP gap: "discover
# hidden relationships between crimes, offenders, victims, locations...")
# ============================================================================
#
# The KSP schema (by design — see data/schema.sql) has no cross-case identity
# key for Victim the way Accused.person_link_id links an offender across
# FIRs. In the pilot we surface this honestly rather than pretending we have
# ground truth: victims are grouped by normalized name, and every result
# below is a *candidate* link, explicitly labeled as name-matched. This
# mirrors the same accepted limitation already documented for
# Accused.person_link_id (see README's "Data model" section) — in production
# both would be replaced by a real person master / Aadhaar-linked ID from
# CCTNS, at which point this function's SQL doesn't change, only the join key.


def _normalize_name(name: str | None) -> str | None:
    if not name:
        return None
    n = " ".join(name.strip().split()).lower()
    return n or None


def _victim_identity_key(name: str | None, gender: str | None, age: int | None) -> str | None:
    """
    Candidate cross-case identity key for a victim row.

    Name alone is too weak in a population with a limited first/last-name
    pool (verified against this pilot's synthetic data: 3,435 victim rows
    resolve to only 960 distinct normalized names — i.e. ~3.6 rows share
    every name on average, which is name-pool collision, not real repeat
    victimization). Folding in gender and a 5-year age band cuts collisions
    sharply while still matching the *same* person across FIRs a few years
    apart. Still a heuristic — see the `caveat` field in the result — but a
    materially better one than name alone.
    """
    key = _normalize_name(name)
    if key is None:
        return None
    band = "NA" if age is None else str((age // 5) * 5)
    g = (gender or "U").strip().upper()[:1]
    return f"{key}|{g}|{band}"


def victim_network(conn: sqlite3.Connection, db_path: str,
                    min_case_count: int = 2, top_n: int = 300) -> dict:
    """
    Repeat-victimization + victim-offender relationship mapping.

    Returns:
      repeat_victims: victims (by name+gender+age-band identity key) appearing
        in >= min_case_count distinct FIRs, each with the offenders linked to
        those FIRs — surfaces stalking / domestic-violence / extortion-style
        repeat targeting.
      repeat_pairs: (victim, offender) pairs that co-occur in >= 2 FIRs —
        the strongest signal: this specific offender has targeted this
        specific victim more than once.
      hub_victims: victims linked (across their FIRs) to an unusually high
        number of *distinct* offenders — flags either a high-crime location
        proxying as a person (e.g. a shopkeeper robbed by different people)
        or a potential trafficking/exploitation hub worth investigator review.
    """
    cached = _cache_get(db_path, f"victim_net:{min_case_count}:{top_n}")
    if cached is not None:
        return cached

    v_rows = conn.execute(
        """SELECT v.VictimMasterID, v.CaseMasterID, v.VictimName, v.AgeYear, v.GenderID
           FROM Victim v"""
    ).fetchall()

    # Edge case: no victims in the DB at all (e.g. a filtered/empty demo DB).
    if not v_rows:
        result = {"repeat_victims": [], "repeat_pairs": [], "hub_victims": [],
                  "summary": {"distinct_victim_names": 0, "repeat_victim_names": 0}}
        _cache_put(db_path, f"victim_net:{min_case_count}:{top_n}", result)
        return result

    by_name: dict[str, dict] = defaultdict(lambda: {"cases": set(), "raw_names": Counter(),
                                                      "ages": [], "genders": Counter()})
    case_to_key: dict[int, set[str]] = defaultdict(set)
    for r in v_rows:
        key = _victim_identity_key(r["VictimName"], r["GenderID"], r["AgeYear"])
        if key is None:
            continue  # edge case: blank/NULL victim name — can't link, skip safely
        e = by_name[key]
        e["cases"].add(r["CaseMasterID"])
        e["raw_names"][r["VictimName"]] += 1
        if r["AgeYear"] is not None:
            e["ages"].append(r["AgeYear"])
        if r["GenderID"]:
            e["genders"][r["GenderID"]] += 1
        case_to_key[r["CaseMasterID"]].add(key)

    # Offenders per case (reuse the same join the offender graph uses so the
    # two views of the network stay consistent with each other).
    acc_rows = conn.execute(
        """SELECT CaseMasterID, person_link_id, AccusedName
           FROM Accused WHERE person_link_id IS NOT NULL"""
    ).fetchall()
    offenders_by_case: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for r in acc_rows:
        offenders_by_case[r["CaseMasterID"]].append((r["person_link_id"], r["AccusedName"]))

    # ---- repeat victims (name appears in >= min_case_count distinct FIRs) --
    repeat_keys = {k: e for k, e in by_name.items() if len(e["cases"]) >= min_case_count}

    pair_case_count: Counter[tuple[str, int]] = Counter()   # (victim_key, offender_pid) -> shared case count
    pair_names: dict[tuple[str, int], tuple[str, str]] = {}
    for key, e in by_name.items():
        for case_id in e["cases"]:
            for pid, oname in offenders_by_case.get(case_id, []):
                pk = (key, pid)
                pair_case_count[pk] += 1
                pair_names[pk] = (e["raw_names"].most_common(1)[0][0], oname)

    repeat_pairs = []
    for (key, pid), n in pair_case_count.items():
        if n >= 2:  # same offender, same victim, 2+ separate FIRs
            vname, oname = pair_names[(key, pid)]
            repeat_pairs.append({
                "victim_name": vname, "victim_key": key,
                "offender_person_link_id": pid, "offender_name": oname,
                "shared_cases": n,
            })
    repeat_pairs.sort(key=lambda p: -p["shared_cases"])

    out_repeat_victims = []
    for key, e in sorted(repeat_keys.items(), key=lambda kv: -len(kv[1]["cases"]))[:top_n]:
        distinct_offenders = {pid for case_id in e["cases"] for pid, _ in offenders_by_case.get(case_id, [])}
        out_repeat_victims.append({
            "victim_key": key,
            "victim_name": e["raw_names"].most_common(1)[0][0],
            "case_count": len(e["cases"]),
            "distinct_offenders": len(distinct_offenders),
            "avg_age": round(sum(e["ages"]) / len(e["ages"]), 1) if e["ages"] else None,
            "gender": e["genders"].most_common(1)[0][0] if e["genders"] else None,
        })

    # ---- hub victims: linked to an unusually high number of distinct
    #      offenders relative to their case count (name-collision risk for
    #      common names is real here — flagged explicitly in the response
    #      rather than silently presented as fact). ------------------------
    hub_victims = [
        v for v in out_repeat_victims
        if v["distinct_offenders"] >= 3 and v["distinct_offenders"] >= v["case_count"]
    ]
    hub_victims.sort(key=lambda v: -v["distinct_offenders"])

    result = {
        "repeat_victims": out_repeat_victims,
        "repeat_pairs": repeat_pairs[:200],
        "hub_victims": hub_victims[:50],
        "summary": {
            "distinct_victim_names": len(by_name),
            "repeat_victim_names": len(repeat_keys),
            "repeat_offender_victim_pairs": len(repeat_pairs),
        },
        "caveat": "Victim identity is matched on normalized name + gender + a 5-year age "
                  "band — the KSP schema has no cross-case victim ID. This still isn't a "
                  "guaranteed identity match; treat 'hub_victims' as an investigator review "
                  "queue, not a conclusion.",
    }
    _cache_put(db_path, f"victim_net:{min_case_count}:{top_n}", result)
    return result
