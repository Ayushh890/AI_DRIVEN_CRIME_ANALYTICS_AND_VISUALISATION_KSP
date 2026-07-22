"""
Schema-grounded LLM assistant for the KSP CIP.

Natural-language → SQLite. Every LLM call is prompted with:
    1. A tightly-worded system role.
    2. The complete KSP FIR schema summary.
    3. A DATA-DERIVED grounding block (top districts, top sub-heads, sample rows)
       that keeps the model's answers aligned with what actually exists in the DB.
    4. A curated few-shot set of question / SQL pairs (used both for grounding
       here AND as the fine-tune training set for scripts/train_llm.py).

Backends (pick via env var KSP_LLM_BACKEND, falling back automatically):
    * ``groq``       – api.groq.com/openai/v1 (recommended, free tier, Llama-3.3 70B)
    * ``openai``     – OpenAI-compatible endpoint (vLLM, LM Studio, OpenAI, Cerebras…)
    * ``ollama``     – http://localhost:11434
    * ``hf``         – HuggingFace router (router.huggingface.co)
    * ``offline``    – rule-based intent templates. Always available; used
                        automatically when no ONLINE credentials are configured.

Env vars respected:
    KSP_LLM_BACKEND    override auto-detection
    KSP_LLM_MODEL      model id
    GROQ_API_KEY / OPENAI_API_KEY / HF_TOKEN
    OPENAI_BASE_URL    for openai backend
    OLLAMA_HOST        for ollama backend

Every query is validated by a safety layer:
    * SELECT / WITH only
    * Single statement
    * Table allowlist (KSP schema tables)
    * Forbidden keyword regex (INSERT/UPDATE/DELETE/DROP/PRAGMA/…)
    * Automatic LIMIT injection
"""
from __future__ import annotations

import functools
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

try:
    import httpx  # type: ignore
except Exception:
    httpx = None  # noqa: N816


# --------------------------------------------------------------------------
# Schema summary and curated Q/SQL few-shots
# --------------------------------------------------------------------------
SCHEMA_SUMMARY = """\
KSP Police FIR System — SQLite schema (case-sensitive column names).

CaseMaster(CaseMasterID PK, CrimeNo, CaseNo, CrimeRegisteredDate,
  PolicePersonID→Employee.EmployeeID, PoliceStationID→Unit.UnitID,
  CaseCategoryID→CaseCategory, GravityOffenceID→GravityOffence,
  CrimeMajorHeadID→CrimeHead.CrimeHeadID,
  CrimeMinorHeadID→CrimeSubHead.CrimeSubHeadID,
  CaseStatusID→CaseStatusMaster, CourtID→Court,
  IncidentFromDate, IncidentToDate, InfoReceivedPSDate,
  latitude, longitude, BriefFacts, modus_operandi, weapon)

Victim(VictimMasterID PK, CaseMasterID, VictimName, AgeYear, GenderID, VictimPolice)
Accused(AccusedMasterID PK, CaseMasterID, AccusedName, AgeYear, GenderID,
  PersonID (A1/A2/…), person_link_id, is_repeat, home_district_id→District)
ComplainantDetails(ComplainantID PK, CaseMasterID, ComplainantName, AgeYear,
  OccupationID→OccupationMaster, ReligionID→ReligionMaster,
  CasteID→CasteMaster.caste_master_id, GenderID)
ArrestSurrender(ArrestSurrenderID PK, CaseMasterID,
  ArrestSurrenderTypeID (1=Arrest, 2=Surrender), ArrestSurrenderDate,
  ArrestSurrenderStateId→State.StateID, ArrestSurrenderDistrictId→District,
  PoliceStationID→Unit, IOID→Employee, CourtID→Court,
  AccusedMasterID→Accused)
ChargesheetDetails(CSID PK, CaseMasterID, csdate, cstype
  (A=Chargesheet / B=False / C=Undetected), PolicePersonID)
ActSectionAssociation(CaseMasterID, ActID→Act.ActCode, SectionID→Section.SectionCode,
  ActOrderID, SectionOrderID)

Masters:
CrimeHead(CrimeHeadID PK, CrimeGroupName)         -- Property Crimes, Cyber Crimes, …
CrimeSubHead(CrimeSubHeadID PK, CrimeHeadID, CrimeHeadName)  -- Theft, Murder, Cyber Fraud, …
Act(ActCode PK, ActDescription, ShortName)
Section(ActCode, SectionCode, SectionDescription)
CaseCategory(CaseCategoryID PK, LookupValue, CategoryCode)  -- FIR/UDR/PAR/ZeroFIR
GravityOffence(GravityOffenceID PK, LookupValue)            -- Heinous/Non-Heinous/Petty
CaseStatusMaster(CaseStatusID PK, CaseStatusName)

Geography:
State(StateID PK, StateName)                    -- Karnataka.StateID = 29
District(DistrictID PK, DistrictName, StateID, HqLat, HqLng, Population, UrbanPct, LiteracyPct, Zone)
Unit(UnitID PK, UnitName, TypeID, ParentUnit, StateID, DistrictID, Lat, Lng) -- Police stations
UnitType(UnitTypeID PK, UnitTypeName, CityDistState, Hierarchy)
Court(CourtID PK, CourtName, DistrictID, StateID)

Personnel:
Employee(EmployeeID PK, DistrictID, UnitID, RankID→Rank, DesignationID→Designation,
  KGID, FirstName, LastName, EmployeeDOB, GenderID, BloodGroupID,
  PhysicallyChallenged, AppointmentDate)
Rank(RankID PK, RankName, Hierarchy)
Designation(DesignationID PK, DesignationName)

Person masters:
CasteMaster(caste_master_id PK, caste_master_name)
ReligionMaster(ReligionID PK, ReligionName)
OccupationMaster(OccupationID PK, OccupationName)
"""

