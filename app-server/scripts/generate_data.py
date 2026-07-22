"""
Synthetic Karnataka Police FIR data generator — full KSP FIR schema.

Populates every table in data/schema.sql with realistic-looking (but entirely
synthetic) data. Includes:
  * Correct 18-digit CrimeNo format: CaseCat(1) + District(4) + Unit(4) + Year(4) + Serial(5)
  * IPC + BNS + NDPS + IT Act + MV Act sections
  * Karnataka + 5 neighbouring states for cross-border arrests
  * Employee hierarchy (ranks, designations, IOs)
  * Court assignments per district
  * Repeat offenders and gang structure via name-linked Accused rows
  * Chargesheets, arrests (including out-of-state arrests)

Run: python scripts/generate_data.py --db data/ksp.db --firs 25000
"""
from __future__ import annotations

import argparse
import math
import random
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

SEED = 42

# ---------- Reference data (Karnataka + neighbours) ---------------------------------

STATES = [
    # (StateID, StateName)
    (29, "Karnataka"),
    (33, "Tamil Nadu"),
    (32, "Kerala"),
    (27, "Maharashtra"),
    (28, "Andhra Pradesh"),
    (36, "Telangana"),
    (30, "Goa"),
]
KARNATAKA_ID = 29

# (DistrictName, Zone, HqLat, HqLng, population, urban_pct, literacy_pct)
KA_DISTRICTS = [
    ("Bengaluru Urban", "South",   12.9716, 77.5946, 9_600_000, 0.90, 87.7),
    ("Bengaluru Rural", "South",   13.2846, 77.6836,   990_000, 0.30, 77.9),
    ("Ramanagara",      "South",   12.7212, 77.2807,  1_082_000, 0.24, 71.4),
    ("Kolar",           "South",   13.1372, 78.1300,  1_540_000, 0.31, 74.4),
    ("Chikkaballapura", "South",   13.4355, 77.7315,  1_255_000, 0.23, 69.8),
    ("Tumakuru",        "South",   13.3410, 77.1010,  2_680_000, 0.22, 75.1),
    ("Mysuru",          "South",   12.2958, 76.6394,  3_000_000, 0.41, 72.8),
    ("Mandya",          "South",   12.5218, 76.8951,  1_805_000, 0.16, 70.1),
    ("Hassan",          "South",   13.0033, 76.1004,  1_776_000, 0.21, 76.1),
    ("Chamarajanagara", "South",   11.9236, 76.9456,  1_020_000, 0.17, 61.4),
    ("Kodagu",          "South",   12.4200, 75.7400,    554_000, 0.16, 82.5),
    ("Dakshina Kannada","South",   12.9141, 74.8560,  2_090_000, 0.47, 88.6),
    ("Udupi",           "South",   13.3409, 74.7421,  1_180_000, 0.29, 86.2),
    ("Chikkamagaluru",  "South",   13.3161, 75.7720,  1_137_000, 0.22, 79.2),
    ("Shivamogga",      "South",   13.9299, 75.5681,  1_753_000, 0.35, 80.5),
    ("Davangere",       "Central", 14.4644, 75.9218,  1_945_000, 0.32, 75.7),
    ("Chitradurga",     "Central", 14.2251, 76.3980,  1_659_000, 0.20, 73.7),
    ("Ballari",         "Central", 15.1394, 76.9214,  2_452_000, 0.36, 67.8),
    ("Vijayanagara",    "Central", 15.3350, 76.4600,  1_353_000, 0.28, 71.0),
    ("Koppal",          "Central", 15.3547, 76.1544,  1_389_000, 0.17, 68.1),
    ("Raichur",         "Central", 16.2076, 77.3463,  1_929_000, 0.22, 59.6),
    ("Yadgir",          "Central", 16.7621, 77.1370,  1_174_000, 0.16, 51.8),
    ("Kalaburagi",      "North",   17.3297, 76.8343,  2_566_000, 0.33, 65.7),
    ("Bidar",           "North",   17.9133, 77.5301,  1_703_000, 0.24, 70.5),
    ("Vijayapura",      "North",   16.8302, 75.7100,  2_177_000, 0.22, 67.2),
    ("Bagalkote",       "North",   16.1848, 75.6961,  1_890_000, 0.29, 68.8),
    ("Belagavi",        "North",   15.8497, 74.4977,  4_780_000, 0.25, 73.5),
    ("Dharwad",         "North",   15.4589, 75.0078,  1_847_000, 0.57, 80.0),
    ("Gadag",           "North",   15.4300, 75.6300,  1_064_000, 0.35, 75.1),
    ("Haveri",          "North",   14.7940, 75.4040,  1_598_000, 0.23, 77.6),
    ("Uttara Kannada",  "North",   14.7935, 74.6869,  1_437_000, 0.29, 84.1),
]

# Districts in neighbouring states with rough centroids (subset — used only for
# cross-border arrest locations).
OUT_OF_STATE_DISTRICTS = [
    # (name, state_id, lat, lng)
    ("Chennai",        33, 13.0827, 80.2707),
    ("Coimbatore",     33, 11.0168, 76.9558),
    ("Salem",          33, 11.6643, 78.1460),
    ("Krishnagiri",    33, 12.5266, 78.2141),
    ("Kannur",         32, 11.8745, 75.3704),
    ("Kasaragod",      32, 12.5000, 74.9900),
    ("Ernakulam",      32, 10.0000, 76.3000),
    ("Pune",           27, 18.5204, 73.8567),
    ("Kolhapur",       27, 16.7050, 74.2433),
    ("Solapur",        27, 17.6599, 75.9064),
    ("Anantapur",      28, 14.6819, 77.6006),
    ("Kurnool",        28, 15.8281, 78.0373),
    ("Chittoor",       28, 13.2172, 79.1003),
    ("Hyderabad",      36, 17.3850, 78.4867),
    ("Mahbubnagar",    36, 16.7333, 77.9833),
    ("Panaji",         30, 15.4909, 73.8278),
]

