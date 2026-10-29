"""The standard dataset report: the same numbered tables with the same columns for every dataset, so reports
of different datasets can be read side by side and compared (`dataset_index`). Built only from the run's
files (plan, scan, inventory, manifests, journals, registration) and `metadata.build`; agent text appears
only as cell values, never as free-form report prose. Tables 6 and 7 are what actually leaves this machine:
the DICOM values per series as the local SCP receives them, and the rows of every management-DB table.

 1 데이터셋        2 분할 구성       3 폴더 판정       4 확인 사항       5 전송·등록
 6 Local SCP 메타데이터 (DICOM)      7 DB 등록 예정    8 원본 메타데이터  9 열 정의   10 DICOM 헤더
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from pydicom.datadict import tag_for_keyword

from . import db as dbmod
from . import metadata
from .local_send import journals
from .prepare import MANIFESTS
from .records import AUTO_COLUMNS, COLUMN_DESC, TABLE_DESC, TABLES, build_records, instance_rows, transfer_rows
from .runs import Run, list_runs
from .util import now_iso

SCHEMA_VERSION = 2
SPLITS = [("train", "학습"), ("validation", "검증"), ("test", "테스트"), ("other", "기타"), ("none", "구분 없음")]
TITLES = {1: "데이터셋", 2: "분할 구성", 3: "폴더 판정", 4: "확인 사항", 5: "전송·등록",
          6: "Local SCP 메타데이터 (DICOM)", 7: "DB 등록 예정", 8: "원본 메타데이터", 9: "열 정의",
          10: "DICOM 헤더 (파일럿 실제 파일)"}
COLUMNS = {
    1: ["항목", "값"],
    2: ["분할", "대상자", "CT", "MR", "라벨", "인스턴스", "포함"],
    3: ["경로", "판정", "역할", "분할", "파일 수", "크기(GB)", "근거"],
    4: ["#", "구분", "내용", "출처"],
    5: ["단계", "대상", "대상자", "시리즈", "인스턴스", "결과", "일시"],
    7: ["테이블", "설명", "예정 행", "신규", "같음", "변경", "충돌", "비고"],
    9: ["#", "표", "열", "태그 / 위치", "설명", "출처", "검증"],
    10: ["태그", "이름", "VR", "값", "출처", "설명"],
}
DB_STATES = ["신규", "같음", "변경", "충돌"]
INDEX_COLUMNS = ["데이터셋", "상태", "대상자", "CT", "MR", "라벨", "인스턴스", "원본(GB)", "라이선스", "상업적 이용",
                 "확인 사항", "파일럿", "승인", "Central", "DB", "분석일", "run"]
ROLE = {"image": "영상", "label": "라벨", "ignore": "무시"}
SPLIT_NAME = dict(SPLITS)


def _section(no: int, rows: list, columns: list | None = None, **extra) -> dict:
    return {"no": no, "title": TITLES[no], "columns": columns or COLUMNS[no], "rows": rows, **extra}


def _gb(n) -> float | None:
    return round(n / 1e9, 2) if n is not None else None


def _join(items) -> str | None:
    items = [str(i) for i in items if i not in (None, "")]
    return ", ".join(items) if items else None


# ---------------------------------------------------------------------- 1 데이터셋
def _s1(run: Run, plan: dict, scan: dict, counts: dict) -> dict:
    ds, a, meta = plan.get("dataset") or {}, plan.get("analysis") or {}, run.meta
    ag = a.get("agent") or {}
    rules = a.get("modality_rules") or []
    rows = [
        ["이름", ds.get("name")],
        ["dataset UUID", ds.get("dataset_uuid") or meta.get("dataset_uuid")],
        ["원본 경로", plan.get("input_root") or meta.get("input_root")],
        ["원본 크기(GB)", _gb((scan.get("totals") or {}).get("bytes"))],
        ["원본 파일 수", sum(v for k, v in (scan.get("totals") or {}).items() if k != "bytes") or None],
        ["출처 URL", ds.get("source_url")],
        ["라이선스", ds.get("license")],
        ["상업적 이용", ds.get("commercial_use")],
        ["인용", a.get("citation")],
        ["설명", ds.get("description")],
        ["PatientID 접두어", ds.get("patient_id_prefix")],
        ["대상자 수", counts["subjects"]],
        ["영상 시리즈 (CT / MR)", f"{counts['CT']} / {counts['MR']}"],
        ["라벨 시리즈 (RTSTRUCT)", counts["labels"]],
        ["인스턴스 수 (예정)", counts["instances"]],
        ["라벨 이름", _join(f"{k} {v}" for k, v in (a.get("label_names") or {}).items())],
        ["대상자 키 규칙", (a.get("subject_pattern") or {}).get("regex")],
        ["Modality 규칙", _join(f"{r['regex']} → {r['modality']}" for r in rules)],
        ["라벨 짝 규칙", {"same_name": "같은 파일명", "subject": "같은 대상자"}.get(a.get("label_pairing"))],
        ["분석 요약 (에이전트)", a.get("summary_ko")],
        ["분석 모델 · 비용", f"{ag['model']} · ${ag['cost_usd']:.2f}" if ag.get("model") and ag.get("cost_usd")
         is not None else None],
        ["분석 일시", ag.get("finished_at") or plan.get("created_at")],
        ["상태", meta.get("status")],
        ["plan revision", meta.get("revision")],
    ]
    return _section(1, rows, kv=True)


# ---------------------------------------------------------------------- 2 분할 구성
def _counts(plan: dict) -> tuple[dict, dict]:
    per = {k: Counter() for k, _ in SPLITS}
    subjects = {k: set() for k, _ in SPLITS}
    excluded_splits = {k: True for k, _ in SPLITS}
    for s in plan.get("series", []):
        sp = s.get("split") if s.get("split") in per else "none"
        if s.get("excluded"):
            continue
        excluded_splits[sp] = False
        if s["kind"] == "rtstruct":
            per[sp]["labels"] += 1
        else:
            per[sp][s.get("modality") or "?"] += 1
            subjects[sp].add(s["subject_key"])
        per[sp]["instances"] += s.get("instance_count") or 0
    has_any = {k: any(x.get("split", "none") == k or (k == "none" and x.get("split") not in per)
                      for x in plan.get("series", [])) for k, _ in SPLITS}
    total = Counter()
    for k in per:
        total.update(per[k])
    totals = {"subjects": len(set().union(*subjects.values())), "CT": total["CT"], "MR": total["MR"],
              "labels": total["labels"], "instances": total["instances"]}
    rows = {k: {"subjects": len(subjects[k]), "CT": per[k]["CT"], "MR": per[k]["MR"], "labels": per[k]["labels"],
                "instances": per[k]["instances"], "present": has_any[k],
                "included": has_any[k] and not excluded_splits[k]} for k, _ in SPLITS}
    return rows, totals


def _s2(rows: dict, totals: dict) -> dict:
    out = [[f"{name} ({k})", r["subjects"], r["CT"], r["MR"], r["labels"], r["instances"],
            ("Y" if r["included"] else "N") if r["present"] else "-"] for (k, name), r in
           ((x, rows[x[0]]) for x in SPLITS)]
    out.append(["합계", totals["subjects"], totals["CT"], totals["MR"], totals["labels"], totals["instances"], ""])
    return _section(2, out, total_row=True)


# ---------------------------------------------------------------------- 3 폴더 판정
def _s3(plan: dict, inv: dict) -> dict:
    a = plan.get("analysis") or {}
    by_source = {}
    for f in inv.get("files", []):
        c = by_source.setdefault(f.get("source"), [0, 0])
        c[0] += 1
        c[1] += f.get("size") or 0
    rows = []
    for src in a.get("sources", []):
        n, b = by_source.get(src["path"], [None, None])
        rows.append([src["path"], "무시" if src["role"] == "ignore" else "포함", ROLE.get(src["role"], src["role"]),
                     SPLIT_NAME.get(src["split"], src["split"]), n, _gb(b), src.get("note")])
    for e in a.get("left_out", []):
        path = e["path"]
        if e.get("files") == 1:          # plans from 2026-10-28 grouped single excluded files by top folder
            path = next((x["path"] for x in a.get("excluded", []) if x.get("reason") == e.get("reason")
                         and x["path"].startswith(path + "/") and x["path"].count("/") == 1), path)
        rows.append([path, "제외", "-", "-", e.get("files"), _gb(e.get("bytes")), e.get("reason")])
    return _section(3, rows)


# ---------------------------------------------------------------------- 4 확인 사항
def _s4(run: Run, plan: dict, scan: dict, md: dict) -> dict:
    a = plan.get("analysis") or {}
    items = [("오류", e, "검증") for e in run.load("plan_errors.json", []) or []]
    items += [("미해결", u.get("message"), "검증") for u in plan.get("unresolved", [])]
    if md.get("unreadable"):  # keep in step with issue_counts()
        items.append(("오류", f"원본 {len(md['unreadable'])}개 파일을 읽을 수 없음 (예: {md['unreadable'][0]})", "원본 헤더"))
    if md.get("unknown_tags"):
        items.append(("오류", f"출처가 정의되지 않은 DICOM 태그: {', '.join(md['unknown_tags'])}", "DICOM 헤더"))
    for t in md.get("dicom", {}).get("tags", []):
        if t["check"].startswith("불일치"):
            ex = t["bad"][0]
            items.append(("오류", f"DICOM {t['key']} {t['check']} (예: {ex['series']} 예정 {ex['expected']!r}, "
                                 f"실제 {ex['dicom']!r})", "표 6 검증"))
    for f in md.get("fields", []):
        if (f.get("db_check") or "").startswith("불일치"):
            items.append(("오류", f"{f['label']}: DB {f['db_check']}", "표 8 검증"))
    if scan.get("errors"):
        items.append(("경고", f"스캔 중 읽지 못한 경로 {len(scan['errors'])}개 (예: {scan['errors'][0]['rel']})", "스캔"))
    items += [("질문", q, "에이전트") for q in a.get("questions", [])]
    items += [("사실", f, "에이전트") for f in a.get("findings", [])]
    return _section(4, [[i, kind, text, src] for i, (kind, text, src) in enumerate(items, start=1)])


# ---------------------------------------------------------------------- 5 전송·등록
def _s5(run: Run, plan: dict, counts: dict, settings) -> dict:
    meta = run.meta
    jl = journals(run)

    def journal_row(scope, kind):
        js = [j for j in jl if j.get("scope", "full") == scope and j["target"].get("kind", "local") == kind]
        if not js:
            return None, None, None
        j = js[-1]
        c = Counter(r["status"] for r in j["instances"].values())
        ok = c["accepted"] + c["warning"]
        total = sum(c.values())
        result = f"{'수신' if ok == total else ('실패' if ok == 0 else '일부')} {ok}/{total}"
        return j["target_key"], result, (j.get("sessions") or [{}])[-1].get("finished_at")

    pilot = run.load(MANIFESTS["pilot"])
    pilot_subjects = (plan.get("pilot") or {}).get("subjects", [])
    p_target, p_result, p_at = journal_row("pilot", "local")
    c_target, c_result, c_at = journal_row("full", "central")
    appr = meta.get("approval") or {}
    reg = (run.load("registration.json") or {}).get("last") or {}
    central = getattr(settings, "central", None)
    central_key = central.key if central and central.configured else None
    local_key = settings.local_scp.key if settings.local_scp else "미설정"
    rows = [
        ["1 파일럿 → Local SCP", p_target or local_key, _join(pilot_subjects),
         len(pilot["series"]) if pilot else None, len(pilot["instances"]) if pilot else None,
         p_result or "미전송", p_at],
        ["2 최종 승인", appr.get("approved_by"), None, None, None, "승인" if appr else "대기", appr.get("approved_at")],
        ["3 Central 전송", c_target or central_key or "미설정", counts["subjects"], counts["series"],
         counts["instances"], c_result or "미전송", c_at],
        ["4 DB 등록", "관리 DB", counts["subjects"], counts["series"], counts["instances"],
         "등록" if reg else "미등록", reg.get("at")],
    ]
    return _section(5, rows)


# ---------------------------------------------------------------------- 6 / 7 / 8 / 9 / 10
def dicom_columns() -> list[dict]:
    return ([{"key": k, "label": label, "desc": ""} for k, label in metadata.DICOM_LEAD] +
            [{"key": t.key, "label": t.key, "desc": f"{t.tag} {t.desc}".strip()} for t in metadata.TAGS] +
            [{"key": "check", "label": "실제 파일 확인", "desc": "변환된 파일의 값과 plan 예정 값 비교"}])


def source_columns(md: dict) -> list[dict]:
    return [{"key": f["key"], "label": f"{f['no']} {f['label']}", "desc": f["desc"]} for f in md["fields"]]


def _s7(records: dict, notes: dict) -> dict:
    rows = [[t, TABLE_DESC.get(t), len(records[t]), None, None, None, None, notes.get(t)] for t in TABLES]
    return _section(7, rows, tables=[{"name": t, "columns": len(COLUMN_DESC[t]) + 2} for t in TABLES])


def _tag_number(keyword: str) -> str:
    tag = tag_for_keyword(keyword)
    return f"({tag >> 16:04X},{tag & 0xFFFF:04X})" if tag is not None else ""


def _s9(md: dict) -> dict:
    rows = []
    for t in md["dicom"]["tags"]:
        rows.append(["6 DICOM", t["key"], t["tag"], t["desc"], t["source"], t["check"]])
    for kw, (kind, why) in metadata.TAG_SOURCE.items():
        if kw not in metadata.TAG_KEYS:
            rows.append(["6 DICOM (모든 시리즈 같음·슬라이스별)", kw, _tag_number(kw), why, kind, "표 10"])
    for table in TABLES:
        for col, desc in COLUMN_DESC[table].items():
            rows.append(["7 DB", f"{table}.{col}", "", desc,
                         "변환·전송 결과" if table in ("instances", "transfer_results") else "plan", "-"])
        for col in AUTO_COLUMNS[table]:
            rows.append(["7 DB", f"{table}.{col}", "", "DB가 채우는 시각", "DB 자동", "-"])
    for f in md["fields"]:
        rows.append(["8 원본", f["label"], "", f["desc"], f"DICOM: {f['dicom'] or '없음'} · DB: {f['db'] or '없음'}",
                     f["db_check"] or "-"])
    return _section(9, [[i, *r] for i, r in enumerate(rows, start=1)])


def _s10(md: dict) -> list[dict]:
    return [_section(10, [[t["tag"], t["keyword"], t["vr"], t["value"], t["source"], t["why"]] for t in h["tags"]],
                     subtitle=f"{h['modality']} · {h['series']}") for h in md["headers"]]


def db_preview(run: Run, conn=None, db_error: str | None = None) -> dict:
    """Table 7 rows: every row registration would write, per table with the DB's own column names, and what
    registration would do with each row in the live DB (신규 / 같음 / 변경 / 충돌). Before the full conversion
    the pilot's files stand in for instances and transfer_results (same deterministic SOP UIDs)."""
    records = build_records(run, preview=True)
    notes = {}
    planned = sum(r["instance_count"] or 0 for r in records["series"])
    if not records["instances"]:
        pilot = run.load(MANIFESTS["pilot"])
        if pilot:
            records["instances"] = instance_rows(run, pilot)
            records["transfer_results"] = transfer_rows(run, {i["sop_uid"] for i in pilot["instances"]})
            notes["instances"] = f"파일럿 파일만 — 전체 {planned:,}행은 전체 변환 뒤 확정"
            notes["transfer_results"] = "파일럿 전송만 — Central 전송 뒤 추가"
        else:
            notes["instances"] = f"전체 변환 뒤 {planned:,}행 생성"
            notes["transfer_results"] = "전송 뒤 생성"
    tables = {}
    for t in TABLES:
        rows = records[t]
        status = None
        if conn is not None:
            with conn.cursor() as cur:
                status = dbmod.row_status(cur, t, rows)
        out, counts = [], Counter()
        for i, r in enumerate(rows):
            row = {c: _cellify(r.get(c)) for c in COLUMN_DESC[t]}
            st, cols = status[i] if status else ("DB 연결 안 됨", [])
            row["_db"], row["_diff"] = st, ", ".join(cols) or None
            counts[st] += 1
            out.append(row)
        tables[t] = {"name": t, "desc": TABLE_DESC.get(t), "note": notes.get(t), "counts": dict(counts),
                     "columns": [{"key": c, "label": c, "desc": d} for c, d in COLUMN_DESC[t].items()] +
                                [{"key": "_db", "label": "DB 상태", "desc": "등록하면: 신규 추가 / 같음 / 변경 / 충돌(거부)"},
                                 {"key": "_diff", "label": "차이 컬럼", "desc": "DB 값과 다른 컬럼"}],
                     "rows": out, "filters": ["_db"]}
    return {"checked_at": now_iso(), "db": db_error or ("ok" if conn is not None else "DB 설정 없음"),
            "tables": tables}