# Curated (question, sql, explanation) triples — used both for few-shot prompting
# and as the fine-tune dataset by scripts/train_llm.py.
CURATED_QA = [
    (
        "top 15 offenders by number of cases",
        "SELECT a.person_link_id, a.AccusedName, COUNT(DISTINCT a.CaseMasterID) AS cases FROM Accused a WHERE a.person_link_id IS NOT NULL GROUP BY a.person_link_id, a.AccusedName ORDER BY cases DESC LIMIT 15",
        "Top repeat offenders by distinct case count.",
    ),
    (
        "which districts have the highest heinous crime count",
        "SELECT d.DistrictName, COUNT(*) AS heinous_cases FROM CaseMaster cm JOIN Unit u ON u.UnitID=cm.PoliceStationID JOIN District d ON d.DistrictID=u.DistrictID JOIN GravityOffence g ON g.GravityOffenceID=cm.GravityOffenceID WHERE g.LookupValue='Heinous' AND d.StateID=29 GROUP BY d.DistrictName ORDER BY heinous_cases DESC LIMIT 15",
        "Heinous case ranking by district within Karnataka.",
    ),
    (
        "how many cyber crime FIRs were registered in Bengaluru Urban in 2026",
        "SELECT COUNT(*) AS cyber_2026 FROM CaseMaster cm JOIN Unit u ON u.UnitID=cm.PoliceStationID JOIN District d ON d.DistrictID=u.DistrictID JOIN CrimeHead ch ON ch.CrimeHeadID=cm.CrimeMajorHeadID WHERE ch.CrimeGroupName='Cyber Crimes' AND d.DistrictName='Bengaluru Urban' AND strftime('%Y', cm.CrimeRegisteredDate)='2026'",
        "Cyber-crime FIR count for Bengaluru Urban in 2026.",
    ),
    (
        "arrests made outside Karnataka in the last 6 months",
        "SELECT ars.ArrestSurrenderDate, s.StateName, d.DistrictName, a.AccusedName FROM ArrestSurrender ars JOIN State s ON s.StateID=ars.ArrestSurrenderStateId LEFT JOIN District d ON d.DistrictID=ars.ArrestSurrenderDistrictId LEFT JOIN Accused a ON a.AccusedMasterID=ars.AccusedMasterID WHERE ars.ArrestSurrenderStateId!=29 AND ars.ArrestSurrenderDate >= date('now','-6 months') ORDER BY ars.ArrestSurrenderDate DESC LIMIT 100",
        "Recent cross-border arrests.",
    ),
    (
        "police station chargesheet rate above 60%",
        "SELECT u.UnitName, COUNT(cm.CaseMasterID) AS total, SUM(CASE WHEN cs.cstype='A' THEN 1 ELSE 0 END) AS chargesheeted, ROUND(100.0*SUM(CASE WHEN cs.cstype='A' THEN 1 ELSE 0 END)/COUNT(cm.CaseMasterID),1) AS pct FROM CaseMaster cm JOIN Unit u ON u.UnitID=cm.PoliceStationID LEFT JOIN ChargesheetDetails cs ON cs.CaseMasterID=cm.CaseMasterID GROUP BY u.UnitID HAVING total>=50 AND pct>=60 ORDER BY pct DESC LIMIT 40",
        "Stations with chargesheet rate ≥ 60% (min 50 cases).",
    ),
    (
        "average age of accused for narcotic offences",
        "SELECT ROUND(AVG(a.AgeYear),1) AS avg_age FROM Accused a JOIN CaseMaster cm ON cm.CaseMasterID=a.CaseMasterID JOIN CrimeHead ch ON ch.CrimeHeadID=cm.CrimeMajorHeadID WHERE ch.CrimeGroupName='Narcotic Offences'",
        "Average accused age for narcotic offences.",
    ),
    (
        "which crime sub-heads spiked in the last 90 days",
        "SELECT sh.CrimeHeadName, SUM(CASE WHEN cm.CrimeRegisteredDate >= date('now','-90 days') THEN 1 ELSE 0 END) AS recent, SUM(CASE WHEN cm.CrimeRegisteredDate < date('now','-90 days') AND cm.CrimeRegisteredDate >= date('now','-180 days') THEN 1 ELSE 0 END) AS prior FROM CaseMaster cm JOIN CrimeSubHead sh ON sh.CrimeSubHeadID=cm.CrimeMinorHeadID GROUP BY sh.CrimeHeadName HAVING recent > prior ORDER BY (recent - prior) DESC LIMIT 20",
        "Sub-heads that grew in the last 90 days vs prior 90.",
    ),
    (
        "investigating officers with the highest caseload",
        "SELECT e.EmployeeID, e.FirstName || ' ' || e.LastName AS io_name, u.UnitName, COUNT(cm.CaseMasterID) AS caseload FROM Employee e JOIN CaseMaster cm ON cm.PolicePersonID=e.EmployeeID JOIN Unit u ON u.UnitID=e.UnitID JOIN Designation d ON d.DesignationID=e.DesignationID WHERE d.DesignationName='Investigating Officer' GROUP BY e.EmployeeID ORDER BY caseload DESC LIMIT 30",
        "Investigating officer caseload leaderboard.",
    ),
    (
        "act section usage frequency",
        "SELECT asa.ActID, asa.SectionID, COUNT(*) AS usages FROM ActSectionAssociation asa GROUP BY asa.ActID, asa.SectionID ORDER BY usages DESC LIMIT 30",
        "Most-cited (act, section) pairs across FIRs.",
    ),
    (
        "cases where the victim is a police officer",
        "SELECT cm.CrimeNo, cm.CrimeRegisteredDate, sh.CrimeHeadName, u.UnitName, v.VictimName FROM Victim v JOIN CaseMaster cm ON cm.CaseMasterID=v.CaseMasterID JOIN CrimeSubHead sh ON sh.CrimeSubHeadID=cm.CrimeMinorHeadID JOIN Unit u ON u.UnitID=cm.PoliceStationID WHERE v.VictimPolice=1 ORDER BY cm.CrimeRegisteredDate DESC LIMIT 50",
        "Cases with a police-officer victim.",
    ),
    (
        "offenders arrested in more than 3 different states",
        "SELECT a.AccusedName, COUNT(DISTINCT ars.ArrestSurrenderStateId) AS states, COUNT(*) AS arrests FROM Accused a JOIN ArrestSurrender ars ON ars.AccusedMasterID=a.AccusedMasterID GROUP BY a.person_link_id, a.AccusedName HAVING states > 3 ORDER BY states DESC, arrests DESC LIMIT 30",
        "Offenders arrested across more than 3 states.",
    ),
    (
        "count of FIRs by crime class",
        "SELECT ch.CrimeGroupName, COUNT(*) AS cases FROM CaseMaster cm JOIN CrimeHead ch ON ch.CrimeHeadID=cm.CrimeMajorHeadID GROUP BY ch.CrimeGroupName ORDER BY cases DESC",
        "Totals by crime class.",
    ),
    (
        "monthly cyber crime trend for the last 12 months",
        "SELECT strftime('%Y-%m', cm.CrimeRegisteredDate) AS month, COUNT(*) AS cases FROM CaseMaster cm JOIN CrimeHead ch ON ch.CrimeHeadID=cm.CrimeMajorHeadID WHERE ch.CrimeGroupName='Cyber Crimes' AND cm.CrimeRegisteredDate >= date('now','-12 months') GROUP BY month ORDER BY month",
        "Monthly cyber-crime volume for the last year.",
    ),
    (
        "which police station registered the most FIRs this year",
        "SELECT u.UnitName, d.DistrictName, COUNT(*) AS firs FROM CaseMaster cm JOIN Unit u ON u.UnitID=cm.PoliceStationID JOIN District d ON d.DistrictID=u.DistrictID WHERE strftime('%Y', cm.CrimeRegisteredDate)=strftime('%Y', date('now')) GROUP BY u.UnitID ORDER BY firs DESC LIMIT 25",
        "This year's FIR volume by police station.",
    ),
]

