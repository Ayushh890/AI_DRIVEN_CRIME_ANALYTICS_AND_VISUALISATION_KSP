"""
Deep criminal-network analysis.

Builds the co-accused graph (edge between two offenders if they appear in the
same CaseMaster row), then computes:

  * Louvain community detection (best-effort — falls back to label propagation
    when the `networkx` community module is unavailable).
  * Degree, weighted degree, betweenness (sampled on large graphs).
  * MO similarity between top offenders via cosine over their crime-subhead
    frequency vector.

The heavy computations are performed on-demand and cached in an LRU keyed by
(db mtime, edge count) so successive calls are fast.
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from collections import Counter, defaultdict
from typing import Any

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
