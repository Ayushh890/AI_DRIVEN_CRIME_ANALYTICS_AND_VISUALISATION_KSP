-- KSP Crime Intelligence Platform — Karnataka Police FIR system schema.
-- SQLite dialect for the pilot; ports to MSSQL/Postgres by trivial type swaps
-- (INTEGER PRIMARY KEY → INT IDENTITY / SERIAL, TEXT → VARCHAR/NVARCHAR).
-- Table & column names match the KSP FIR System ER document.

PRAGMA foreign_keys = ON;

-- ============================================================================
-- Geography / organizational masters
-- ============================================================================
CREATE TABLE State (
    StateID       INTEGER PRIMARY KEY,
    StateName     TEXT NOT NULL UNIQUE,
    NationalityID INTEGER DEFAULT 1,
    Active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE District (
    DistrictID   INTEGER PRIMARY KEY,
    DistrictName TEXT NOT NULL,
    StateID      INTEGER NOT NULL REFERENCES State(StateID),
    -- Geographic centroid — used by the map. Not in the KSP spec but required
    -- for the geospatial UI; carry it here so ingest doesn't need a side table.
    HqLat        REAL,
    HqLng        REAL,
    Population   INTEGER,
    UrbanPct     REAL,
    LiteracyPct  REAL,
    Zone         TEXT,
    Active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_district_state ON District(StateID);

CREATE TABLE UnitType (
    UnitTypeID   INTEGER PRIMARY KEY,
    UnitTypeName TEXT NOT NULL,
    CityDistState TEXT,       -- 'City' / 'District' / 'State'
    Hierarchy    INTEGER,
    Active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE Unit (
    UnitID       INTEGER PRIMARY KEY,
    UnitName     TEXT NOT NULL,
    TypeID       INTEGER REFERENCES UnitType(UnitTypeID),
    ParentUnit   INTEGER REFERENCES Unit(UnitID),
    NationalityID INTEGER DEFAULT 1,
    StateID      INTEGER REFERENCES State(StateID),
    DistrictID   INTEGER REFERENCES District(DistrictID),
    Lat          REAL,
    Lng          REAL,
    Active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_unit_district ON Unit(DistrictID);
CREATE INDEX ix_unit_type ON Unit(TypeID);

CREATE TABLE Court (
    CourtID    INTEGER PRIMARY KEY,
    CourtName  TEXT NOT NULL,
    DistrictID INTEGER REFERENCES District(DistrictID),
    StateID    INTEGER REFERENCES State(StateID),
    Active     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_court_district ON Court(DistrictID);

CREATE TABLE Rank (
    RankID    INTEGER PRIMARY KEY,
    RankName  TEXT NOT NULL,
    Hierarchy INTEGER,
    Active    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE Designation (
    DesignationID   INTEGER PRIMARY KEY,
    DesignationName TEXT NOT NULL,
    SortOrder       INTEGER,
    Active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE Employee (
    EmployeeID          INTEGER PRIMARY KEY,
    DistrictID          INTEGER REFERENCES District(DistrictID),
    UnitID              INTEGER REFERENCES Unit(UnitID),
    RankID              INTEGER REFERENCES Rank(RankID),
    DesignationID       INTEGER REFERENCES Designation(DesignationID),
    KGID                TEXT UNIQUE,
    FirstName           TEXT,
    LastName            TEXT,
    EmployeeDOB         TEXT,
    GenderID            TEXT,
    BloodGroupID        TEXT,
    PhysicallyChallenged INTEGER DEFAULT 0,
    AppointmentDate     TEXT
);
CREATE INDEX ix_emp_unit ON Employee(UnitID);
CREATE INDEX ix_emp_district ON Employee(DistrictID);

-- ============================================================================
-- Person masters
-- ============================================================================
CREATE TABLE CasteMaster (
    caste_master_id   INTEGER PRIMARY KEY,
    caste_master_name TEXT NOT NULL
);
CREATE TABLE ReligionMaster (
    ReligionID   INTEGER PRIMARY KEY,
    ReligionName TEXT NOT NULL
);
CREATE TABLE OccupationMaster (
    OccupationID   INTEGER PRIMARY KEY,
    OccupationName TEXT NOT NULL
);

-- ============================================================================
-- Case / crime classification masters
-- ============================================================================
CREATE TABLE CaseCategory (
    CaseCategoryID INTEGER PRIMARY KEY,
    LookupValue    TEXT NOT NULL,       -- FIR / UDR / PAR / ZeroFIR
    CategoryCode   INTEGER NOT NULL     -- 1=FIR, 3=UDR, 4=PAR, 8=ZeroFIR
);

CREATE TABLE GravityOffence (
    GravityOffenceID INTEGER PRIMARY KEY,
    LookupValue      TEXT NOT NULL     -- Heinous / Non-Heinous / Petty
);

CREATE TABLE CaseStatusMaster (
    CaseStatusID   INTEGER PRIMARY KEY,
    CaseStatusName TEXT NOT NULL       -- UnderInvestigation / ChargeSheeted / Closed / Pending
);

CREATE TABLE CrimeHead (
    CrimeHeadID    INTEGER PRIMARY KEY,
    CrimeGroupName TEXT NOT NULL,       -- e.g. Crimes Against Body
    Active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE CrimeSubHead (
    CrimeSubHeadID INTEGER PRIMARY KEY,
    CrimeHeadID    INTEGER NOT NULL REFERENCES CrimeHead(CrimeHeadID),
    CrimeHeadName  TEXT NOT NULL,       -- e.g. Murder, Robbery
    SeqID          INTEGER
);
CREATE INDEX ix_subhead_head ON CrimeSubHead(CrimeHeadID);

CREATE TABLE Act (
    ActCode        TEXT PRIMARY KEY,    -- e.g. 'IPC', 'BNS', 'NDPS', 'IT'
    ActDescription TEXT,
    ShortName      TEXT,
    Active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE Section (
    ActCode            TEXT NOT NULL REFERENCES Act(ActCode),
    SectionCode        TEXT NOT NULL,
    SectionDescription TEXT,
    Active             INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (ActCode, SectionCode)
);

CREATE TABLE CrimeHeadActSection (
    CrimeHeadID INTEGER NOT NULL REFERENCES CrimeHead(CrimeHeadID),
    ActCode     TEXT NOT NULL,
    SectionCode TEXT NOT NULL,
    PRIMARY KEY (CrimeHeadID, ActCode, SectionCode),
    FOREIGN KEY (ActCode, SectionCode) REFERENCES Section(ActCode, SectionCode)
);

-- ============================================================================
-- Core case tables
-- ============================================================================
CREATE TABLE CaseMaster (
    CaseMasterID       INTEGER PRIMARY KEY,
    CrimeNo            TEXT NOT NULL UNIQUE,   -- 18-digit: CaseCatCode(1)+District(4)+Unit(4)+Year(4)+Serial(5)
    CaseNo             TEXT NOT NULL,          -- last 9 digits of CrimeNo
    CrimeRegisteredDate TEXT NOT NULL,
    PolicePersonID     INTEGER REFERENCES Employee(EmployeeID),
    PoliceStationID    INTEGER NOT NULL REFERENCES Unit(UnitID),
    CaseCategoryID     INTEGER NOT NULL REFERENCES CaseCategory(CaseCategoryID),
    GravityOffenceID   INTEGER REFERENCES GravityOffence(GravityOffenceID),
    CrimeMajorHeadID   INTEGER REFERENCES CrimeHead(CrimeHeadID),
    CrimeMinorHeadID   INTEGER REFERENCES CrimeSubHead(CrimeSubHeadID),
    CaseStatusID       INTEGER REFERENCES CaseStatusMaster(CaseStatusID),
    CourtID            INTEGER REFERENCES Court(CourtID),
    IncidentFromDate   TEXT NOT NULL,
    IncidentToDate     TEXT NOT NULL,
    InfoReceivedPSDate TEXT NOT NULL,
    latitude           REAL NOT NULL,
    longitude          REAL NOT NULL,
    BriefFacts         TEXT,
    -- Denormalized for query efficiency in the pilot (also useful for spatial ML).
    modus_operandi     TEXT,
    weapon             TEXT
);
CREATE INDEX ix_case_station   ON CaseMaster(PoliceStationID);
CREATE INDEX ix_case_head      ON CaseMaster(CrimeMajorHeadID);
CREATE INDEX ix_case_subhead   ON CaseMaster(CrimeMinorHeadID);
CREATE INDEX ix_case_status    ON CaseMaster(CaseStatusID);
CREATE INDEX ix_case_court     ON CaseMaster(CourtID);
CREATE INDEX ix_case_incident  ON CaseMaster(IncidentFromDate);
CREATE INDEX ix_case_geo       ON CaseMaster(latitude, longitude);
CREATE INDEX ix_case_io        ON CaseMaster(PolicePersonID);

-- One-to-one supplemental record.
CREATE TABLE Inv_OccuranceTime (
    CaseMasterID       INTEGER PRIMARY KEY REFERENCES CaseMaster(CaseMasterID),
    OccurredWeekday    INTEGER,   -- 0..6
    OccurredHour       INTEGER,   -- 0..23
    LocationType       TEXT       -- Street / Residence / Commercial / Highway / Online
);

CREATE TABLE ComplainantDetails (
    ComplainantID   INTEGER PRIMARY KEY,
    CaseMasterID    INTEGER NOT NULL REFERENCES CaseMaster(CaseMasterID),
    ComplainantName TEXT,
    AgeYear         INTEGER,
    OccupationID    INTEGER REFERENCES OccupationMaster(OccupationID),
    ReligionID      INTEGER REFERENCES ReligionMaster(ReligionID),
    CasteID         INTEGER REFERENCES CasteMaster(caste_master_id),
    GenderID        TEXT
);
CREATE INDEX ix_complainant_case ON ComplainantDetails(CaseMasterID);

CREATE TABLE Victim (
    VictimMasterID INTEGER PRIMARY KEY,
    CaseMasterID   INTEGER NOT NULL REFERENCES CaseMaster(CaseMasterID),
    VictimName     TEXT,
    AgeYear        INTEGER,
    GenderID       TEXT,
    VictimPolice   INTEGER DEFAULT 0
);
CREATE INDEX ix_victim_case ON Victim(CaseMasterID);

CREATE TABLE Accused (
    AccusedMasterID INTEGER PRIMARY KEY,
    CaseMasterID    INTEGER NOT NULL REFERENCES CaseMaster(CaseMasterID),
    AccusedName     TEXT NOT NULL,
    AgeYear         INTEGER,
    GenderID        TEXT,
    PersonID        TEXT,               -- A1, A2, ...
    -- Denormalized identity link so an accused person can be tracked across
    -- multiple cases without a full person master (KSP schema doesn't include
    -- one). This is populated by name-matching in the pilot.
    person_link_id  INTEGER,
    is_repeat       INTEGER DEFAULT 0,
    home_district_id INTEGER REFERENCES District(DistrictID)
);
CREATE INDEX ix_accused_case ON Accused(CaseMasterID);
CREATE INDEX ix_accused_link ON Accused(person_link_id);

CREATE TABLE ActSectionAssociation (
    CaseMasterID  INTEGER NOT NULL REFERENCES CaseMaster(CaseMasterID),
    ActID         TEXT NOT NULL REFERENCES Act(ActCode),
    SectionID     TEXT NOT NULL,
    ActOrderID    INTEGER,
    SectionOrderID INTEGER,
    PRIMARY KEY (CaseMasterID, ActID, SectionID)
);

CREATE TABLE ArrestSurrender (
    ArrestSurrenderID       INTEGER PRIMARY KEY,
    CaseMasterID            INTEGER NOT NULL REFERENCES CaseMaster(CaseMasterID),
    ArrestSurrenderTypeID   INTEGER NOT NULL,   -- 1=Arrest, 2=Surrender
    ArrestSurrenderDate     TEXT NOT NULL,
    ArrestSurrenderStateId  INTEGER REFERENCES State(StateID),
    ArrestSurrenderDistrictId INTEGER REFERENCES District(DistrictID),
    PoliceStationID         INTEGER REFERENCES Unit(UnitID),
    IOID                    INTEGER REFERENCES Employee(EmployeeID),
    CourtID                 INTEGER REFERENCES Court(CourtID),
    AccusedMasterID         INTEGER REFERENCES Accused(AccusedMasterID),
    IsAccused               INTEGER DEFAULT 1,
    IsComplainantAccused    INTEGER DEFAULT 0
);
CREATE INDEX ix_arr_case ON ArrestSurrender(CaseMasterID);
CREATE INDEX ix_arr_state ON ArrestSurrender(ArrestSurrenderStateId);
CREATE INDEX ix_arr_accused ON ArrestSurrender(AccusedMasterID);
CREATE INDEX ix_arr_io ON ArrestSurrender(IOID);

CREATE TABLE ChargesheetDetails (
    CSID           INTEGER PRIMARY KEY,
    CaseMasterID   INTEGER NOT NULL REFERENCES CaseMaster(CaseMasterID),
    csdate         TEXT NOT NULL,
    cstype         TEXT NOT NULL,    -- A=Chargesheet, B=False, C=Undetected
    PolicePersonID INTEGER REFERENCES Employee(EmployeeID)
);
CREATE INDEX ix_cs_case ON ChargesheetDetails(CaseMasterID);

-- ============================================================================
-- Materialized helpers for the pilot
-- ============================================================================
CREATE TABLE monthly_baseline (
    district_id INTEGER NOT NULL,
    subhead_id  INTEGER NOT NULL,
    month       INTEGER NOT NULL,   -- 1..12
    mean_count  REAL NOT NULL,
    stddev_count REAL NOT NULL,
    PRIMARY KEY (district_id, subhead_id, month)
);

CREATE TABLE weekly_counts (
    unit_id     INTEGER NOT NULL,
    head_id     INTEGER NOT NULL,
    week_start  TEXT NOT NULL,     -- Monday of the ISO week
    count       INTEGER NOT NULL,
    PRIMARY KEY (unit_id, head_id, week_start)
);
CREATE INDEX ix_weekly_head ON weekly_counts(head_id);
CREATE INDEX ix_weekly_week ON weekly_counts(week_start);