SYSTEM_PROMPT_HEADER = """You are the SQL analyst for the Karnataka Police State Crime Records Bureau (SCRB).
You answer analytical questions by writing SQLite queries against the KSP FIR schema.

STRICT RULES
============
* Emit ONE valid SQLite SELECT (or WITH … SELECT) statement.
* Never modify data (no INSERT / UPDATE / DELETE / DROP / ALTER / CREATE / PRAGMA / ATTACH / VACUUM).
* Use only tables from the schema below. Column names ARE case-sensitive.
* Karnataka's StateID is 29 — use it when the user says "Karnataka" or "state".
* Prefer JOINs over sub-queries. Always add a sensible LIMIT.
* Respond with STRICT JSON only:  {"sql": "<query>", "explanation": "<one line>"}
  No prose outside the JSON. No markdown fences.
"""


# --------------------------------------------------------------------------
# Data-derived grounding (auto-refreshed every 10 minutes)
# --------------------------------------------------------------------------
_DATA_CACHE: dict[str, Any] = {"ts": 0, "text": ""}


def _grounding(conn: sqlite3.Connection) -> str:
    """Small factual grounding block — top districts, sub-heads, sample rows."""
    now = time.time()
    if now - _DATA_CACHE["ts"] < 600 and _DATA_CACHE["text"]:
        return _DATA_CACHE["text"]
    try:
        top_districts = conn.execute(
            "SELECT DistrictName FROM District WHERE StateID=29 ORDER BY Population DESC LIMIT 8"
        ).fetchall()
        top_subheads = conn.execute("""
            SELECT sh.CrimeHeadName, COUNT(*) AS c
            FROM CaseMaster cm JOIN CrimeSubHead sh ON sh.CrimeSubHeadID = cm.CrimeMinorHeadID
            GROUP BY sh.CrimeHeadName ORDER BY c DESC LIMIT 8
        """).fetchall()
        neighbour_states = conn.execute(
            "SELECT StateName FROM State WHERE StateID != 29 ORDER BY StateName"
        ).fetchall()
        drange = conn.execute(
            "SELECT MIN(IncidentFromDate), MAX(IncidentFromDate) FROM CaseMaster"
        ).fetchone()
        text = (
            "DATA GROUNDING (auto)\n"
            "---------------------\n"
            f"Data window: {drange[0][:10] if drange[0] else '?'} → {drange[1][:10] if drange[1] else '?'}\n"
            f"Top Karnataka districts by population: {', '.join(r[0] for r in top_districts)}\n"
            f"Most-common sub-heads: {', '.join(f'{r[0]} ({r[1]})' for r in top_subheads)}\n"
            f"Neighbouring states in this DB: {', '.join(r[0] for r in neighbour_states)}\n"
        )
    except sqlite3.Error:
        text = ""
    _DATA_CACHE["ts"] = now
    _DATA_CACHE["text"] = text
    return text


