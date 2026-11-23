"""One dataset list (CONTRACT §78.8, U210): every dataset the system knows — the datasets registered in the
management DB (old DB, deployment bundles, this system's intake) and every catalogue entry of §78.7 — one row per
dataset, each row saying where the dataset stands.

A catalogue entry and a registered dataset become one row only on a link that is not a name guess:
- `intake_run`: a run of this system read a folder the catalogue holds for the entry (confirmed or proposed, not
  rejected) and registered the dataset. The run's folder is the evidence; a person who rejects that folder on the
  entry page, or rejects the link itself, splits the row again.
- `person`: a person linked the entry to the dataset on the entry page (catalog_dataset_links).
- registered datasets that carry the same PatientID prefix are one dataset (several runs / revisions of it).
- catalogue entries with the same name after trimming (and no conflicting folder or link) are one dataset (§102): the
  audited list has a row per modality; the row unites their modalities and regions and sums the counts.
A name match between an entry and a registered dataset is only a proposal a person confirms or rejects; unlinked
ones stay two rows and are countable by the `link` filter.

Read only except `decide_link` (writes catalog_dataset_links). Nothing here downloads, moves, deletes or sends.
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter

from . import catalogue as cat
from . import nfs

STANDS = ("central", "registered", "downloaded", "downloading", "not_downloaded", "blocked", "failed", "unknown")
STAND_KO = {"central": "등록 · Central 전송", "registered": "등록됨", "downloaded": "받음 (미등록)",
            **{k: v for k, v in cat.AVAILABILITY_KO.items() if k != "downloaded"}}
SOURCES = {"list": "감수 목록", "folder": "내부 폴더", "own": "이 시스템", "legacy": "기존 DB", "deploy": "배포 묶음"}
LINKS = {"merged": "목록 + 등록 (연결됨)", "catalogue_only": "목록만 (미등록)", "registered_only": "등록만 (목록 밖)",
         "proposal": "연결 후보 있음 — 사람이 확인"}
LINK_METHODS = {"intake_run": "인테이크 run의 폴더", "person": "사람이 연결", "prefix": "같은 접두어", "name": "이름 비슷 (후보)"}
REGISTERED_KO = {"yes": "등록됨", "no": "미등록"}
DB_COMMERCIAL = {"Y": "yes", "N": "no", "U": "unknown"}
RESTRICTIVE = ("no", "unknown", "yes")                         # a merged row takes the most restrictive value
AVAIL_ORDER = ("downloaded", "downloading", "blocked", "failed", "unknown", "not_downloaded")
DICOM_MODALITY = {"CT": "CT", "MR": "MR", "PT": "PT", "NM": "NM", "US": "US", "MG": "MG", "CR": "X-ray",
                  "DX": "X-ray", "XA": "XA/RF", "RF": "XA/RF"}  # the catalogue's modality words; RT objects are labels
TIER_ORIGIN = {"PUB": "public", "NC": "public", "IO": "internal"}
NOT_IN_CATALOGUE = "none"

ORIGIN_SQL = ("CASE WHEN x.target_dataset_id IS NOT NULL THEN 'legacy' WHEN d.name LIKE 'deploy/%%' THEN 'deploy'"
              " ELSE 'own' END")


class LinkError(Exception):
    pass


# --------------------------------------------------------------------------- reading the registered side
def registered(conn) -> list[dict]:
    """Every registered dataset with its origin, counts, modalities, intake runs and central sends."""
    from . import db as dbmod
    rows = dbmod.query(conn, f"""
        SELECT d.dataset_uuid, d.name, d.license, d.commercial_use, d.patient_id_prefix, d.created_at,
               x.target_dataset_id AS legacy_id, {ORIGIN_SQL} AS origin,
               COALESCE(sb.n, 0) AS n_subjects, COALESCE(ag.n_series, 0) AS n_series,
               COALESCE(ag.n_rt, 0) AS n_rt, COALESCE(ag.n_inst, 0) AS n_instances,
               COALESCE(ag.n_train, 0) AS n_train, COALESCE(ag.n_valid, 0) AS n_valid
        FROM datasets d
        LEFT JOIN external_mappings x ON x.dataset_uuid=d.dataset_uuid AND x.target_system='oncolabdb.lab_datasets'
        LEFT JOIN (SELECT dataset_uuid, COUNT(DISTINCT pid) AS n FROM (  -- own patients + linked series patients
                     SELECT dataset_uuid, patient_id AS pid FROM subjects
                     UNION SELECT m.dataset_uuid, sj.patient_id FROM dataset_series m
                       JOIN series se ON se.series_uid=m.series_uid JOIN studies st ON st.study_uid=se.study_uid
                       JOIN subjects sj ON sj.subject_uuid=st.subject_uuid) u GROUP BY dataset_uuid) sb
             ON sb.dataset_uuid=d.dataset_uuid
        LEFT JOIN (SELECT m.dataset_uuid, COUNT(*) AS n_series, SUM(se.modality='RTSTRUCT') AS n_rt,
                          SUM(se.instance_count) AS n_inst, SUM(m.split LIKE 'train%%') AS n_train,
                          SUM(m.split LIKE 'valid%%') AS n_valid
                   FROM dataset_series m JOIN series se ON se.series_uid=m.series_uid
                   GROUP BY m.dataset_uuid) ag ON ag.dataset_uuid=d.dataset_uuid
        ORDER BY d.name""")
    by = {r["dataset_uuid"]: dict(r, modalities=[], runs=[], n_central=0) for r in rows}
    for r in dbmod.query(conn, "SELECT m.dataset_uuid, se.modality, COUNT(*) AS n FROM dataset_series m"
                               " JOIN series se ON se.series_uid=m.series_uid GROUP BY m.dataset_uuid, se.modality"
                               " ORDER BY n DESC"):
        if r["dataset_uuid"] in by:
            by[r["dataset_uuid"]]["modalities"].append(r["modality"])
    for r in dbmod.query(conn, "SELECT run_uuid, run_label, dataset_uuid, input_root, status, registered_at"
                               " FROM ingest_runs ORDER BY created_at"):
        if r["dataset_uuid"] in by:
            by[r["dataset_uuid"]]["runs"].append(r)
    for r in dbmod.query(conn, "SELECT r.dataset_uuid, COUNT(*) AS n FROM ingest_runs r JOIN transfer_results t"
                               " ON t.run_uuid=r.run_uuid WHERE t.target_key LIKE 'central:%%'"
                               " AND t.status='accepted' GROUP BY r.dataset_uuid"):
        if r["dataset_uuid"] in by:
            by[r["dataset_uuid"]]["n_central"] = int(r["n"])
    return list(by.values())


def totals(conn) -> dict:
    from . import db as dbmod
    return dbmod.query(conn, "SELECT (SELECT COUNT(DISTINCT patient_id) FROM subjects) AS subjects,"
                             " (SELECT COUNT(*) FROM studies) AS studies, (SELECT COUNT(*) FROM series) AS series,"
                             " (SELECT COALESCE(SUM(instance_count),0) FROM series) AS instances")[0]


def person_links(conn) -> list[dict]:
    from . import db as dbmod
    return dbmod.query(conn, "SELECT * FROM catalog_dataset_links")


def decide_link(conn, entry_id: int, dataset_uuid: str, state: str, by: str, note: str = "") -> None:
    """A person links an entry to a registered dataset (`confirmed`), refuses a link or proposal (`rejected`), or
    takes back their decision (`clear`)."""
    if state not in ("confirmed", "rejected", "clear"):
        raise LinkError("confirmed / rejected / clear 중 하나")
    if not by.strip():
        raise LinkError("누가 확인했는지 적어 주세요")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 AS x FROM catalog_entries WHERE entry_id=%s", (entry_id,))
        ok = cur.fetchall()
        cur.execute("SELECT 1 AS x FROM datasets WHERE dataset_uuid=%s", (dataset_uuid,))
        if not ok or not cur.fetchall():
            raise LinkError("항목이나 등록된 데이터셋이 없습니다")
        if state == "clear":
            cur.execute("DELETE FROM catalog_dataset_links WHERE entry_id=%s AND dataset_uuid=%s",
                        (entry_id, dataset_uuid))
        else:
            cur.execute("""INSERT INTO catalog_dataset_links (entry_id, dataset_uuid, state, note, decided_by)
                           VALUES (%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE state=VALUES(state), note=VALUES(note),
                                                   decided_by=VALUES(decided_by), decided_at=CURRENT_TIMESTAMP""",
                        (entry_id, dataset_uuid, state, note[:2000] or None, by[:128]))
    conn.commit()


# --------------------------------------------------------------------------- linking
def _primary(path: str | None) -> str:
    p = str(path or "").rstrip("/")
    for a, b in nfs.mirrors():                                 # a run may have read the read-only mirror
        if p == b or p.startswith(b + "/"):
            return a + p[len(b):]
    return p


def _inside(path: str, folder: str) -> bool:
    return bool(path) and (path == folder or path.startswith(folder.rstrip("/") + "/"))


def link(entries: list[dict], datasets: list[dict], decided: list[dict]) -> dict:
    """{(entry_id, dataset_uuid): {method, state, note, by, at}} — every link and proposal between the two sides.
    state `confirmed` merges the two into one row; `proposed` and `rejected` do not."""
    out = {}
    person = {(d["entry_id"], d["dataset_uuid"]): d for d in decided}
    for e in entries:
        for m in e["folders"]:
            if m["state"] == "rejected":
                continue
            fp = m["folder"]["path"]
            for d in datasets:
                for r in d["runs"]:
                    if _inside(_primary(r["input_root"]), fp):
                        out[(e["entry_id"], d["dataset_uuid"])] = {
                            "method": "intake_run", "state": "confirmed",
                            "note": f"run {r['run_label'] or r['run_uuid'][:8]} 입력 {r['input_root']}"
                                    + ("" if m["state"] == "confirmed" else " (폴더↔목록 행은 이름 후보 — 아니면 폴더나 연결을 '아님'으로)")}
    fake = [{"name": re.sub(r"^deploy/", "", d["name"]), "path": d["dataset_uuid"], "role": "source"} for d in datasets]
    keyed = [dict(e, key=e["entry_id"]) for e in entries if not e["is_marker"]]
    for eid, found in cat.propose_matches(keyed, fake).items():
        for uuid, _, score in found:
            out.setdefault((eid, uuid), {"method": "name", "state": "proposed", "score": score,
                                         "note": "이름이 비슷함 — 같은 데이터셋인지 사람이 확인"})
    for key, d in person.items():
        prev = out.get(key, {})
        out[key] = {"method": "person" if d["state"] == "confirmed" else prev.get("method", "person"),
                    "state": d["state"], "note": d.get("note") or prev.get("note"),
                    "by": d["decided_by"], "at": d["decided_at"]}
    return out


# --------------------------------------------------------------------------- rows
def _most_restrictive(values) -> str:
    vals = [v for v in values if v]
    return next((v for v in RESTRICTIVE if v in vals), "unknown")


def _tier_origin(prefix: str | None) -> str | None:
    if not prefix:
        return None
    return TIER_ORIGIN.get(prefix.rsplit("_", 1)[-1].upper())


def _merge_fields(ents: list[dict]) -> dict:
    if len(ents) == 1:
        return ents[0]["fields"]
    out = {}
    for e in ents:
        for h, v in e["fields"].items():
            if v is None:
                continue
            if h not in out:
                out[h] = v
            elif str(v) not in str(out[h]).split(" | "):
                out[h] = f"{out[h]} | {v}"
    return out


def make_row(ents: list[dict], dss: list[dict], links: dict, planned: dict) -> dict:
    first = ents[0] if ents else None
    for d in dss:
        d["prefix"] = d["patient_id_prefix"] or planned.get(d["dataset_uuid"])
    prefixes = sorted({d["prefix"] for d in dss if d["prefix"]})
    tags = {"modality": [], "roi_category": [], "body": [], "country": []}
    for e in ents:
        for k in tags:
            tags[k] += [v for v in e["tags"].get(k, []) if v not in tags[k]]
    for d in dss:
        tags["modality"] += [m for m in (DICOM_MODALITY.get(x) for x in d["modalities"]) if m and m not in tags["modality"]]
    tags["verification"] = first["tags"].get("verification", "없음") if first else "없음"
    tags["new_rois"] = "yes" if any(e["tags"].get("new_rois") == "yes" for e in ents) else "no"
    licences = sorted({e["eff"]["licence"] or "none stated" for e in ents}) or \
        sorted({cat.licence_name(d["license"]) or "none stated" for d in dss})
    origins = {e["eff"]["origin"] for e in ents} or {_tier_origin(d["prefix"]) or "unknown" for d in dss}
    origin = "public" if origins == {"public"} else "internal" if origins == {"internal"} else \
        (origins.pop() if len(origins) == 1 else "unknown")
    avail = next((a for a in AVAIL_ORDER if any(e["eff"]["availability"] == a for e in ents)), NOT_IN_CATALOGUE)
    comm = _most_restrictive([e["eff"]["commercial_use"] for e in ents]
                             + [DB_COMMERCIAL.get(d["commercial_use"], "unknown") for d in dss])
    stand = "central" if any(d["n_central"] for d in dss) else "registered" if dss else avail
    stand_parts = []                       # §102: the parts named where a grouped row's parts stand differently
    if len(ents) > 1 and not dss and len({e["eff"]["availability"] for e in ents}) > 1:
        for e in ents:
            label = ", ".join(e["tags"].get("modality") or []) or f"목록 {e['list_row'] or e['entry_id']}행"
            stand_parts.append({"label": label, "stand": e["eff"]["availability"], "entry_id": e["entry_id"]})
    folders, seen = [], set()
    for e in ents:
        for m in e["folders"]:
            if m["folder_id"] not in seen:
                seen.add(m["folder_id"])
                folders.append(m)
    row_links = [dict(v, entry_id=k[0], dataset_uuid=k[1]) for k, v in links.items()
                 if (k[0] in {e["entry_id"] for e in ents} or k[1] in {d["dataset_uuid"] for d in dss})]
    kinds = [("folder" if e["kind"] == "folder" else "list") for e in ents] + [d["origin"] for d in dss]
    link_vals = ["merged" if ents and dss else "catalogue_only" if ents else "registered_only"]
    if any(x["state"] == "proposed" for x in row_links):
        link_vals.append("proposal")
    row = {
        "key": f"e{first['entry_id']}" if first else f"d{dss[0]['dataset_uuid']}",
        "name": (dss[0]["name"] if dss and len(ents) != 1 and len({name_key(e["name"]) for e in ents}) != 1
                 else re.sub(r"\s+", " ", first["name"]).strip()), "entries": ents, "datasets": dss,
        "entry_id": first["entry_id"] if first else None, "list_row": first["list_row"] if first else None,
        "is_marker": bool(ents) and all(e["is_marker"] for e in ents),
        "kind": (first["kind"] if first else "registered"), "link": first["link"] if first else None,
        "fields": _merge_fields(ents) if ents else {}, "tags": tags, "folders": folders,
        "n_proposed": sum(e["n_proposed"] for e in ents),
        "availability_note": "; ".join(e["availability_note"] or "" for e in ents if e["availability_note"]),
        "diff": any((e["availability_note"] or "").startswith("차이:") for e in ents),
        "edited": sorted({f for e in ents for f in e["edited"]}), "commercial_reason": "; ".join(
            sorted({e["commercial_reason"] for e in ents if e.get("commercial_reason")})),
        "licences": licences, "prefixes": prefixes, "stand": stand, "stand_parts": stand_parts,
        "registered": "yes" if dss else "no", "sources": sorted(set(kinds), key=list(SOURCES).index),
        "link_state": link_vals, "links": row_links,
        "eff": {"commercial_use": comm, "licence": " / ".join(licences), "origin": origin, "availability": avail},
        "n_subjects": sum(int(d["n_subjects"]) for d in dss), "n_series": sum(int(d["n_series"]) for d in dss),
        "n_instances": sum(int(d["n_instances"] or 0) for d in dss), "n_central": sum(d["n_central"] for d in dss),
    }
    db_comm = sorted({d["commercial_use"] for d in dss})
    if dss and ents and any(DB_COMMERCIAL.get(c) != comm for c in db_comm):
        row["commercial_reason"] = (row["commercial_reason"] + "; " if row["commercial_reason"] else "") + \
            f"등록 값 {'/'.join(db_comm)} — 가장 제한적인 값을 씀"
    row["_text"] = " ".join([cat.search_text(e) for e in ents] + [
        f"{d['name']} {d['prefix'] or ''} {d['dataset_uuid']}" for d in dss]).lower()
    return row


def name_key(name: str | None) -> str:
    """The grouping key of a catalogue name (§102): whitespace collapsed, trimmed, case folded."""
    return re.sub(r"\s+", " ", name or "").strip().casefold()


def _link_key(e: dict) -> str:
    v = str((e.get("fields") or {}).get("데이터셋 링크") or "").strip()
    return v.split()[0].rstrip("/").casefold() if v else ""


def same_name_groups(entries: list[dict]) -> list[list[dict]]:
    """Catalogue entries that are one dataset under §102: the same name after trimming, the same confirmed source
    folders where both have some, the same dataset link where both have one. The audited list has one row per
    modality (CPTAC-CCRCC MR + CT) and repeats some rows in its second section (BTCV, AbdomenCT-1K, a team note
    row …); those become one group. Returns only groups of two or more."""
    by_key: dict[str, list[dict]] = {}
    for e in entries:
        if not name_key(e["name"]):
            continue
        folders = frozenset(m["folder"]["path"] for m in e["folders"] if m["state"] == "confirmed")
        link = _link_key(e)
        clusters = by_key.setdefault(name_key(e["name"]), [])
        for c in clusters:
            if (not c["folders"] or not folders or c["folders"] == folders) and \
                    (not c["link"] or not link or c["link"] == link):
                c["entries"].append(e)
                c["folders"] = c["folders"] or folders
                c["link"] = c["link"] or link
                break
        else:
            clusters.append({"folders": folders, "link": link, "entries": [e]})
    return [c["entries"] for cs in by_key.values() for c in cs if len(c["entries"]) > 1]


def build(entries: list[dict], datasets: list[dict], decided: list[dict], planned: dict | None = None) -> list[dict]:
    """The rows of the one list: groups of entries and registered datasets joined by confirmed links and by a
    shared PatientID prefix (union-find); catalogue order first, then the registered datasets outside it."""
    planned = planned or {}
    links = link(entries, datasets, decided)
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for e in entries:
        find(("e", e["entry_id"]))
    by_prefix = {}
    for d in datasets:
        find(("d", d["dataset_uuid"]))
        p = d["patient_id_prefix"] or planned.get(d["dataset_uuid"])
        if p:
            if p in by_prefix:
                union(("d", d["dataset_uuid"]), by_prefix[p])
            by_prefix.setdefault(p, ("d", d["dataset_uuid"]))
    for (eid, uuid), v in links.items():
        if v["state"] == "confirmed":
            union(("e", eid), ("d", uuid))
    for group in same_name_groups(entries):                     # §102: one row per dataset, not per modality
        for e in group[1:]:
            union(("e", e["entry_id"]), ("e", group[0]["entry_id"]))
    groups: dict = {}
    for e in entries:
        groups.setdefault(find(("e", e["entry_id"])), ([], []))[0].append(e)
    order = {"own": 0, "legacy": 1, "deploy": 2}
    for d in sorted(datasets, key=lambda d: (order.get(d["origin"], 3), d["name"].lower())):
        groups.setdefault(find(("d", d["dataset_uuid"])), ([], []))[1].append(d)
    rows = [make_row(ents, dss, links, planned) for ents, dss in groups.values()]
    for e in entries:                                           # the entry page shows its links
        e["dataset_links"] = [dict(v, dataset_uuid=k[1]) for k, v in links.items() if k[0] == e["entry_id"]]
    return rows


# --------------------------------------------------------------------------- filters, export
FACETS = {  # param -> (label, values of a row)
    "stand": ("어디까지 왔나", lambda r: [r["stand"]]),
    "registered": ("등록", lambda r: [r["registered"]]),
    "source": ("출처", lambda r: r["sources"]),
    **{k: v for k, v in cat.FACETS.items() if k not in ("licence", "diff", "kind")},
    "licence": ("라이선스", lambda r: r["licences"]),
    "diff": ("목록과 스캔", lambda r: ["차이" if r["diff"] else "일치"] if r["entries"] else ["목록 밖"]),
    "kind": ("행 종류", lambda r: ["marker" if r["is_marker"] else r["kind"]]),
    "link": ("목록 ↔ 등록 연결", lambda r: r["link_state"]),
}
ORDER = ("stand", "registered", "source", "commercial", "licence", "origin", "availability", "modality", "roi",
         "body", "country", "verification", "new_rois", "diff", "proposed", "kind", "link")
FACETS = {k: FACETS[k] for k in ORDER}
OLD_ORIGIN = {"own", "legacy", "deploy"}                       # /datasets?origin=deploy of the old page = source


def apply_filters(rows, params, q=""):
    return cat.apply_filters(rows, params, q, facets=FACETS)


EXPORT_FIXED = [("name", "데이터셋"), ("stand", "어디까지 왔나"), ("registered", "등록"), ("sources", "출처"),
                ("origin", "출처 구분"), ("licence", "라이선스(정규화)"), ("commercial_use", "상업적 이용"),
                ("prefixes", "접두어"), ("modality", "모달리티"), ("availability", "보유 상태 (NAS)"),
                ("availability_note", "보유 상태 근거"), ("folders", "폴더"), ("entry_ids", "카탈로그 항목 ID"),
                ("dataset_uuids", "등록 dataset_uuid"), ("dataset_names", "등록 이름"), ("n_subjects", "환자"),
                ("n_series", "시리즈"), ("n_instances", "DICOM 파일"), ("n_central", "Central 전송 파일")]


def export_rows(rows: list[dict], headers: list[str]) -> list[list]:
    out = [[label for _, label in EXPORT_FIXED] + headers]
    for r in rows:
        fixed = {"stand": STAND_KO.get(r["stand"], r["stand"]), "registered": r["registered"],
                 "sources": ", ".join(SOURCES.get(s, s) for s in r["sources"]),
                 "prefixes": ", ".join(r["prefixes"]), "modality": ", ".join(r["tags"]["modality"]),
                 "folders": " | ".join(m["folder"]["path"] for m in r["folders"] if m["state"] == "confirmed"),
                 "entry_ids": ", ".join(str(e["entry_id"]) for e in r["entries"]),
                 "dataset_uuids": ", ".join(d["dataset_uuid"] for d in r["datasets"]),
                 "dataset_names": " | ".join(d["name"] for d in r["datasets"]), **r["eff"]}
        out.append([fixed.get(k, r.get(k)) for k, _ in EXPORT_FIXED] + [r["fields"].get(h) for h in headers])
    return out


def planned_prefixes(runs_dir) -> dict:
    """{dataset_uuid: PatientID prefix} from the runs' plans (newest wins), for registered datasets without one."""
    from .runs import list_runs
    runs = list_runs(runs_dir) if runs_dir else []
    return {m["dataset_uuid"]: m["patient_id_prefix"] for m in reversed(runs) if m.get("patient_id_prefix")}


# --------------------------------------------------------------------------- a short-lived cache per web app
class ListCache:
    """The built rows for `ttl` seconds (the registered counts change on imports only); any write through the web
    clears it."""

    def __init__(self, runs_dir=None, ttl: float = 60.0):
        self.runs_dir, self.ttl, self._at, self._val, self._lock = runs_dir, ttl, 0.0, None, threading.Lock()

    def get(self, conn) -> tuple[list[dict], list[dict], dict, list[dict]]:
        with self._lock:
            if self._val is not None and time.monotonic() - self._at < self.ttl:
                return self._val
        entries, extra = cat.load(conn)
        datasets = registered(conn)
        rows = build(entries, datasets, person_links(conn), planned_prefixes(self.runs_dir))
        val = (rows, entries, {**extra, "totals": totals(conn), "datasets": datasets}, datasets)
        with self._lock:
            self._val, self._at = val, time.monotonic()
        return val

    def clear(self) -> None:
        with self._lock:
            self._val = None


def summary(rows: list[dict]) -> dict:
    return {"rows": len(rows), "stand": dict(Counter(r["stand"] for r in rows)),
            "registered_rows": sum(r["registered"] == "yes" for r in rows),
            "registered_datasets": sum(len(r["datasets"]) for r in rows),
            "merged": [r["name"] for r in rows if r["entries"] and r["datasets"]],
            "commercial": dict(Counter(r["eff"]["commercial_use"] for r in rows)),
            "link": dict(Counter(v for r in rows for v in r["link_state"]))}
