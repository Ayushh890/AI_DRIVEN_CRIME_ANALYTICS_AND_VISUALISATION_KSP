"""
Authentication for the KSP CIP.

Design choices
==============
* SQLite `users` + `sessions` tables (created on first server boot).
* PBKDF2-HMAC-SHA256 password hashing (Python stdlib — no C-extension deps
  so it works cleanly on any Catalyst AppSail runtime).
* Opaque server-side session tokens (secrets.token_urlsafe), stored in the DB
  and delivered to the client as an HttpOnly Secure cookie.
* Constant-time comparisons where relevant.
* Role field on every user: 'admin' | 'scrb' | 'sp' | 'sho' | 'user'.
  Registration defaults to 'user'; the first-registered account gets 'admin'.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Cookie, HTTPException, Response
from pydantic import BaseModel, EmailStr, Field

SESSION_COOKIE = "ksp_sid"
SESSION_TTL_DAYS = 14

PBKDF2_ITER = 200_000     # ~120ms on a modern box, tolerable per-login
SALT_BYTES = 16


def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema(db_path: str) -> None:
    """Create users + sessions tables if missing. Called once at startup."""
    with _get_conn(db_path) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT UNIQUE NOT NULL,
                full_name     TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'user',
                kgid          TEXT UNIQUE,          -- Karnataka Govt ID
                employee_id   INTEGER,              -- FK → crime DB Employee(EmployeeID)
                rank_name     TEXT,
                designation   TEXT,
                unit_name     TEXT,
                district_name TEXT,
                phone         TEXT,
                photo_data_url TEXT,                -- base64 data URL (pilot; use File Store in prod)
                bio           TEXT,
                created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at  TEXT NOT NULL,
                ip          TEXT,
                user_agent  TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS ix_sessions_expires ON sessions(expires_at);
        """)


def lookup_kgid(crime_db_path: str, kgid: str) -> dict | None:
    """Verify a KGID against the Employee table. Returns denormalized officer
    metadata if the KGID is valid, else None."""
    kgid = (kgid or "").strip().upper()
    if not kgid.startswith("KGID-"):
        return None
    try:
        with sqlite3.connect(crime_db_path) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("""
                SELECT e.EmployeeID, e.KGID, e.FirstName, e.LastName,
                       r.RankName, dg.DesignationName, u.UnitName, d.DistrictName
                FROM Employee e
                LEFT JOIN Rank r         ON r.RankID = e.RankID
                LEFT JOIN Designation dg ON dg.DesignationID = e.DesignationID
                LEFT JOIN Unit u         ON u.UnitID = e.UnitID
                LEFT JOIN District d     ON d.DistrictID = e.DistrictID
                WHERE UPPER(e.KGID) = ?
            """, (kgid,)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


# --------------------------------------------------------------------------
# Password hashing (PBKDF2-HMAC-SHA256, stdlib-only)
# --------------------------------------------------------------------------
def hash_password(pwd: str, iterations: int = PBKDF2_ITER) -> str:
    if not isinstance(pwd, str) or len(pwd) < 6:
        raise ValueError("password must be at least 6 characters")
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(pwd: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iters)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except (ValueError, TypeError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(got, expected)


# --------------------------------------------------------------------------
# Session management
# --------------------------------------------------------------------------
def _new_sid() -> str:
    return secrets.token_urlsafe(32)


def create_session(db_path: str, user_id: int, ip: str | None = None, ua: str | None = None) -> str:
    sid = _new_sid()
    expires_at = (datetime.utcnow() + timedelta(days=SESSION_TTL_DAYS)).isoformat(timespec="seconds")
    with _get_conn(db_path) as c:
        c.execute(
            "INSERT INTO sessions(session_id, user_id, expires_at, ip, user_agent) VALUES(?,?,?,?,?)",
            (sid, user_id, expires_at, ip, ua),
        )
        c.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,))
        c.commit()
    return sid