def build_system_prompt(conn: sqlite3.Connection | None) -> str:
    parts = [SYSTEM_PROMPT_HEADER, "\nSCHEMA\n======\n" + SCHEMA_SUMMARY]
    if conn is not None:
        g = _grounding(conn)
        if g:
            parts.append("\n" + g)
    fewshot_json = "\n".join(
        f"Q: {q}\nA: {json.dumps({'sql': re.sub(r'\\s+', ' ', s).strip(), 'explanation': e})}"
        for q, s, e in CURATED_QA[:8]
    )
    parts.append("\nEXAMPLES\n========\n" + fewshot_json)
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Safety layer
# --------------------------------------------------------------------------
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|"
    r"replace|grant|revoke|truncate)\b", re.I
)
ALLOWED_TABLES = {
    "casemaster", "victim", "accused", "complainantdetails", "arrestsurrender",
    "chargesheetdetails", "actsectionassociation", "inv_occurancetime",
    "crimehead", "crimesubhead", "act", "section", "crimeheadactsection",
    "casecategory", "gravityoffence", "casestatusmaster",
    "state", "district", "unit", "unittype", "court",
    "employee", "rank", "designation",
    "castemaster", "religionmaster", "occupationmaster",
    "monthly_baseline", "weekly_counts",
}


def _sanitize_sql(sql: str) -> tuple[bool, str]:
    s = sql.strip().strip("`").strip()
    # Strip markdown code fences if the model got clever.
    s = re.sub(r"^```(?:sql|sqlite)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    s = s.rstrip(";").strip()
    if not s: return False, "empty query"
    if not re.match(r"(?is)^\s*(with|select)\b", s):
        return False, "only SELECT / WITH queries are allowed"
    if FORBIDDEN.search(s): return False, "forbidden keyword"
    if ";" in s:            return False, "multiple statements not allowed"
    tables = re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z_0-9]*)", s, re.I)
    for t in tables:
        if t.lower() not in ALLOWED_TABLES:
            return False, f"unknown or disallowed table: {t}"
    if not re.search(r"\blimit\s+\d+", s, re.I):
        s = s + " LIMIT 500"
    return True, s


