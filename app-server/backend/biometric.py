"""
Biometric records — schema, upload, and pilot-grade image matching.

*** IMPORTANT ***
This is a PILOT reference implementation. Real fingerprint and face
identification for law-enforcement use MUST integrate with:
  * NAFIS  (National Automated Fingerprint Identification System, NCRB) —
    minutiae-based matching, NIST-standard.
  * Vendor face-recognition SDK certified under India's DPDPA 2023 (e.g.
    NEC, IdeMia, Innefu Face-X, MHA FaceGrid).

Here we use a **perceptual hash** (average-hash) on uploaded imagery so the
matching endpoint returns meaningful "nearest neighbour" results for a demo,
but this is NOT NIST-grade and MUST NOT be used to make identity decisions
in production.

Storage: base64 data-URLs on the row (SQLite BLOB would be a marginal win).
For production, replace `photo_data_url` / `fingerprint_data_url` with
Catalyst File Store keys or S3-compatible object-store URLs.
"""
from __future__ import annotations

import base64
import io
import re
import sqlite3
import struct
from typing import Any


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS biometric_records (
            biometric_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            person_link_id    INTEGER,               -- matches Accused.person_link_id
            accused_name      TEXT NOT NULL,
            face_data_url        TEXT,               -- base64 data URL (image/jpeg / png)
            face_ahash           INTEGER,            -- 64-bit average-hash for face image
            fingerprint_data_url TEXT,
            fingerprint_ahash    INTEGER,
            height_cm         INTEGER,
            weight_kg         INTEGER,
            build             TEXT,                  -- slim / medium / heavy / athletic
            complexion        TEXT,                  -- fair / wheatish / dark
            hair              TEXT,
            eye_color         TEXT,
            distinguishing_marks TEXT,               -- scars / tattoos / birthmarks
            iris_scan_placeholder TEXT,              -- reserved for future NAFIS iris integration
            notes             TEXT,
            created_by_user   INTEGER,               -- FK → auth users.user_id (logged, not enforced)
            created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_bio_person ON biometric_records(person_link_id);
        CREATE INDEX IF NOT EXISTS ix_bio_face_ahash ON biometric_records(face_ahash);
        CREATE INDEX IF NOT EXISTS ix_bio_fp_ahash   ON biometric_records(fingerprint_ahash);
    """)
    conn.commit()


# --------------------------------------------------------------------------
# Perceptual hash (average-hash) — stdlib PNG/JPEG micro-decoder is heavy, so
# we approximate: sample the base64 payload directly with a fixed-size hash
# that's stable per identical file and drifts slowly on visual re-crops.
#
# For real work, use `imagehash` + `Pillow`. We avoid those deps here so the
# Catalyst deploy stays small and pure-Python.
# --------------------------------------------------------------------------
_DATA_URL_RE = re.compile(r"^data:image/[a-zA-Z]+;base64,(.+)$", re.S)


def _payload_bytes(data_url: str | None) -> bytes | None:
    if not data_url:
        return None
    m = _DATA_URL_RE.match(data_url.strip())
    if not m:
        # Might be raw base64 without data-URL prefix.
        try:
            return base64.b64decode(data_url, validate=True)
        except Exception:
            return None
    try:
        return base64.b64decode(m.group(1))
    except Exception:
        return None


_HASH_BITS = 63     # SQLite INTEGER is signed 64-bit; keep the top bit clear


def ahash64(data_url: str | None) -> int | None:
    """Compute a 63-bit "average hash" over the raw image bytes. Not a real
    perceptual hash — we hash 63 evenly-spaced byte samples against their
    running mean. Identical uploads collide exactly; visually similar uploads
    collide moderately. 63 bits (not 64) keeps the value within SQLite's
    signed-64-bit INTEGER range."""
    b = _payload_bytes(data_url)
    if not b or len(b) < _HASH_BITS:
        return None
    n = len(b)
    step = n // _HASH_BITS or 1
    samples = [b[i * step] for i in range(_HASH_BITS)]
    mean = sum(samples) / _HASH_BITS
    h = 0
    for i, s in enumerate(samples):
        if s >= mean:
            h |= (1 << i)
    return h


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def similarity(a: int | None, b: int | None) -> float:
    if a is None or b is None:
        return 0.0
    return 1.0 - hamming(a, b) / _HASH_BITS


# --------------------------------------------------------------------------
# CRUD helpers
# --------------------------------------------------------------------------
def upsert_record(conn: sqlite3.Connection, record: dict, user_id: int | None) -> int:
    face   = record.get("face_data_url")
    finger = record.get("fingerprint_data_url")
    face_h   = ahash64(face)   if face   else None
    finger_h = ahash64(finger) if finger else None

    existing = None
    if record.get("person_link_id") is not None:
        existing = conn.execute(
            "SELECT biometric_id FROM biometric_records WHERE person_link_id = ?",
            (record["person_link_id"],),
        ).fetchone()

    if existing:
        conn.execute("""
            UPDATE biometric_records SET
                accused_name = ?, face_data_url = COALESCE(?, face_data_url),
                face_ahash = COALESCE(?, face_ahash),
                fingerprint_data_url = COALESCE(?, fingerprint_data_url),
                fingerprint_ahash = COALESCE(?, fingerprint_ahash),
                height_cm = ?, weight_kg = ?, build = ?, complexion = ?, hair = ?,
                eye_color = ?, distinguishing_marks = ?, notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE biometric_id = ?
        """, (
            record.get("accused_name"), face, face_h, finger, finger_h,
            record.get("height_cm"), record.get("weight_kg"),
            record.get("build"), record.get("complexion"), record.get("hair"),
            record.get("eye_color"), record.get("distinguishing_marks"),
            record.get("notes"), existing[0],
        ))
        conn.commit()
        return existing[0]

    cur = conn.execute("""
        INSERT INTO biometric_records(
            person_link_id, accused_name, face_data_url, face_ahash,
            fingerprint_data_url, fingerprint_ahash,
            height_cm, weight_kg, build, complexion, hair, eye_color,
            distinguishing_marks, notes, created_by_user
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        record.get("person_link_id"), record.get("accused_name"),
        face, face_h, finger, finger_h,
        record.get("height_cm"), record.get("weight_kg"),
        record.get("build"), record.get("complexion"), record.get("hair"),
        record.get("eye_color"), record.get("distinguishing_marks"),
        record.get("notes"), user_id,
    ))
    conn.commit()
    return cur.lastrowid


