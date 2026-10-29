"""Management DB access: migrations and idempotent registration of a run's records.

Only the configured NEW schema is touched. Production scpdb/oncolabdb are never connected to here.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from pathlib import Path

import pymysql
import pymysql.cursors

from .records import TABLES, build_records, to_db_value
from .runs import Run
from .util import now_iso

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "sql" / "migrations"
NAME_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
FORBIDDEN_SCHEMAS = {"scpdb", "oncolabdb", "mysql", "information_schema", "performance_schema", "sys"}


class DbError(Exception):
    pass


class RegistrationConflict(DbError):
    def __init__(self, conflicts: list[str]):
        super().__init__("등록 충돌 — 아무것도 저장하지 않았습니다:\n- " + "\n- ".join(conflicts[:50]))
        self.conflicts = conflicts


def connect(db: dict, database: str | None = ""):
    name = db.get("name") if database == "" else database
    if name is not None and (not NAME_RE.fullmatch(name) or name.lower() in FORBIDDEN_SCHEMAS):
        raise DbError(f"허용되지 않는 schema 이름: {name!r}")
    try:
        return pymysql.connect(host=db["host"], port=int(db["port"]), user=db["user"], password=db["password"],
                               database=name, charset="utf8mb4", autocommit=False, connect_timeout=5,
                               cursorclass=pymysql.cursors.DictCursor)
    except pymysql.MySQLError as e:
        raise DbError(f"DB 연결 실패 ({db.get('host')}:{db.get('port')}/{name}): {e}") from None


@contextmanager
def connection(db: dict):
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- migrations
def migration_files() -> list[tuple[str, str, list[str]]]:
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        text = p.read_text(encoding="utf-8")
        body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
        statements = [s.strip() for s in body.split(";") if s.strip()]
        out.append((p.stem, hashlib.sha256(text.encode("utf-8")).hexdigest(), statements))
    return out


def migration_status(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE 'schema_migrations'")
        applied = {}
        if cur.fetchone():
            cur.execute("SELECT version, sha256, applied_at FROM schema_migrations ORDER BY version")
            applied = {r["version"]: r for r in cur.fetchall()}
    files = migration_files()
    changed = [v for v, sha, _ in files if v in applied and applied[v]["sha256"] != sha]
    pending = [v for v, _, _ in files if v not in applied]
    return {"applied": sorted(applied), "pending": pending, "changed": changed,
            "current": max(applied) if applied else None}


def migrate(conn) -> list[str]:
    """Apply pending migrations in order. MariaDB DDL is not transactional: a failure stops and reports."""
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(64) CHARACTER SET ascii PRIMARY KEY,"
                    " sha256 CHAR(64) CHARACTER SET ascii NOT NULL,"
                    " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB")
    conn.commit()
    status = migration_status(conn)
    if status["changed"]:
        raise DbError(f"이미 적용된 migration 파일이 바뀌었습니다: {status['changed']} — 새 migration으로 추가하세요")
    done = []
    for version, sha, statements in migration_files():
        if version not in status["pending"]:
            continue
        with conn.cursor() as cur:
            for i, stmt in enumerate(statements, start=1):
                try:
                    cur.execute(stmt)
                except pymysql.MySQLError as e:
                    raise DbError(f"migration {version} 문장 {i} 실패: {e}") from None
            cur.execute("INSERT INTO schema_migrations(version, sha256) VALUES (%s, %s)", (version, sha))
        conn.commit()
        done.append(version)
    return done


# --------------------------------------------------------------------------- registration
KEYS = {"datasets": ["dataset_uuid"], "subjects": ["subject_uuid"], "studies": ["study_uid"],
        "series": ["series_uid"], "dataset_series": ["dataset_uuid", "series_uid"], "ingest_runs": ["run_uuid"],
        "instances": ["sop_uid"], "transfer_results": ["run_uuid", "target_key", "sop_uid"]}
# Columns that identify an object; a different value on an existing row is a conflict, never an overwrite.
IDENTITY = {"subjects": ["dataset_uuid", "source_subject_key", "patient_id"], "studies": ["subject_uuid"],
            "series": ["study_uid"], "instances": ["series_uid", "sop_class_uid", "artifact_sha256"],
            "ingest_runs": ["dataset_uuid", "manifest_sha256"]}
# Columns kept from the first registration.
KEEP_FIRST = {"series": {"first_run_uuid"}, "instances": {"prepared_run_uuid", "artifact_relative_path"}}


def _fetch_existing(cur, table: str, rows: list[dict]) -> dict:
    keys = KEYS[table]
    found = {}
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        cond = " OR ".join("(" + " AND ".join(f"`{k}`=%s" for k in keys) + ")" for _ in chunk)
        cur.execute(f"SELECT * FROM `{table}` WHERE {cond}", [r[k] for r in chunk for k in keys])
        for r in cur.fetchall():
            found[tuple(r[k] for k in keys)] = r
    return found


def register(conn, records: dict) -> dict:
    """Insert/update every row in one transaction; identity conflicts roll everything back."""
    conflicts, stats = [], {}
    try:
        with conn.cursor() as cur:
            subj = records["subjects"]
            if subj:
                cond = " OR ".join("(dataset_uuid=%s AND source_subject_key=%s)" for _ in subj)
                cur.execute(f"SELECT * FROM subjects WHERE {cond}",
                            [x for r in subj for x in (r["dataset_uuid"], r["source_subject_key"])])
                for r in cur.fetchall():
                    want = next(s for s in subj if s["source_subject_key"] == r["source_subject_key"])
                    if r["subject_uuid"] != want["subject_uuid"] or r["patient_id"] != want["patient_id"]:
                        conflicts.append(f"subjects: {r['source_subject_key']!r} 기존 PatientID {r['patient_id']!r}")
            for table in TABLES:
                rows = records[table]
                if not rows:
                    stats[table] = {"inserted": 0, "updated": 0, "unchanged": 0}
                    continue
                existing = _fetch_existing(cur, table, rows)
                ins = upd = same = 0
                for row in rows:
                    key = tuple(row[k] for k in KEYS[table])
                    old = existing.get(key)
                    values = {c: to_db_value(c, v) for c, v in row.items()}
                    if old is None:
                        cols = ", ".join(f"`{c}`" for c in values)
                        cur.execute(f"INSERT INTO `{table}` ({cols}) VALUES ({', '.join(['%s'] * len(values))})",
                                    list(values.values()))
                        ins += 1
                        continue
                    for c in IDENTITY.get(table, []):
                        if c in row and old.get(c) != row[c]:
                            conflicts.append(f"{table} {key}: {c} 기존 {old.get(c)!r} != 새 {row[c]!r}")
                    changes = {c: v for c, v in values.items()
                               if c not in KEYS[table] and c not in KEEP_FIRST.get(table, set())
                               and _norm(old.get(c)) != _norm(v)}
                    if changes:
                        sets = ", ".join(f"`{c}`=%s" for c in changes)
                        where = " AND ".join(f"`{k}`=%s" for k in KEYS[table])
                        cur.execute(f"UPDATE `{table}` SET {sets} WHERE {where}", [*changes.values(), *key])
                        upd += 1
                    else:
                        same += 1
                stats[table] = {"inserted": ins, "updated": upd, "unchanged": same}
            if conflicts:
                raise RegistrationConflict(conflicts)
            run_uuid = records["ingest_runs"][0]["run_uuid"]
            cur.execute("UPDATE ingest_runs SET registered_at=CURRENT_TIMESTAMP WHERE run_uuid=%s", (run_uuid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def row_status(cur, table: str, rows: list[dict]) -> list[tuple[str, list[str]]]:
    """What registration would do with each proposed row, read-only: ("신규", []) not in the DB, ("같음", []),
    ("변경", columns) it would update, ("충돌", columns) an identity column differs, so it would refuse."""
    existing = _fetch_existing(cur, table, rows) if rows else {}
    out = []
    for row in rows:
        old = existing.get(tuple(row[k] for k in KEYS[table]))
        if old is None:
            out.append(("신규", []))
            continue
        bad = [c for c in IDENTITY.get(table, []) if c in row and old.get(c) != row[c]]
        diffs = [c for c, v in row.items() if c not in KEYS[table] and c not in KEEP_FIRST.get(table, set())
                 and _norm(old.get(c)) != _norm(to_db_value(c, v))]
        out.append(("충돌", bad) if bad else (("변경", diffs) if diffs else ("같음", [])))
    return out


def _norm(v):
    import json
    if isinstance(v, str):
        try:
            return json.dumps(json.loads(v), sort_keys=True, ensure_ascii=False) if v[:1] in "{[" else v
        except ValueError:
            return v
    return v


def register_run(conn, run: Run) -> dict:
    records = build_records(run)
    stats = register(conn, records)
    reg = run.load("registration.json") or {"history": []}
    reg["history"].append({"at": now_iso(), "stats": stats, "status_at_registration": run.meta["status"]})
    reg["last"] = reg["history"][-1]
    run.save("registration.json", reg)
    return stats


# --------------------------------------------------------------------------- read helpers (web)
BROWSABLE = TABLES + ["schema_migrations", "external_mappings"]


def table_counts(conn) -> dict:
    out = {}
    with conn.cursor() as cur:
        for t in BROWSABLE:
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM `{t}`")
                out[t] = cur.fetchone()["n"]
            except pymysql.MySQLError:
                out[t] = None
    return out


def change_marks(conn, tables: list[str], exact: bool = False) -> dict:
    """{table: mark} that changes when the table changes, for the live DB pages: the row count, or with `exact`
    the live CHECKSUM TABLE (a scan, so only for the tables a page shows rows of). `tables` must already be
    whitelisted names."""
    out = {}
    with conn.cursor() as cur:
        if exact and tables:
            cur.execute("CHECKSUM TABLE " + ", ".join(f"`{t}`" for t in tables))
            return {r["Table"].rsplit(".", 1)[-1]: str(r["Checksum"]) for r in cur.fetchall()}
        for t in tables:
            cur.execute(f"SELECT COUNT(*) AS n FROM `{t}`")
            out[t] = str(cur.fetchone()["n"])
    return out


def query(conn, sql: str, args=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())