def _cellify(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else v


# ---------------------------------------------------------------------- build
def build(run: Run, settings, refresh: bool = False) -> dict:
    plan = run.load("plan.json") or {}
    if not plan.get("series"):
        return {"ready": False, "schema_version": SCHEMA_VERSION}
    md = metadata.build(run, refresh)
    scan, inv = run.load("scan.json", {}) or {}, run.load("inventory.json", {}) or {}
    split_rows, totals = _counts(plan)
    counts = {**totals, "series": sum(1 for s in plan["series"] if not s.get("excluded"))}
    preview = db_preview(run)
    sections = [_s1(run, plan, scan, counts), _s2(split_rows, totals), _s3(plan, inv), _s4(run, plan, scan, md),
                _s5(run, plan, counts, settings),
                _section(6, [], columns=[c["label"] for c in dicom_columns()], src="dicom"),
                _s7({t: v["rows"] for t, v in preview["tables"].items()},
                    {t: v["note"] for t, v in preview["tables"].items()}),
                _section(8, [], columns=[c["label"] for c in source_columns(md)], src="series"),
                _s9(md), *(_s10(md) or [_section(10, [], subtitle="파일럿 변환 전")])]
    return {"ready": True, "schema_version": SCHEMA_VERSION, "run_id": run.id, "sections": sections,
            "dicom": {"columns": dicom_columns(), "rows": md["dicom"]["rows"],
                      "filters": ["split", "kind", "pilot", "origin", "check"]},
            "series": {"columns": source_columns(md), "rows": md["rows"],
                       "filters": ["split", "kind", "included", "pilot"]},
            "counts": counts, "dataset_name": (plan.get("dataset") or {}).get("name")}


def issue_counts(run: Run, plan: dict, scan: dict) -> Counter:
    """Section 4 counts per kind without re-reading headers (for the comparison table): field-check mismatches
    and unknown tags only show in the full report."""
    c = Counter({"오류": len(run.load("plan_errors.json", []) or []), "미해결": len(plan.get("unresolved", [])),
                 "경고": 1 if scan.get("errors") else 0,
                 "질문": len((plan.get("analysis") or {}).get("questions", []))})
    files = (run.load(metadata.HEADERS_FILE) or {}).get("files", {})
    if any("error" in (h.get("header") or {}) for h in files.values()):
        c["오류"] += 1
    return c


def index_row(run: Run) -> list:
    """One row per dataset for the comparison table (INDEX_COLUMNS) — cheap: no header reads."""
    plan, meta = run.load("plan.json") or {}, run.meta
    scan = run.load("scan.json", {}) or {}
    _, t = _counts(plan) if plan.get("series") else ({}, {"subjects": 0, "CT": 0, "MR": 0, "labels": 0,
                                                            "instances": 0})
    ic = issue_counts(run, plan, scan)
    jl = journals(run)

    def sent(scope, kind):
        js = [j for j in jl if j.get("scope", "full") == scope and j["target"].get("kind", "local") == kind]
        if not js:
            return "미전송"
        c = Counter(r["status"] for r in js[-1]["instances"].values())
        ok = c["accepted"] + c["warning"]
        return f"{'수신' if ok == sum(c.values()) else '일부'} {ok}/{sum(c.values())}"
    ds = plan.get("dataset") or {}
    return [ds.get("name") or Path(meta.get("input_root", "")).name, meta["status"], t["subjects"], t["CT"], t["MR"],
            t["labels"], t["instances"], _gb((scan.get("totals") or {}).get("bytes")), ds.get("license"),
            ds.get("commercial_use"), f"오류 {ic['오류']} · 미해결 {ic['미해결']} · 경고 {ic['경고']} · 질문 {ic['질문']}", sent("pilot", "local"),
            (meta.get("approval") or {}).get("approved_by") or "대기", sent("full", "central"),
            "등록" if run.load("registration.json") else "미등록", (meta.get("created_at") or "")[:10], run.id]


def dataset_index(runs_dir: Path) -> list[list]:
    """Latest run per dataset (same folder = same dataset UUID), newest first."""
    latest = {}
    for m in list_runs(runs_dir):
        key = m.get("dataset_uuid") or m["run_id"]
        if key not in latest or m["run_id"] > latest[key]["run_id"]:
            latest[key] = m
    rows = [index_row(Run(runs_dir, m["run_id"])) for m in latest.values()]
    return sorted(rows, key=lambda r: r[-1], reverse=True)
