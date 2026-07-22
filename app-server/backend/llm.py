"""
Schema-grounded LLM assistant.

Provides a natural-language query interface over the KSP FIR database.

Design:
  * The full schema summary (table + column names + one-line description) is
    injected into every LLM prompt.
  * The model is asked to emit a JSON object with:
      { "sql": "<single SELECT statement>", "explanation": "<one line>" }
  * The SQL is validated by a safety layer before execution: read-only,
    single statement, allowlisted tables, forced LIMIT, no destructive verbs.
  * The result set is formatted back to the user with a compact table.

Backends (pluggable via KSP_LLM_BACKEND env var):
  * `ollama` — POST to http://localhost:11434/api/generate.  Default when the
    OLLAMA_HOST env var is present.  Model = KSP_LLM_MODEL (default llama3.2:1b).
  * `openai` — OpenAI-compatible endpoint (works with vLLM, LM Studio, LocalAI,
    or a real OpenAI key). Requires OPENAI_BASE_URL + OPENAI_API_KEY.
  * `offline` — rule-based intent matcher for common queries. Runs when no
    LLM is configured; ensures the assistant is always usable at demo time.

The training script scripts/train_llm.py provides a LoRA fine-tune recipe
for offline model customization on this schema.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, asdict
from typing import Any

try:
    import httpx  # type: ignore
except Exception:
    httpx = None  # noqa: N816

# --- Schema description injected into every LLM prompt ------------------------------
SCHEMA_SUMMARY = """\
KSP Police FIR System — key tables (SQLite):

CaseMaster(CaseMasterID PK, CrimeNo, CaseNo, CrimeRegisteredDate,
  PolicePersonID→Employee, PoliceStationID→Unit, CaseCategoryID→CaseCategory,
  GravityOffenceID→GravityOffence, CrimeMajorHeadID→CrimeHead,
  CrimeMinorHeadID→CrimeSubHead, CaseStatusID→CaseStatusMaster,
  CourtID→Court, IncidentFromDate, IncidentToDate, InfoReceivedPSDate,
  latitude, longitude, BriefFacts, modus_operandi, weapon)

Victim(VictimMasterID PK, CaseMasterID→CaseMaster, VictimName, AgeYear, GenderID, VictimPolice)
Accused(AccusedMasterID PK, CaseMasterID, AccusedName, AgeYear, GenderID,
  PersonID (A1/A2/…), person_link_id, is_repeat, home_district_id)
ComplainantDetails(ComplainantID PK, CaseMasterID, ComplainantName, AgeYear,
  OccupationID, ReligionID, CasteID, GenderID)
ArrestSurrender(ArrestSurrenderID PK, CaseMasterID, ArrestSurrenderTypeID
  (1=Arrest,2=Surrender), ArrestSurrenderDate, ArrestSurrenderStateId→State,
  ArrestSurrenderDistrictId→District, PoliceStationID→Unit, IOID→Employee,
  CourtID→Court, AccusedMasterID→Accused)
ChargesheetDetails(CSID PK, CaseMasterID, csdate, cstype (A=Chargesheet/B=False/C=Undetected), PolicePersonID)
ActSectionAssociation(CaseMasterID, ActID→Act, SectionID→Section, ActOrderID, SectionOrderID)

Masters:
CrimeHead(CrimeHeadID PK, CrimeGroupName)      -- e.g. Property Crimes, Cyber Crimes
CrimeSubHead(CrimeSubHeadID PK, CrimeHeadID, CrimeHeadName)  -- Murder, Theft, Cyber Fraud, ...
Act(ActCode PK, ActDescription, ShortName)     -- BNS, IPC, IT, NDPS, MV, POCSO
Section(ActCode, SectionCode PK part, SectionDescription)
CaseCategory(CaseCategoryID PK, LookupValue, CategoryCode)   -- FIR/UDR/PAR/ZeroFIR
GravityOffence(GravityOffenceID PK, LookupValue)             -- Heinous/Non-Heinous/Petty
CaseStatusMaster(CaseStatusID PK, CaseStatusName)            -- UnderInvestigation/ChargeSheeted/Closed/…

Geography:
State(StateID PK, StateName)   -- Karnataka.StateID = 29
District(DistrictID PK, DistrictName, StateID, HqLat, HqLng, Population, UrbanPct, LiteracyPct, Zone)
Unit(UnitID PK, UnitName, TypeID, ParentUnit, StateID, DistrictID, Lat, Lng)  -- Police stations
UnitType(UnitTypeID PK, UnitTypeName, CityDistState, Hierarchy)
Court(CourtID PK, CourtName, DistrictID, StateID)

Personnel:
Employee(EmployeeID PK, DistrictID, UnitID, RankID, DesignationID, KGID, FirstName, LastName,
  EmployeeDOB, GenderID, BloodGroupID, PhysicallyChallenged, AppointmentDate)
Rank(RankID PK, RankName, Hierarchy)
Designation(DesignationID PK, DesignationName)