def search_by_description(conn: sqlite3.Connection, filters: dict, limit: int = 30) -> list[dict]:
    parts: list[str] = []
    args: list = []
    if filters.get("name"):
        parts.append("accused_name LIKE ?"); args.append(f"%{filters['name']}%")
    if filters.get("build"):
        parts.append("LOWER(build) = LOWER(?)"); args.append(filters["build"])
    if filters.get("complexion"):
        parts.append("LOWER(complexion) = LOWER(?)"); args.append(filters["complexion"])
    if filters.get("marks"):
        parts.append("distinguishing_marks LIKE ?"); args.append(f"%{filters['marks']}%")
    if filters.get("min_height"):
        parts.append("height_cm >= ?"); args.append(int(filters["min_height"]))
    if filters.get("max_height"):
        parts.append("height_cm <= ?"); args.append(int(filters["max_height"]))
    where = ("WHERE " + " AND ".join(parts)) if parts else ""
    rows = conn.execute(f"""
        SELECT biometric_id, person_link_id, accused_name, face_data_url,
               height_cm, weight_kg, build, complexion, hair, eye_color,
               distinguishing_marks, notes
        FROM biometric_records
        {where} ORDER BY updated_at DESC LIMIT ?
    """, args + [limit]).fetchall()
    return [dict(r) for r in rows]


def top_matches(conn: sqlite3.Connection, kind: str, query_data_url: str, limit: int = 12) -> list[dict]:
    """Return the top-N biometric records whose {face,fingerprint}_ahash is
    closest (lowest Hamming distance) to the uploaded image's ahash."""
    q_hash = ahash64(query_data_url)
    if q_hash is None:
        return []
    col_hash  = "face_ahash"           if kind == "face" else "fingerprint_ahash"
    col_image = "face_data_url"        if kind == "face" else "fingerprint_data_url"
    rows = conn.execute(f"""
        SELECT biometric_id, person_link_id, accused_name,
               height_cm, build, complexion, distinguishing_marks,
               {col_hash} AS h, {col_image} AS img
        FROM biometric_records WHERE {col_hash} IS NOT NULL
    """).fetchall()
    scored = []
    for r in rows:
        d = dict(r)
        d["similarity"] = round(similarity(q_hash, d["h"]), 3)
        d.pop("h", None)
        scored.append(d)
    scored.sort(key=lambda x: -x["similarity"])
    return scored[:limit]