CASE_CATEGORIES = [
    # (LookupValue, CategoryCode)
    ("FIR", 1),
    ("UDR", 3),
    ("PAR", 4),
    ("ZeroFIR", 8),
]
GRAVITY = ["Heinous", "Non-Heinous", "Petty"]
CASE_STATUSES = ["UnderInvestigation", "ChargeSheeted", "Closed", "Pending Trial", "PendingBeforeCourt"]

# CrimeHead → [CrimeSubHead ...]
CRIME_HEADS = {
    "Crimes Against Body":     ["Murder", "Attempt to Murder", "Grievous Hurt", "Simple Hurt", "Kidnapping"],
    "Crimes Against Women":    ["Sexual Offence", "Dowry Death", "Domestic Violence", "Molestation"],
    "Property Crimes":         ["Theft", "Burglary", "Robbery", "Dacoity", "Vehicle Theft"],
    "Economic Offences":       ["Cheating", "Criminal Breach of Trust", "Forgery"],
    "Cyber Crimes":            ["Cyber Fraud", "Cyber Harassment", "Data Theft", "Impersonation"],
    "Narcotic Offences":       ["Possession (Small)", "Commercial Quantity", "Peddling"],
    "Traffic Offences":        ["Rash Driving", "Drunk Driving", "Hit and Run"],
    "Other IPC":               ["Public Nuisance", "Missing Person", "Riot"],
}

# CrimeHead subhead weights and (act, section) hooks.
SUBHEAD_META = {
    "Murder":                 {"gravity": "Heinous",     "weight": 0.9,  "sections": [("BNS", "103"), ("IPC", "302")]},
    "Attempt to Murder":      {"gravity": "Heinous",     "weight": 1.4,  "sections": [("BNS", "109"), ("IPC", "307")]},
    "Grievous Hurt":          {"gravity": "Non-Heinous", "weight": 4.0,  "sections": [("BNS", "117"), ("IPC", "325")]},
    "Simple Hurt":             {"gravity": "Petty",       "weight": 11.0, "sections": [("BNS", "115"), ("IPC", "323")]},
    "Kidnapping":              {"gravity": "Heinous",     "weight": 1.4,  "sections": [("BNS", "137"), ("IPC", "363")]},
    "Sexual Offence":          {"gravity": "Heinous",     "weight": 2.6,  "sections": [("BNS", "063"), ("POCSO", "6")]},
    "Dowry Death":             {"gravity": "Heinous",     "weight": 0.6,  "sections": [("IPC", "304B")]},
    "Domestic Violence":       {"gravity": "Non-Heinous", "weight": 2.3,  "sections": [("PWDVA", "3")]},
    "Molestation":             {"gravity": "Non-Heinous", "weight": 2.0,  "sections": [("BNS", "074"), ("IPC", "354")]},
    "Theft":                   {"gravity": "Non-Heinous", "weight": 22.0, "sections": [("BNS", "303"), ("IPC", "379")]},
    "Burglary":                {"gravity": "Non-Heinous", "weight": 12.0, "sections": [("BNS", "305"), ("IPC", "457")]},
    "Robbery":                 {"gravity": "Heinous",     "weight": 4.5,  "sections": [("BNS", "309"), ("IPC", "392")]},
    "Dacoity":                 {"gravity": "Heinous",     "weight": 0.8,  "sections": [("BNS", "310"), ("IPC", "395")]},
    "Vehicle Theft":           {"gravity": "Non-Heinous", "weight": 5.0,  "sections": [("BNS", "303"), ("IPC", "379")]},
    "Cheating":                {"gravity": "Non-Heinous", "weight": 14.0, "sections": [("BNS", "318"), ("IPC", "420")]},
    "Criminal Breach of Trust":{"gravity": "Non-Heinous", "weight": 2.0,  "sections": [("IPC", "406")]},
    "Forgery":                 {"gravity": "Non-Heinous", "weight": 1.5,  "sections": [("IPC", "465")]},
    "Cyber Fraud":             {"gravity": "Non-Heinous", "weight": 6.5,  "sections": [("IT", "66"), ("IT", "66D")]},
    "Cyber Harassment":        {"gravity": "Non-Heinous", "weight": 2.8,  "sections": [("IT", "67")]},
    "Data Theft":              {"gravity": "Non-Heinous", "weight": 1.0,  "sections": [("IT", "43"), ("IT", "72")]},
    "Impersonation":           {"gravity": "Non-Heinous", "weight": 1.4,  "sections": [("IT", "66C")]},
    "Possession (Small)":      {"gravity": "Non-Heinous", "weight": 2.2,  "sections": [("NDPS", "20")]},
    "Commercial Quantity":     {"gravity": "Heinous",     "weight": 0.6,  "sections": [("NDPS", "20"), ("NDPS", "27A")]},
    "Peddling":                {"gravity": "Non-Heinous", "weight": 0.4,  "sections": [("NDPS", "20")]},
    "Rash Driving":            {"gravity": "Petty",       "weight": 5.0,  "sections": [("MV", "184")]},
    "Drunk Driving":           {"gravity": "Petty",       "weight": 2.0,  "sections": [("MV", "185")]},
    "Hit and Run":             {"gravity": "Non-Heinous", "weight": 1.0,  "sections": [("MV", "134")]},
    "Public Nuisance":         {"gravity": "Petty",       "weight": 3.0,  "sections": [("BNS", "296")]},
    "Missing Person":          {"gravity": "Non-Heinous", "weight": 4.1,  "sections": [("MP-Reg", "1")]},
    "Riot":                    {"gravity": "Non-Heinous", "weight": 0.7,  "sections": [("IPC", "147")]},
}