Person masters:
CasteMaster(caste_master_id PK, caste_master_name)
ReligionMaster(ReligionID PK, ReligionName)
OccupationMaster(OccupationID PK, OccupationName)
"""

SYSTEM_PROMPT = f"""You are a SQL analyst for the Karnataka Police State Crime Records Bureau.
You answer questions about crime by writing SQLite queries against the KSP FIR schema.

RULES (strict):
  * Emit ONE valid SQLite SELECT statement.
  * Never modify data (no INSERT / UPDATE / DELETE / DROP / ALTER / CREATE / PRAGMA / ATTACH / VACUUM).
  * Use only tables from the schema below.
  * Prefer joins over subqueries; add a LIMIT if the question implies a list.
  * Use column names EXACTLY as in the schema (case-sensitive).
  * Karnataka's StateID is 29.
  * Always respond as strict JSON: {{"sql": "...", "explanation": "..."}}. No prose outside the JSON.

Schema:
{SCHEMA_SUMMARY}"""


# --- safety layer -------------------------------------------------------------------
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|replace|"
    r"grant|revoke|truncate)\b", re.I
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
    """Return (ok, sanitized_sql_or_reason)."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        return False, "empty query"
    if not re.match(r"(?is)^\s*(with|select)\b", s):
        return False, "only SELECT / WITH queries are allowed"
    if FORBIDDEN.search(s):
        return False, "forbidden keyword"
    # Reject multiple statements.
    if ";" in s:
        return False, "multiple statements not allowed"
    # Table allowlist — check every identifier following FROM/JOIN.
    tables = re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z_0-9]*)", s, re.I)
    for t in tables:
        if t.lower() not in ALLOWED_TABLES:
            return False, f"unknown or disallowed table: {t}"
    # Force a LIMIT for safety.
    if not re.search(r"\blimit\s+\d+", s, re.I):
        s = s + " LIMIT 500"
    return True, s


# --- backends -----------------------------------------------------------------------
def _call_ollama(prompt: str, model: str, host: str) -> str:
    if httpx is None:
        raise RuntimeError("httpx not installed")
    with httpx.Client(timeout=60.0) as c:
        r = c.post(f"{host.rstrip('/')}/api/generate", json={
            "model": model, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.0},
            "format": "json",
        })
        r.raise_for_status()
        return r.json().get("response", "")


def _call_openai(prompt: str, model: str, base_url: str, api_key: str) -> str:
    if httpx is None:
        raise RuntimeError("httpx not installed")
    with httpx.Client(timeout=60.0) as c:
        r = c.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model, "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        r.raise_for_status()
        j = r.json()
        return j["choices"][0]["message"]["content"]


# --- offline fallback: intent → SQL templates ---------------------------------------
_TEMPLATES = [
    (re.compile(r"top\s+(\d+)?\s*offenders|repeat offenders|most active accused", re.I),
     lambda m: (f"""
        SELECT a.person_link_id, a.AccusedName, COUNT(DISTINCT a.CaseMasterID) AS cases,
               COUNT(DISTINCT cm.CrimeMinorHeadID) AS distinct_subheads
        FROM Accused a JOIN CaseMaster cm ON cm.CaseMasterID = a.CaseMasterID
        WHERE a.person_link_id IS NOT NULL
        GROUP BY a.person_link_id, a.AccusedName ORDER BY cases DESC LIMIT {m.group(1) or 15}
     """, "Top repeat offenders by case count")),

    (re.compile(r"cross[- ]border|out[- ]of[- ]state arrest|arrested outside karnataka", re.I),
     lambda m: ("""
        SELECT s.StateName, d.DistrictName, COUNT(*) AS arrests
        FROM ArrestSurrender ars
        JOIN State s ON s.StateID = ars.ArrestSurrenderStateId
        LEFT JOIN District d ON d.DistrictID = ars.ArrestSurrenderDistrictId
        WHERE ars.ArrestSurrenderStateId != 29
        GROUP BY s.StateName, d.DistrictName ORDER BY arrests DESC LIMIT 25
     """, "Arrests made outside Karnataka")),

    (re.compile(r"chargesheet(\s+rate|ed)?|solve rate", re.I),
     lambda m: ("""
        SELECT u.UnitName,
               COUNT(cm.CaseMasterID) AS total,
               SUM(CASE WHEN cs.cstype = 'A' THEN 1 ELSE 0 END) AS chargesheeted,
               ROUND(100.0 * SUM(CASE WHEN cs.cstype = 'A' THEN 1 ELSE 0 END) / COUNT(cm.CaseMasterID), 1) AS pct
        FROM CaseMaster cm
        JOIN Unit u ON u.UnitID = cm.PoliceStationID
        LEFT JOIN ChargesheetDetails cs ON cs.CaseMasterID = cm.CaseMasterID
        GROUP BY u.UnitID HAVING total >= 50 ORDER BY pct DESC LIMIT 25
     """, "Chargesheet rate by police station")),

    (re.compile(r"heinous|gravity", re.I),
     lambda m: ("""
        SELECT d.DistrictName, COUNT(*) AS heinous_cases
        FROM CaseMaster cm
        JOIN Unit u ON u.UnitID = cm.PoliceStationID
        JOIN District d ON d.DistrictID = u.DistrictID
        JOIN GravityOffence g ON g.GravityOffenceID = cm.GravityOffenceID
        WHERE g.LookupValue = 'Heinous'
        GROUP BY d.DistrictName ORDER BY heinous_cases DESC LIMIT 20
     """, "Heinous cases by district")),

    (re.compile(r"cyber", re.I),
     lambda m: ("""
        SELECT d.DistrictName, COUNT(*) AS cyber_cases
        FROM CaseMaster cm
        JOIN Unit u ON u.UnitID = cm.PoliceStationID
        JOIN District d ON d.DistrictID = u.DistrictID
        JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID
        WHERE ch.CrimeGroupName = 'Cyber Crimes'
        GROUP BY d.DistrictName ORDER BY cyber_cases DESC LIMIT 20
     """, "Cyber-crime cases by district")),

    (re.compile(r"under\s+investigation|pending", re.I),
     lambda m: ("""
        SELECT d.DistrictName, COUNT(*) AS pending
        FROM CaseMaster cm
        JOIN Unit u ON u.UnitID = cm.PoliceStationID
        JOIN District d ON d.DistrictID = u.DistrictID
        JOIN CaseStatusMaster s ON s.CaseStatusID = cm.CaseStatusID
        WHERE s.CaseStatusName IN ('UnderInvestigation','Pending Trial','PendingBeforeCourt')
        GROUP BY d.DistrictName ORDER BY pending DESC LIMIT 20
     """, "Pending cases by district")),

    (re.compile(r"court", re.I),
     lambda m: ("""
        SELECT c.CourtName, d.DistrictName, COUNT(*) AS cases
        FROM CaseMaster cm
        JOIN Court c ON c.CourtID = cm.CourtID
        JOIN District d ON d.DistrictID = c.DistrictID
        GROUP BY c.CourtID ORDER BY cases DESC LIMIT 25
     """, "Cases pending by court")),

    (re.compile(r"total|how many|count", re.I),
     lambda m: ("""
        SELECT ch.CrimeGroupName, COUNT(*) AS cases
        FROM CaseMaster cm JOIN CrimeHead ch ON ch.CrimeHeadID = cm.CrimeMajorHeadID
        GROUP BY ch.CrimeGroupName ORDER BY cases DESC
     """, "Case totals by crime head")),
]