def destroy_session(db_path: str, sid: str) -> None:
    with _get_conn(db_path) as c:
        c.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        c.commit()


def load_session(db_path: str, sid: str | None) -> dict | None:
    if not sid:
        return None
    with _get_conn(db_path) as c:
        row = c.execute(
            """SELECT s.session_id, s.user_id, s.expires_at,
                      u.email, u.full_name, u.role, u.designation, u.unit_name
               FROM sessions s
               JOIN users u ON u.user_id = s.user_id
               WHERE s.session_id = ? AND s.expires_at > datetime('now')""",
            (sid,),
        ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# Public helpers used by main.py
# --------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterBody(BaseModel):
    email:       str = Field(..., min_length=4, max_length=200)
    password:    str = Field(..., min_length=6, max_length=128)
    kgid:        str = Field(..., min_length=5, max_length=40)      # REQUIRED — Karnataka Govt ID
    phone:       str | None = Field(default=None, max_length=20)


class LoginBody(BaseModel):
    email:    str = Field(..., min_length=4, max_length=200)
    password: str = Field(..., min_length=6, max_length=128)


def register_user(db_path: str, body: RegisterBody, crime_db_path: str) -> dict:
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "invalid email")

    # KGID whitelist check — verifies the applicant is on the payroll.
    officer = lookup_kgid(crime_db_path, body.kgid)
    if not officer:
        raise HTTPException(
            403,
            "This KGID is not in the Karnataka Police roster. Access is restricted to "
            "serving officers, investigators, and forensic personnel.",
        )
    kgid_norm = officer["KGID"]
    full_name = f"{officer['FirstName'] or ''} {officer['LastName'] or ''}".strip() or "Officer"

    with _get_conn(db_path) as c:
        existing = c.execute(
            "SELECT user_id FROM users WHERE email = ? OR kgid = ?",
            (email, kgid_norm),
        ).fetchone()
        if existing:
            raise HTTPException(409, "an account already exists for this email or KGID")

        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        role = "admin" if total == 0 else "officer"

        try:
            pwd_hash = hash_password(body.password)
        except ValueError as e:
            raise HTTPException(400, str(e))

        cur = c.execute(
            """INSERT INTO users(
                    email, full_name, password_hash, role,
                    kgid, employee_id, rank_name, designation, unit_name, district_name, phone
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                email, full_name, pwd_hash, role,
                kgid_norm, officer["EmployeeID"],
                officer.get("RankName"), officer.get("DesignationName"),
                officer.get("UnitName"), officer.get("DistrictName"),
                body.phone,
            ),
        )
        c.commit()
        uid = cur.lastrowid
        return {
            "user_id": uid, "email": email, "full_name": full_name, "role": role,
            "kgid": kgid_norm, "employee_id": officer["EmployeeID"],
            "rank_name": officer.get("RankName"), "designation": officer.get("DesignationName"),
            "unit_name": officer.get("UnitName"), "district_name": officer.get("DistrictName"),
        }


def login_user(db_path: str, body: LoginBody, ip: str | None = None, ua: str | None = None) -> tuple[dict, str]:
    email = body.email.strip().lower()
    with _get_conn(db_path) as c:
        row = c.execute(
            "SELECT user_id, email, full_name, password_hash, role, designation, unit_name FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "invalid email or password")
    user = {k: row[k] for k in ("user_id", "email", "full_name", "role", "designation", "unit_name")}
    sid = create_session(db_path, user["user_id"], ip=ip, ua=ua)
    return user, sid


def set_session_cookie(response: Response, sid: str) -> None:
    # Secure by default (Catalyst is HTTPS). Set KSP_COOKIE_SECURE=0 for
    # dev / testing over plain http.
    secure = os.environ.get("KSP_COOKIE_SECURE", "1") != "0"
    response.set_cookie(
        key=SESSION_COOKIE, value=sid,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True, samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