# --------------------------------------------------------------------------
# Provider-specific HTTP calls
# --------------------------------------------------------------------------
def _openai_compat_call(base_url: str, api_key: str, model: str,
                        system: str, user: str) -> str:
    if httpx is None: raise RuntimeError("httpx not installed")
    body = {
        "model": model, "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    with httpx.Client(timeout=45.0) as c:
        r = c.post(f"{base_url.rstrip('/')}/chat/completions",
                   headers={"Authorization": f"Bearer {api_key}"}, json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _ollama_call(host: str, model: str, system: str, user: str) -> str:
    if httpx is None: raise RuntimeError("httpx not installed")
    body = {"model": model,
            "prompt": f"{system}\n\nQ: {user}\nA:",
            "stream": False, "format": "json",
            "options": {"temperature": 0.0}}
    with httpx.Client(timeout=60.0) as c:
        r = c.post(f"{host.rstrip('/')}/api/generate", json=body)
        r.raise_for_status()
        return r.json().get("response", "")


def _hf_call(model: str, token: str, system: str, user: str) -> str:
    if httpx is None: raise RuntimeError("httpx not installed")
    body = {
        "model": model, "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    with httpx.Client(timeout=45.0) as c:
        r = c.post("https://router.huggingface.co/v1/chat/completions",
                   headers={"Authorization": f"Bearer {token}"}, json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------
# Offline intent templates (fallback + zero-config mode)
# --------------------------------------------------------------------------
_TEMPLATES = [
    (re.compile(r"top\s+(\d+)?\s*offenders|repeat offenders|most active accused|prolific", re.I),
     lambda m: (
        f"""SELECT a.person_link_id, a.AccusedName, COUNT(DISTINCT a.CaseMasterID) AS cases,
              COUNT(DISTINCT cm.CrimeMinorHeadID) AS distinct_subheads
           FROM Accused a JOIN CaseMaster cm ON cm.CaseMasterID = a.CaseMasterID
           WHERE a.person_link_id IS NOT NULL
           GROUP BY a.person_link_id, a.AccusedName ORDER BY cases DESC LIMIT {m.group(1) or 15}""",
        "Top repeat offenders by distinct case count.")),
    (re.compile(r"cross[- ]border|out[- ]of[- ]state arrest|arrested outside karnataka|inter[- ]state", re.I),
     lambda m: (
        """SELECT s.StateName, d.DistrictName, COUNT(*) AS arrests
           FROM ArrestSurrender ars
           JOIN State s ON s.StateID = ars.ArrestSurrenderStateId
           LEFT JOIN District d ON d.DistrictID = ars.ArrestSurrenderDistrictId
           WHERE ars.ArrestSurrenderStateId != 29
           GROUP BY s.StateName, d.DistrictName ORDER BY arrests DESC LIMIT 25""",
        "Arrests made outside Karnataka.")),
    (re.compile(r"chargesheet(\s+rate|ed)?|solve\s+rate|conviction\s+rate", re.I),
     lambda m: (
        """SELECT d.DistrictName, COUNT(cm.CaseMasterID) AS total,
              SUM(CASE WHEN cs.cstype='A' THEN 1 ELSE 0 END) AS chargesheeted,
              ROUND(100.0*SUM(CASE WHEN cs.cstype='A' THEN 1 ELSE 0 END)/NULLIF(COUNT(cm.CaseMasterID),0),1) AS pct
           FROM CaseMaster cm
           JOIN Unit u ON u.UnitID = cm.PoliceStationID
           JOIN District d ON d.DistrictID = u.DistrictID
           LEFT JOIN ChargesheetDetails cs ON cs.CaseMasterID = cm.CaseMasterID
           WHERE d.StateID = 29
           GROUP BY d.DistrictID ORDER BY pct DESC LIMIT 25""",
        "Chargesheet rate by district (Karnataka).")),
    (re.compile(r"heinous|gravity", re.I),
     lambda m: (
        """SELECT d.DistrictName, COUNT(*) AS heinous_cases
           FROM CaseMaster cm
           JOIN Unit u ON u.UnitID = cm.PoliceStationID
           JOIN District d ON d.DistrictID = u.DistrictID
           JOIN GravityOffence g ON g.GravityOffenceID = cm.GravityOffenceID
           WHERE g.LookupValue = 'Heinous' AND d.StateID = 29
           GROUP BY d.DistrictName ORDER BY heinous_cases DESC LIMIT 20""",
        "Heinous cases by Karnataka district.")),
    (re.compile(r"cyber", re.I),
     lambda m: (
        """SELECT d.DistrictName, COUNT(*) AS cyber_cases
           FROM CaseMaster cm
           JOIN Unit u ON u.UnitID = cm.PoliceStationID
           JOIN District d ON d.DistrictID = u.DistrictID
           JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID
           WHERE ch.CrimeGroupName = 'Cyber Crimes' AND d.StateID = 29
           GROUP BY d.DistrictName ORDER BY cyber_cases DESC LIMIT 20""",
        "Cyber-crime cases by district.")),
    (re.compile(r"narcotic|drug|ndps", re.I),
     lambda m: (
        """SELECT d.DistrictName, COUNT(*) AS narcotic_cases
           FROM CaseMaster cm
           JOIN Unit u ON u.UnitID = cm.PoliceStationID
           JOIN District d ON d.DistrictID = u.DistrictID
           JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID
           WHERE ch.CrimeGroupName = 'Narcotic Offences' AND d.StateID = 29
           GROUP BY d.DistrictName ORDER BY narcotic_cases DESC LIMIT 20""",
        "Narcotic-offence cases by district.")),
    (re.compile(r"under\s+investigation|pending", re.I),
     lambda m: (
        """SELECT d.DistrictName, COUNT(*) AS pending
           FROM CaseMaster cm
           JOIN Unit u ON u.UnitID = cm.PoliceStationID
           JOIN District d ON d.DistrictID = u.DistrictID
           JOIN CaseStatusMaster s ON s.CaseStatusID = cm.CaseStatusID
           WHERE s.CaseStatusName IN ('UnderInvestigation','Pending Trial','PendingBeforeCourt')
             AND d.StateID = 29
           GROUP BY d.DistrictName ORDER BY pending DESC LIMIT 20""",
        "Pending cases by Karnataka district.")),
    (re.compile(r"court", re.I),
     lambda m: (
        """SELECT c.CourtName, d.DistrictName, COUNT(*) AS cases
           FROM CaseMaster cm
           JOIN Court c ON c.CourtID = cm.CourtID
           JOIN District d ON d.DistrictID = c.DistrictID
           GROUP BY c.CourtID ORDER BY cases DESC LIMIT 25""",
        "Cases handled by court.")),
    (re.compile(r"community|gang", re.I),
     lambda m: (
        """SELECT a.person_link_id, a.AccusedName, COUNT(DISTINCT a.CaseMasterID) AS cases
           FROM Accused a
           WHERE a.person_link_id IS NOT NULL
           GROUP BY a.person_link_id, a.AccusedName
           HAVING cases >= 15
           ORDER BY cases DESC LIMIT 30""",
        "Highly-linked offenders (candidates for gang detection).")),
    (re.compile(r"spike|surge|last\s+(?:90|30)\s+days|emerging", re.I),
     lambda m: (
        """SELECT sh.CrimeHeadName,
              SUM(CASE WHEN cm.CrimeRegisteredDate >= date('now','-90 days') THEN 1 ELSE 0 END) AS recent,
              SUM(CASE WHEN cm.CrimeRegisteredDate < date('now','-90 days')
                       AND cm.CrimeRegisteredDate >= date('now','-180 days') THEN 1 ELSE 0 END) AS prior
           FROM CaseMaster cm
           JOIN CrimeSubHead sh ON sh.CrimeSubHeadID = cm.CrimeMinorHeadID
           GROUP BY sh.CrimeHeadName HAVING recent > prior ORDER BY (recent - prior) DESC LIMIT 20""",
        "Sub-heads that grew in the last 90 days.")),
    (re.compile(r"investigating\s+officer|io\b|caseload", re.I),
     lambda m: (
        """SELECT e.FirstName || ' ' || e.LastName AS io_name, u.UnitName, COUNT(cm.CaseMasterID) AS caseload
           FROM Employee e JOIN Designation d ON d.DesignationID = e.DesignationID
           JOIN CaseMaster cm ON cm.PolicePersonID = e.EmployeeID
           JOIN Unit u ON u.UnitID = e.UnitID
           WHERE d.DesignationName = 'Investigating Officer'
           GROUP BY e.EmployeeID ORDER BY caseload DESC LIMIT 25""",
        "Investigating officer caseload leaderboard.")),
    (re.compile(r"total|how many|count|volume", re.I),
     lambda m: (
        """SELECT ch.CrimeGroupName, COUNT(*) AS cases
           FROM CaseMaster cm JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID
           GROUP BY ch.CrimeGroupName ORDER BY cases DESC""",
        "Case totals by crime class.")),
]


def _offline_sql(question: str) -> tuple[str, str] | None:
    for pat, tpl in _TEMPLATES:
        m = pat.search(question)
        if m:
            sql, explanation = tpl(m)
            return re.sub(r"\s+", " ", sql).strip(), explanation
    return None


# --------------------------------------------------------------------------
# Knowledge mode — general Q&A on crime, evidence, procedure, law & order
# --------------------------------------------------------------------------
KNOWLEDGE_PROMPT = """You are the KSP CIP Domain Assistant — an authoritative reference for
Karnataka Police officers on crime investigation, evidence handling, and law
and order. You answer conversationally and cite the exact BNS / IPC / BNSS /
IEA / NDPS / IT Act / MV Act sections that apply.

Coverage
========
• Substantive offences under the Bharatiya Nyaya Sanhita, 2023 (BNS) and
  the legacy Indian Penal Code, 1860 (IPC). Reference both when relevant so
  officers trained on IPC can map to BNS.
• Procedure under the Bharatiya Nagarik Suraksha Sanhita (BNSS) and the
  Code of Criminal Procedure, 1973 (CrPC) — FIR registration, Zero FIR,
  arrest procedure, searches, remand.
• Evidence handling: chain of custody, forensic sampling, digital evidence
  under Section 63 of the Bharatiya Sakshya Adhiniyam / Section 65B IEA.
• Special Acts: NDPS Act, POCSO, IT Act 2000 (esp. §§ 43, 65, 66, 67), Arms
  Act, MV Act, PWDVA, SC/ST Prevention of Atrocities Act.
• Investigation best practice, panchanama drafting, seizure memos,
  chargesheet preparation (Form IF-1 through IF-6).
• Karnataka-specific: KSP standing orders, Bengaluru Cyber Crime SOPs,
  CCTNS workflow.

Response rules
==============
1. Be crisp and directly useful for an operational officer.
2. Always cite the sections you rely on ("BNS §303 corresponds to IPC §379…").
3. If the question is procedural, list steps as a numbered list.
4. If the question is legal, quote the section's essence, not the raw text.
5. If a question is ambiguous, ask ONE targeted clarifying question.
6. NEVER fabricate section numbers. If you don't know, say so.
7. Respond in Markdown so the UI can render lists, headings and code blocks.
"""

ROUTER_PROMPT = """You are a router for the KSP CIP Assistant. Classify the user's message
into exactly one of:

  "analytics"  – the question needs to be answered from the crime database
                 (counts, comparisons, offender leaderboards, hotspots, etc.).
  "knowledge"  – the question needs domain knowledge about law, procedure,
                 evidence handling, or interpretation of the Act (no database
                 lookup would answer it).

Reply as strict JSON only:  {"mode": "analytics" | "knowledge"}
"""


def _openai_compat_chat(base_url: str, api_key: str, model: str,
                        messages: list[dict], json_mode: bool = False,
                        temperature: float = 0.2) -> str:
    if httpx is None: raise RuntimeError("httpx not installed")
    body: dict[str, Any] = {"model": model, "temperature": temperature, "messages": messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    with httpx.Client(timeout=45.0) as c:
        r = c.post(f"{base_url.rstrip('/')}/chat/completions",
                   headers={"Authorization": f"Bearer {api_key}"}, json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _configured_backend() -> str:
    forced = os.environ.get("KSP_LLM_BACKEND")
    if forced:
        return forced.lower().strip()
    if os.environ.get("GROQ_API_KEY"):     return "groq"
    if os.environ.get("OPENAI_BASE_URL") and os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("HF_TOKEN"):         return "hf"
    if os.environ.get("OLLAMA_HOST"):      return "ollama"
    return "offline"


def _backend_endpoint_and_key(backend: str) -> tuple[str, str, str] | None:
    """Return (base_url, api_key, default_model) for OpenAI-compatible backends, or None."""
    if backend == "groq":
        return ("https://api.groq.com/openai/v1", os.environ.get("GROQ_API_KEY", ""),
                os.environ.get("KSP_LLM_MODEL", "llama-3.3-70b-versatile"))
    if backend == "openai":
        return (os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                os.environ.get("OPENAI_API_KEY", ""),
                os.environ.get("KSP_LLM_MODEL", "gpt-4o-mini"))
    return None


def _route_intent(question: str, backend: str) -> str:
    """LLM-classify a question as 'analytics' or 'knowledge'. Falls back to a
    keyword heuristic if the router call fails."""
    conf = _backend_endpoint_and_key(backend)
    if conf:
        base, key, model = conf
        try:
            raw = _openai_compat_chat(
                base, key, model,
                [
                    {"role": "system", "content": ROUTER_PROMPT},
                    {"role": "user",   "content": question},
                ],
                json_mode=True, temperature=0.0,
            )
            m = re.search(r'\{[^{}]*"mode"[^{}]*\}', raw, re.S)
            if m:
                mode = json.loads(m.group(0)).get("mode", "").lower()
                if mode in ("analytics", "knowledge"):
                    return mode
        except Exception:
            pass
    # Heuristic fallback.
    if re.search(r"\b(procedure|section|bns|ipc|cr[.\s]?p[.\s]?c|bnss|evidence|arrest|panchnama|panchanama|"
                 r"chargesheet|seizure|law|advice|explain|what should|how do i|"
                 r"applicable|zero fir|remand|bail|fir\s+process|difference between|"
                 r"how to draft|sop|standing order)\b", question, re.I):
        return "knowledge"
    return "analytics"


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------
@dataclass
class LLMResult:
    question: str
    mode: str              # 'analytics' | 'knowledge'
    sql: str
    explanation: str
    backend: str
    columns: list[str]
    rows: list[list]
    row_count: int
    answer: str | None = None    # populated in knowledge mode
    error: str | None = None


# --------------------------------------------------------------------------
# Analytics path — text-to-SQL (existing behaviour, refactored)
# --------------------------------------------------------------------------
def _ask_llm_sql(question: str, system: str) -> tuple[str, str, str]:
    backend = _configured_backend()
    raw: str | None = None

    conf = _backend_endpoint_and_key(backend)
    try:
        if conf:
            base, key, model = conf
            if key:
                raw = _openai_compat_chat(
                    base, key, model,
                    [{"role": "system", "content": system},
                     {"role": "user",   "content": f"Question: {question}\n\nRespond with JSON only."}],
                    json_mode=True, temperature=0.0,
                )
        elif backend == "hf":
            raw = _hf_call(
                os.environ.get("KSP_LLM_MODEL", "meta-llama/Llama-3.3-70B-Instruct"),
                os.environ["HF_TOKEN"], system, question,
            )
        elif backend == "ollama":
            raw = _ollama_call(
                os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                os.environ.get("KSP_LLM_MODEL", "llama3.2:1b"),
                system, question,
            )
    except Exception:
        raw = None

    if raw:
        try:
            j = json.loads(raw)
            if j.get("sql"): return j["sql"], j.get("explanation", ""), backend
        except Exception:
            m = re.search(r'\{[^{}]*"sql"[^{}]*\}', raw, re.S)
            if m:
                try:
                    j = json.loads(m.group(0))
                    return j.get("sql", ""), j.get("explanation", ""), backend
                except Exception:
                    pass

    off = _offline_sql(question)
    if off:
        sql, expl = off
        return sql, expl, "offline"
    return (
        "SELECT COUNT(*) AS total_cases FROM CaseMaster",
        "Could not interpret the question; showing total case count.",
        "offline",
    )


# --------------------------------------------------------------------------
# Knowledge path — direct chat completion with the domain prompt
# --------------------------------------------------------------------------
def _ask_llm_knowledge(question: str, history: list[dict]) -> tuple[str, str]:
    backend = _configured_backend()
    conf = _backend_endpoint_and_key(backend)
    if not conf or not conf[1]:
        # Offline fallback — canned response.
        return (
            "The KSP CIP Assistant knowledge mode needs an online LLM. "
            "Enable it with `./enable-online-llm.sh groq <your_api_key>` and try again. "
            "For SQL-style analytics questions, the offline templates still work.",
            "offline",
        )
    base, key, model = conf
    messages = [{"role": "system", "content": KNOWLEDGE_PROMPT}]
    # Include up to 6 prior turns.
    for turn in (history or [])[-6:]:
        role = turn.get("role"); content = turn.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)[:4000]})
    messages.append({"role": "user", "content": question})
    try:
        answer = _openai_compat_chat(base, key, model, messages, json_mode=False, temperature=0.25)
        return answer.strip(), backend
    except Exception as e:
        return f"⚠ Could not reach the LLM: {e}", backend


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------
def ask(conn: sqlite3.Connection, question: str,
        history: list[dict] | None = None, mode: str = "auto") -> LLMResult:
    backend = _configured_backend()
    resolved_mode = mode
    if resolved_mode == "auto":
        resolved_mode = _route_intent(question, backend)

    if resolved_mode == "knowledge":
        answer, used = _ask_llm_knowledge(question, history or [])
        return LLMResult(
            question=question, mode="knowledge",
            sql="", explanation="", backend=used,
            columns=[], rows=[], row_count=0, answer=answer,
        )

    # analytics
    system = build_system_prompt(conn)
    sql, expl, used = _ask_llm_sql(question, system)
    ok, out = _sanitize_sql(sql)
    if not ok:
        return LLMResult(question=question, mode="analytics", sql=sql, explanation=expl,
                         backend=used, columns=[], rows=[], row_count=0, error=out)
    try:
        cur = conn.execute(out)
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchall()
        return LLMResult(question=question, mode="analytics", sql=out, explanation=expl,
                         backend=used, columns=cols, rows=[list(r) for r in rows],
                         row_count=len(rows))
    except sqlite3.Error as e:
        return LLMResult(question=question, mode="analytics", sql=out, explanation=expl,
                         backend=used, columns=[], rows=[], row_count=0, error=str(e))