ACTS = [
    ("BNS",   "Bharatiya Nyaya Sanhita 2023",     "BNS"),
    ("IPC",   "Indian Penal Code 1860",            "IPC"),
    ("IT",    "Information Technology Act 2000",   "IT Act"),
    ("NDPS",  "Narcotic Drugs & Psychotropic Substances Act 1985", "NDPS"),
    ("MV",    "Motor Vehicles Act 1988",           "MV Act"),
    ("POCSO", "Protection of Children from Sexual Offences 2012",  "POCSO"),
    ("PWDVA", "Protection of Women from Domestic Violence 2005",   "PWDVA"),
    ("MP-Reg","Missing Persons Registry",          "MP Reg"),
]

UNIT_TYPES = [
    (1, "Police Commissionerate", "City",     1),
    (2, "District Police HQ",     "District", 2),
    (3, "Sub-Division",           "District", 3),
    (4, "Circle",                 "District", 4),
    (5, "Police Station",         "District", 5),
    (6, "Outpost",                "District", 6),
]

RANKS = [
    (1, "DGP",       1), (2, "ADGP", 2), (3, "IGP", 3), (4, "DIG", 4),
    (5, "SP",        5), (6, "ASP",  6), (7, "DySP", 7),
    (8, "Inspector", 8), (9, "Sub-Inspector", 9), (10, "ASI", 10),
    (11, "Head Constable", 11), (12, "Constable", 12),
]

DESIGNATIONS = [
    (1, "Superintendent of Police",    1),
    (2, "Station House Officer",       2),
    (3, "Investigating Officer",       3),
    (4, "Duty Officer",                4),
    (5, "Beat Constable",              5),
    (6, "Cyber Cell",                  6),
]

CASTES = [
    (1, "General"), (2, "OBC"), (3, "SC"), (4, "ST"),
]
RELIGIONS = [
    (1, "Hindu"), (2, "Muslim"), (3, "Christian"),
    (4, "Sikh"), (5, "Jain"), (6, "Buddhist"), (7, "Other"),
]
OCCUPATIONS = [
    (1, "Farmer"), (2, "Labourer"), (3, "Business"), (4, "Government Employee"),
    (5, "Private Employee"), (6, "Student"), (7, "Homemaker"), (8, "Unemployed"),
    (9, "Self-Employed"), (10, "Driver"), (11, "Shopkeeper"),
]

# Character resources.
FIRST_NAMES = [
    "Aditya", "Aishwarya", "Arjun", "Ananya", "Bharath", "Chethan", "Deepa",
    "Divya", "Ganesh", "Harish", "Indira", "Jayanth", "Kavya", "Kiran",
    "Lakshmi", "Manoj", "Meera", "Nagaraj", "Nikhil", "Pavan", "Pooja",
    "Rahul", "Rajesh", "Ramya", "Ravi", "Rekha", "Sandeep", "Sanjay",
    "Shilpa", "Shivakumar", "Sowmya", "Sudha", "Suresh", "Uma", "Vasanth",
    "Venkatesh", "Vidya", "Vinay", "Yogesh", "Zaheer", "Farhan", "Nazeer",
    "Imran", "Roshan", "Sameer",
]
LAST_NAMES = [
    "Gowda", "Rao", "Reddy", "Shetty", "Naik", "Hegde", "Kumar", "Prasad",
    "Bhat", "Iyer", "Nayak", "Kulkarni", "Patil", "Deshpande", "Joshi",
    "Murthy", "Acharya", "Rajanna", "Shastri", "Krishnappa", "Khan", "Shaikh",
    "Pinto", "D'Souza",
]

ALIASES = ["Chikka", "Dodda", "Kaddi", "Ganja", "Silent", "Bullet", "Chotu", "Kala"]

MO_BY_CLASS = {
    "Property Crimes":      ["night-break-in", "chain-snatching", "pickpocket", "shop-break", "vehicle-theft"],
    "Crimes Against Body":  ["group-assault", "premeditated", "sudden-provocation", "gang-rivalry"],
    "Crimes Against Women": ["domestic", "workplace", "acquaintance", "stranger"],
    "Economic Offences":    ["fake-doc", "false-invoice", "identity-fraud"],
    "Cyber Crimes":         ["upi-fraud", "otp-phish", "job-scam", "sextortion", "loan-app"],
    "Narcotic Offences":    ["street-sale", "commercial", "peddling", "possession"],
    "Traffic Offences":     ["hit-and-run", "drunk-driving", "overspeeding"],
    "Other IPC":            ["general", "runaway", "elopement"],
}
WEAPONS_BY_CLASS = {
    "Crimes Against Body":  ["knife", "stick", "sickle", "firearm", "stone", None],
    "Crimes Against Women": [None, "knife"],
    "Property Crimes":      [None, None, "knife", "iron-rod"],
    "Economic Offences":    [None],
    "Cyber Crimes":         [None],
    "Narcotic Offences":    [None],
    "Traffic Offences":     [None],
    "Other IPC":            [None, "stick"],
}
LOCATION_TYPES = ["Street", "Residence", "Commercial", "Highway", "Online", "Public"]