def _offline_sql(question: str) -> tuple[str, str] | None:
    for pat, tpl in _TEMPLATES:
        m = pat.search(question)
        if m:
            sql, explanation = tpl(m)
            return re.sub(r"\s+", " ", sql).strip(), explanation
    return None


# --- public entrypoints -------------------------------------------------------------
@dataclass
class LLMResult:
    question: str
    sql: str
    explanation: str
    backend: str
    columns: list[str]
    rows: list[list]
    row_count: int
    error: str | None = None


def _configured_backend() -> str:
    b = os.environ.get("KSP_LLM_BACKEND")
    if b: return b.lower()
    if os.environ.get("OLLAMA_HOST"):
        return "ollama"
    if os.environ.get("OPENAI_BASE_URL"):
        return "openai"
    return "offline"


def _ask_llm(question: str) -> tuple[str, str, str]:
    """Return (sql, explanation, backend_used)."""
    backend = _configured_backend()
    prompt = f"Question: {question}\n\nRespond with JSON only."
    raw: str | None = None
    if backend == "ollama":
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        model = os.environ.get("KSP_LLM_MODEL", "llama3.2:1b")
        raw = _call_ollama(f"{SYSTEM_PROMPT}\n\n{prompt}", model, host)
    elif backend == "openai":
        base = os.environ["OPENAI_BASE_URL"]
        key = os.environ.get("OPENAI_API_KEY", "sk-none")
        model = os.environ.get("KSP_LLM_MODEL", "gpt-4o-mini")
        raw = _call_openai(prompt, model, base, key)

    if raw:
        try:
            j = json.loads(raw)
            return j["sql"], j.get("explanation", ""), backend
        except Exception:
            pass  # fall through to offline

    off = _offline_sql(question)
    if off:
        sql, expl = off
        return sql, expl, "offline"
    # Last resort — count everything.
    return "SELECT COUNT(*) AS total_cases FROM CaseMaster", \
           "Could not interpret the question; returning total case count.", "offline"


def ask(conn: sqlite3.Connection, question: str) -> LLMResult:
    sql, expl, backend = _ask_llm(question)
    ok, out = _sanitize_sql(sql)
    if not ok:
        return LLMResult(question=question, sql=sql, explanation=expl, backend=backend,
                         columns=[], rows=[], row_count=0, error=out)
    try:
        cur = conn.execute(out)
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchall()
        return LLMResult(question=question, sql=out, explanation=expl, backend=backend,
                         columns=cols, rows=[list(r) for r in rows], row_count=len(rows))
    except sqlite3.Error as e:
        return LLMResult(question=question, sql=out, explanation=expl, backend=backend,
                         columns=[], rows=[], row_count=0, error=str(e))