# ---------- helpers -----------------------------------------------------------------

def jitter(lat: float, lng: float, km: float, rng: random.Random) -> tuple[float, float]:
    r = rng.random() * km / 111.0
    theta = rng.random() * 2 * math.pi
    return lat + r * math.cos(theta), lng + r * math.sin(theta) / max(0.5, math.cos(math.radians(lat)))


def weighted_choice(items, weights, rng):
    return rng.choices(items, weights=weights, k=1)[0]


def person_name(rng: random.Random) -> tuple[str, str]:
    return rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)


def crime_no(cat_code: int, district_id: int, unit_id: int, year: int, serial: int) -> str:
    return f"{cat_code}{district_id:04d}{unit_id:04d}{year:04d}{serial:05d}"


# ---------- main --------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/ksp.db")
    ap.add_argument("--firs", type=int, default=25000)
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--stations-per-district", type=int, default=25)
    args = ap.parse_args()

    rng = random.Random(SEED)
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    conn.executescript(Path("data/schema.sql").read_text())
    cur = conn.cursor()

    # ---- Masters ----
    for sid, sname in STATES:
        cur.execute("INSERT INTO State(StateID,StateName) VALUES(?,?)", (sid, sname))

    district_lookup: dict[str, int] = {}
    d_id = 4300  # KA district ids start at 4300 (arbitrary, keeps CrimeNo 4-digit)
    for name, zone, lat, lng, pop, urban, lit in KA_DISTRICTS:
        d_id += 1
        district_lookup[name] = d_id
        cur.execute(
            "INSERT INTO District(DistrictID,DistrictName,StateID,HqLat,HqLng,Population,UrbanPct,LiteracyPct,Zone) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (d_id, name, KARNATAKA_ID, lat, lng, pop, urban, lit, zone),
        )
    # Out-of-state districts (for arrest locations).
    oos_district_ids: list[tuple[int, int, float, float]] = []
    for name, state_id, lat, lng in OUT_OF_STATE_DISTRICTS:
        d_id += 1
        oos_district_ids.append((d_id, state_id, lat, lng))
        cur.execute(
            "INSERT INTO District(DistrictID,DistrictName,StateID,HqLat,HqLng) VALUES(?,?,?,?,?)",
            (d_id, name, state_id, lat, lng),
        )

    for ut in UNIT_TYPES:
        cur.execute("INSERT INTO UnitType(UnitTypeID,UnitTypeName,CityDistState,Hierarchy) VALUES(?,?,?,?)", ut)

    for r in RANKS:
        cur.execute("INSERT INTO Rank(RankID,RankName,Hierarchy) VALUES(?,?,?)", r)
    for d in DESIGNATIONS:
        cur.execute("INSERT INTO Designation(DesignationID,DesignationName,SortOrder) VALUES(?,?,?)", d)

    for cid, cname in CASTES:
        cur.execute("INSERT INTO CasteMaster(caste_master_id,caste_master_name) VALUES(?,?)", (cid, cname))
    for rid, rname in RELIGIONS:
        cur.execute("INSERT INTO ReligionMaster(ReligionID,ReligionName) VALUES(?,?)", (rid, rname))
    for oid, oname in OCCUPATIONS:
        cur.execute("INSERT INTO OccupationMaster(OccupationID,OccupationName) VALUES(?,?)", (oid, oname))

    cat_ids: dict[str, tuple[int, int]] = {}  # name -> (id, code)
    for i, (name, code) in enumerate(CASE_CATEGORIES, start=1):
        cur.execute("INSERT INTO CaseCategory(CaseCategoryID,LookupValue,CategoryCode) VALUES(?,?,?)", (i, name, code))
        cat_ids[name] = (i, code)

    gravity_ids: dict[str, int] = {}
    for i, name in enumerate(GRAVITY, start=1):
        cur.execute("INSERT INTO GravityOffence(GravityOffenceID,LookupValue) VALUES(?,?)", (i, name))
        gravity_ids[name] = i

    status_ids: dict[str, int] = {}
    for i, name in enumerate(CASE_STATUSES, start=1):
        cur.execute("INSERT INTO CaseStatusMaster(CaseStatusID,CaseStatusName) VALUES(?,?)", (i, name))
        status_ids[name] = i

    # Acts / sections.
    for code, desc, short in ACTS:
        cur.execute("INSERT INTO Act(ActCode,ActDescription,ShortName) VALUES(?,?,?)", (code, desc, short))
    # Insert distinct (act, section) pairs from SUBHEAD_META.
    seen_sections = set()
    for meta in SUBHEAD_META.values():
        for act, sec in meta["sections"]:
            if (act, sec) not in seen_sections:
                cur.execute("INSERT OR IGNORE INTO Section(ActCode,SectionCode,SectionDescription) VALUES(?,?,?)",
                            (act, sec, f"{act} sec {sec}"))
                seen_sections.add((act, sec))

    # Crime heads / subheads.
    head_ids: dict[str, int] = {}
    subhead_ids: dict[str, int] = {}
    for hi, (hname, subs) in enumerate(CRIME_HEADS.items(), start=1):
        cur.execute("INSERT INTO CrimeHead(CrimeHeadID,CrimeGroupName) VALUES(?,?)", (hi, hname))
        head_ids[hname] = hi
        for si, sname in enumerate(subs, start=1):
            sid_ = len(subhead_ids) + 1
            cur.execute(
                "INSERT INTO CrimeSubHead(CrimeSubHeadID,CrimeHeadID,CrimeHeadName,SeqID) VALUES(?,?,?,?)",
                (sid_, hi, sname, si),
            )
            subhead_ids[sname] = sid_
            for act, sec in SUBHEAD_META[sname]["sections"]:
                cur.execute(
                    "INSERT OR IGNORE INTO CrimeHeadActSection(CrimeHeadID,ActCode,SectionCode) VALUES(?,?,?)",
                    (hi, act, sec),
                )

    # ---- Units (police stations, hierarchical) ----
    unit_id = 0
    stations: list[tuple[int, int, float, float]] = []  # (unit_id, district_id, lat, lng)
    for name, zone, hq_lat, hq_lng, pop, urban, lit in KA_DISTRICTS:
        did = district_lookup[name]
        # 1 district HQ per district.
        unit_id += 1
        hq_unit = unit_id
        cur.execute(
            "INSERT INTO Unit(UnitID,UnitName,TypeID,ParentUnit,StateID,DistrictID,Lat,Lng) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (hq_unit, f"{name} District HQ", 2, None, KARNATAKA_ID, did, hq_lat, hq_lng),
        )
        # Police stations.
        n_stations = max(6, int(args.stations_per_district * (pop / 2_000_000)))
        for k in range(n_stations):
            unit_id += 1
            slat, slng = jitter(hq_lat, hq_lng, km=25 * (1 - urban * 0.5), rng=rng)
            st_name = f"{name} PS-{k+1}"
            cur.execute(
                "INSERT INTO Unit(UnitID,UnitName,TypeID,ParentUnit,StateID,DistrictID,Lat,Lng) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (unit_id, st_name, 5, hq_unit, KARNATAKA_ID, did, slat, slng),
            )
            stations.append((unit_id, did, slat, slng))

    # Courts — 2..5 per district.
    court_id = 0
    district_courts: dict[int, list[int]] = {}
    for name, _z, _lat, _lng, _p, _u, _l in KA_DISTRICTS:
        did = district_lookup[name]
        n = rng.randint(2, 5)
        district_courts[did] = []
        for k in range(n):
            court_id += 1
            cur.execute(
                "INSERT INTO Court(CourtID,CourtName,DistrictID,StateID) VALUES(?,?,?,?)",
                (court_id, f"{name} JMFC-{k+1}", did, KARNATAKA_ID),
            )
            district_courts[did].append(court_id)

    # Employees — IOs, SHOs, constables.
    employee_ids_per_unit: dict[int, list[int]] = {}
    io_employee_ids: list[int] = []
    emp_id = 0
    for st_id, did, _lat, _lng in stations:
        # 1 SHO, 3-6 IOs, plus constables (we track only IOs & SHO for the schema).
        n_ios = rng.randint(3, 6)
        employee_ids_per_unit[st_id] = []
        for role_idx in range(n_ios + 1):
            emp_id += 1
            first, last = person_name(rng)
            rank_id = 8 if role_idx == 0 else rng.choice([9, 10, 11])
            desig_id = 2 if role_idx == 0 else 3
            dob = datetime(1980, 1, 1) + timedelta(days=rng.randint(0, 365 * 20))
            appt = dob + timedelta(days=365 * rng.randint(22, 40))
            cur.execute(
                "INSERT INTO Employee(EmployeeID,DistrictID,UnitID,RankID,DesignationID,KGID,FirstName,LastName,"
                "EmployeeDOB,GenderID,BloodGroupID,PhysicallyChallenged,AppointmentDate) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (emp_id, did, st_id, rank_id, desig_id, f"KGID-{emp_id:06d}", first, last,
                 dob.date().isoformat(), rng.choice(["M", "F"]), rng.choice(["A+", "B+", "O+", "AB+"]),
                 0, appt.date().isoformat()),
            )
            employee_ids_per_unit[st_id].append(emp_id)
            if desig_id == 3:
                io_employee_ids.append(emp_id)

    # ---- Cast synthetic offender + victim identities ----
    n_offenders = max(2500, args.firs // 8)
    n_victims   = max(6000, args.firs // 3)
    offender_identities: list[dict] = []
    victim_identities:   list[dict] = []
    for _ in range(n_offenders):
        first, last = person_name(rng)
        offender_identities.append({
            "name": f"{first} {last}",
            "gender": rng.choices(["M", "F", "T"], weights=[0.86, 0.13, 0.01])[0],
            "age": max(15, min(70, int(rng.gauss(29, 10)))),
            "district_id": rng.choices(
                [district_lookup[d[0]] for d in KA_DISTRICTS],
                weights=[d[4] for d in KA_DISTRICTS],
            )[0],
            "alias": rng.choice(ALIASES) + " " + first if rng.random() < 0.12 else None,
        })
    for _ in range(n_victims):
        first, last = person_name(rng)
        victim_identities.append({
            "name": f"{first} {last}",
            "gender": rng.choices(["M", "F", "T"], weights=[0.55, 0.44, 0.01])[0],
            "age": max(10, min(85, int(rng.gauss(34, 15)))),
        })

    prolific = rng.sample(range(len(offender_identities)), k=max(60, n_offenders // 30))
    # Gangs (co-offender network seeds).
    gangs: list[list[int]] = []
    remaining = list(prolific)
    rng.shuffle(remaining)
    while len(remaining) >= 3:
        size = rng.randint(3, 6)
        gangs.append(remaining[:size])
        remaining = remaining[size:]

    # ---- Generate cases (CaseMaster + children) ----
    end = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=30 * args.months)
    total_seconds = int((end - start).total_seconds())

    subhead_names = list(SUBHEAD_META.keys())
    subhead_weights = [SUBHEAD_META[n]["weight"] for n in subhead_names]
    # Reverse lookup: subhead -> head name.
    subhead_head = {}
    for hname, subs in CRIME_HEADS.items():
        for s in subs:
            subhead_head[s] = hname

    case_id = 0
    accused_id = 0
    victim_id = 0
    complainant_id = 0
    arrest_id = 0
    cs_id = 0
    # Serial per (station, year, category_code).
    serial: dict[tuple[int, int, int], int] = {}

    district_weights = [pop * (1 + urban) for _n, _z, _lat, _lng, pop, urban, _l in KA_DISTRICTS]

    for _ in range(args.firs):
        case_id += 1

        # Pick district and station.
        dname = weighted_choice([d[0] for d in KA_DISTRICTS], district_weights, rng)
        did = district_lookup[dname]
        d_stations = [s for s in stations if s[1] == did]
        st = rng.choice(d_stations)
        st_id, _d, s_lat, s_lng = st

        # Occurrence time — with cyber-fraud uptick in the last 90 days.
        occurred_ts = start + timedelta(seconds=rng.randint(0, total_seconds))
        recent = (end - occurred_ts).days < 90

        subw = list(subhead_weights)
        for i, s in enumerate(subhead_names):
            if recent and s == "Cyber Fraud":
                subw[i] *= 2.6
            if recent and s == "Burglary" and _zone_of(dname) == "South":
                subw[i] *= 1.5
        subhead = rng.choices(subhead_names, weights=subw, k=1)[0]
        head = subhead_head[subhead]
        head_id = head_ids[head]
        subhead_id = subhead_ids[subhead]
        gravity_id = gravity_ids[SUBHEAD_META[subhead]["gravity"]]

        # Time-of-day skew.
        if head == "Property Crimes":     hour = int(rng.gauss(23, 3)) % 24
        elif head == "Crimes Against Body": hour = int(rng.gauss(20, 4)) % 24
        elif head == "Cyber Crimes":      hour = int(rng.gauss(14, 5)) % 24
        else:                             hour = rng.randint(6, 22)
        occurred_ts = occurred_ts.replace(hour=hour, minute=rng.randint(0, 59))

        # Hotspot clustering — 8% of incidents cluster within 400m of a random hot point.
        if rng.random() < 0.08:
            hot_lat, hot_lng = jitter(s_lat, s_lng, km=1.5, rng=rng)
            lat, lng = jitter(hot_lat, hot_lng, km=0.4, rng=rng)
        else:
            lat, lng = jitter(s_lat, s_lng, km=6, rng=rng)

        mo = rng.choice(MO_BY_CLASS[head])
        weapon = rng.choice(WEAPONS_BY_CLASS[head])
        status_name = rng.choices(list(status_ids.keys()),
                                   weights=[0.28, 0.32, 0.24, 0.10, 0.06])[0]
        status_id = status_ids[status_name]
        court_id = rng.choice(district_courts[did]) if status_name in ("ChargeSheeted", "Pending Trial", "PendingBeforeCourt") else None
        info_ts = occurred_ts + timedelta(minutes=rng.randint(10, 60 * 24))
        reg_ts   = info_ts + timedelta(minutes=rng.randint(30, 60 * 12))
        year = reg_ts.year

        # Case category — mostly FIR, some UDR/PAR/ZeroFIR.
        cat_name = rng.choices(["FIR", "UDR", "PAR", "ZeroFIR"], weights=[0.88, 0.06, 0.04, 0.02])[0]
        cat_id, cat_code = cat_ids[cat_name]
        key = (st_id, year, cat_code)
        serial[key] = serial.get(key, 0) + 1
        cno = crime_no(cat_code, did, st_id, year, serial[key])
        cnum = cno[-9:]

        # SHO / IO for this case.
        emp_pool = employee_ids_per_unit[st_id]
        io = rng.choice(emp_pool[1:]) if len(emp_pool) > 1 else emp_pool[0]

        cur.execute(
            "INSERT INTO CaseMaster("
            "CaseMasterID,CrimeNo,CaseNo,CrimeRegisteredDate,PolicePersonID,PoliceStationID,"
            "CaseCategoryID,GravityOffenceID,CrimeMajorHeadID,CrimeMinorHeadID,CaseStatusID,CourtID,"
            "IncidentFromDate,IncidentToDate,InfoReceivedPSDate,latitude,longitude,BriefFacts,"
            "modus_operandi,weapon) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id, cno, cnum, reg_ts.date().isoformat(), io, st_id,
                cat_id, gravity_id, head_id, subhead_id, status_id, court_id,
                occurred_ts.isoformat(timespec="seconds"),
                (occurred_ts + timedelta(minutes=rng.randint(0, 120))).isoformat(timespec="seconds"),
                info_ts.isoformat(timespec="seconds"),
                lat, lng,
                f"{head}: {subhead} reported at {mo}.",
                mo, weapon,
            ),
        )

        # Occurrence side-table.
        cur.execute(
            "INSERT INTO Inv_OccuranceTime(CaseMasterID,OccurredWeekday,OccurredHour,LocationType) VALUES(?,?,?,?)",
            (case_id, occurred_ts.weekday(), occurred_ts.hour, rng.choice(LOCATION_TYPES)),
        )

        # Act/Section associations for the subhead.
        for order_i, (act, sec) in enumerate(SUBHEAD_META[subhead]["sections"], start=1):
            cur.execute(
                "INSERT OR IGNORE INTO ActSectionAssociation("
                "CaseMasterID,ActID,SectionID,ActOrderID,SectionOrderID) VALUES(?,?,?,?,?)",
                (case_id, act, sec, 1, order_i),
            )

        # Complainant (1 per case for FIRs; 0 for UDR).
        if cat_name != "UDR":
            complainant_id += 1
            v = rng.choice(victim_identities)
            cur.execute(
                "INSERT INTO ComplainantDetails("
                "ComplainantID,CaseMasterID,ComplainantName,AgeYear,OccupationID,ReligionID,CasteID,GenderID) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (complainant_id, case_id, v["name"], v["age"],
                 rng.choice([o[0] for o in OCCUPATIONS]),
                 rng.choices([r[0] for r in RELIGIONS], weights=[0.82,0.11,0.03,0.01,0.01,0.01,0.01])[0],
                 rng.choice([c[0] for c in CASTES]),
                 v["gender"]),
            )

        # Victim(s).
        n_v = 1 if rng.random() < 0.85 else 2
        vic_indices = rng.sample(range(len(victim_identities)), k=n_v)
        for vi in vic_indices:
            victim_id += 1
            v = victim_identities[vi]
            is_police = 1 if rng.random() < 0.02 else 0
            cur.execute(
                "INSERT INTO Victim(VictimMasterID,CaseMasterID,VictimName,AgeYear,GenderID,VictimPolice) "
                "VALUES(?,?,?,?,?,?)",
                (victim_id, case_id, v["name"], v["age"], v["gender"], is_police),
            )

        # Accused — draw offender identities.
        r = rng.random()
        if r < 0.60:
            acc_idxs = [rng.randrange(n_offenders)]
        elif r < 0.85:
            if rng.random() < 0.55 and gangs:
                g = rng.choice(gangs)
                acc_idxs = rng.sample(g, k=min(2, len(g)))
            else:
                acc_idxs = rng.sample(range(n_offenders), k=2)
        else:
            if rng.random() < 0.70 and gangs:
                g = rng.choice(gangs)
                acc_idxs = rng.sample(g, k=min(len(g), rng.randint(3, 5)))
            else:
                acc_idxs = rng.sample(range(n_offenders), k=rng.randint(3, 5))
        # Prolific replacement.
        if rng.random() < 0.22:
            acc_idxs[0] = rng.choice(prolific)

        for i, ox in enumerate(acc_idxs, start=1):
            accused_id += 1
            o = offender_identities[ox]
            cur.execute(
                "INSERT INTO Accused("
                "AccusedMasterID,CaseMasterID,AccusedName,AgeYear,GenderID,PersonID,person_link_id,home_district_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (accused_id, case_id, o["name"], o["age"], o["gender"], f"A{i}", ox, o["district_id"]),
            )

            # Arrest / surrender — 70% chance the accused was arrested.
            if rng.random() < 0.70:
                arrest_id += 1
                # 10% cross-border arrests for prolific / repeat offenders.
                if ox in prolific and rng.random() < 0.25:
                    oos = rng.choice(oos_district_ids)
                    arr_district = oos[0]
                    arr_state = oos[1]
                    arr_ps = st_id  # PS handling the arrest is still KA — request executed by KA police.
                else:
                    arr_district = did
                    arr_state = KARNATAKA_ID
                    arr_ps = st_id
                arr_type = 1 if rng.random() < 0.9 else 2
                arr_date = (reg_ts + timedelta(days=rng.randint(0, 60))).date().isoformat()
                cur.execute(
                    "INSERT INTO ArrestSurrender("
                    "ArrestSurrenderID,CaseMasterID,ArrestSurrenderTypeID,ArrestSurrenderDate,"
                    "ArrestSurrenderStateId,ArrestSurrenderDistrictId,PoliceStationID,IOID,CourtID,"
                    "AccusedMasterID,IsAccused,IsComplainantAccused) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (arrest_id, case_id, arr_type, arr_date, arr_state, arr_district,
                     arr_ps, io, court_id, accused_id, 1, 0),
                )

        # Chargesheet if status is ChargeSheeted / Pending Trial / Closed.
        if status_name in ("ChargeSheeted", "Pending Trial", "PendingBeforeCourt"):
            cs_id += 1
            cs_type = rng.choices(["A", "B", "C"], weights=[0.85, 0.08, 0.07])[0]
            cs_date = (reg_ts + timedelta(days=rng.randint(30, 180))).date().isoformat()
            cur.execute(
                "INSERT INTO ChargesheetDetails(CSID,CaseMasterID,csdate,cstype,PolicePersonID) VALUES(?,?,?,?,?)",
                (cs_id, case_id, cs_date, cs_type, io),
            )
        elif status_name == "Closed" and rng.random() < 0.4:
            cs_id += 1
            cs_type = rng.choice(["B", "C"])   # False or Undetected
            cs_date = (reg_ts + timedelta(days=rng.randint(30, 180))).date().isoformat()
            cur.execute(
                "INSERT INTO ChargesheetDetails(CSID,CaseMasterID,csdate,cstype,PolicePersonID) VALUES(?,?,?,?,?)",
                (cs_id, case_id, cs_date, cs_type, io),
            )

    # Repeat-offender flag — top 15% of offenders by case count.
    threshold_row = cur.execute("""
        SELECT MIN(cnt) FROM (
            SELECT COUNT(DISTINCT CaseMasterID) AS cnt
            FROM Accused
            WHERE person_link_id IS NOT NULL
            GROUP BY person_link_id
            ORDER BY cnt DESC
            LIMIT (SELECT MAX(1, COUNT(DISTINCT person_link_id)/7) FROM Accused WHERE person_link_id IS NOT NULL)
        )
    """).fetchone()
    repeat_threshold = threshold_row[0] if threshold_row and threshold_row[0] else 5
    cur.execute("""
        UPDATE Accused SET is_repeat = 1
        WHERE person_link_id IN (
            SELECT person_link_id FROM Accused
            WHERE person_link_id IS NOT NULL
            GROUP BY person_link_id
            HAVING COUNT(DISTINCT CaseMasterID) >= ?
        )
    """, (repeat_threshold,))
    print(f"  repeat_threshold    {repeat_threshold} cases (top ~15% offenders)")

    # ---- Precompute baselines & weekly counts (for anomalies + prediction) ----
    # Monthly baselines per (district, subhead, calendar month).
    agg = cur.execute("""
        SELECT u.DistrictID, cm.CrimeMinorHeadID,
               strftime('%m', cm.IncidentFromDate) AS m,
               strftime('%Y-%m', cm.IncidentFromDate) AS ym,
               COUNT(*) AS c
        FROM CaseMaster cm JOIN Unit u ON u.UnitID = cm.PoliceStationID
        WHERE cm.CrimeMinorHeadID IS NOT NULL
        GROUP BY u.DistrictID, cm.CrimeMinorHeadID, ym
    """).fetchall()
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for did, sid_, m, _ym, c in agg:
        buckets.setdefault((did, sid_, int(m)), []).append(c)
    for (did, sid_, m), counts in buckets.items():
        if len(counts) >= 2:
            mean = statistics.fmean(counts); sd = statistics.pstdev(counts)
        else:
            mean = counts[0] if counts else 0.0; sd = 0.0
        cur.execute(
            "INSERT OR REPLACE INTO monthly_baseline(district_id,subhead_id,month,mean_count,stddev_count) VALUES(?,?,?,?,?)",
            (did, sid_, m, mean, sd),
        )

    # Weekly counts per (unit, head) — used by the forecasting endpoint.
    cur.execute("""
        INSERT INTO weekly_counts(unit_id, head_id, week_start, count)
        SELECT cm.PoliceStationID,
               cm.CrimeMajorHeadID,
               date(cm.IncidentFromDate, 'weekday 0', '-6 days') AS week_start,
               COUNT(*) AS c
        FROM CaseMaster cm
        WHERE cm.CrimeMajorHeadID IS NOT NULL
        GROUP BY cm.PoliceStationID, cm.CrimeMajorHeadID, week_start
    """)

    conn.commit()
    counts = {
        "State":            cur.execute("SELECT COUNT(*) FROM State").fetchone()[0],
        "District":         cur.execute("SELECT COUNT(*) FROM District").fetchone()[0],
        "Unit":             cur.execute("SELECT COUNT(*) FROM Unit").fetchone()[0],
        "Court":            cur.execute("SELECT COUNT(*) FROM Court").fetchone()[0],
        "Employee":         cur.execute("SELECT COUNT(*) FROM Employee").fetchone()[0],
        "CrimeHead":        cur.execute("SELECT COUNT(*) FROM CrimeHead").fetchone()[0],
        "CrimeSubHead":     cur.execute("SELECT COUNT(*) FROM CrimeSubHead").fetchone()[0],
        "Act":              cur.execute("SELECT COUNT(*) FROM Act").fetchone()[0],
        "Section":          cur.execute("SELECT COUNT(*) FROM Section").fetchone()[0],
        "CaseMaster":       cur.execute("SELECT COUNT(*) FROM CaseMaster").fetchone()[0],
        "Victim":           cur.execute("SELECT COUNT(*) FROM Victim").fetchone()[0],
        "Accused":          cur.execute("SELECT COUNT(*) FROM Accused").fetchone()[0],
        "ArrestSurrender":  cur.execute("SELECT COUNT(*) FROM ArrestSurrender").fetchone()[0],
        "Chargesheet":      cur.execute("SELECT COUNT(*) FROM ChargesheetDetails").fetchone()[0],
        "RepeatAccused":    cur.execute("SELECT COUNT(*) FROM Accused WHERE is_repeat=1").fetchone()[0],
        "CrossBorderArrests": cur.execute("SELECT COUNT(*) FROM ArrestSurrender WHERE ArrestSurrenderStateId != 29").fetchone()[0],
        "weekly_counts":    cur.execute("SELECT COUNT(*) FROM weekly_counts").fetchone()[0],
    }
    print(f"Generated database at {db_path}")
    for k, v in counts.items():
        print(f"  {k:<20} {v:,}")
    conn.close()


def _zone_of(district_name: str) -> str:
    for name, zone, *_ in KA_DISTRICTS:
        if name == district_name:
            return zone
    return "South"


if __name__ == "__main__":
    main()
