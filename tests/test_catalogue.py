"""The dataset catalogue (CONTRACT §78.7): list import, licence / commercial use, the read-only folder scan,
availability, hand edits, filters and exports."""
import io
import os
import re

import openpyxl
import pytest
from fastapi.testclient import TestClient

from dataops import catalogue as cat
from dataops import db as dbmod
from dataops.web.app import create_app

HEAD = ["데이터셋 이름", "출처", "데이터셋 링크", "ROI category", "Modality", "라이선스 정보", "데이터 수집 나라",
        "촬영 신체 범위", "신규 ROI 리스트", "전체 ROI 리스트", "다운로드 여부", "변환 여부", "검증상태", "검수 일자"]


def _row(name, link, licence, download=None, modality="CT", roi="OAR (liver)", country="미국 (NIH)",
         body="abdomen", new_roi="해당없음", all_roi="liver", verify="검증"):
    return [name, "정리현황(메인)", link, roi, modality, licence, country, body, new_roi, all_roi, download, None,
            verify, "2026-08-20"]


ROWS = [
    _row("02 Pancreas-CT TCIA", "https://tcia/pancreas", "(상업적 이용가능, 출처 표기) CC BY 3.0", "완료",
         roi="OAR (pancreas)", all_roi="pancreas"),
    _row("CHAOS-MR", "https://chaos", "(상업적 이용불가 — 비영리·동일조건) CC BY-NC-SA 4.0", "완료", modality="MR (T2-SPIR)"),
    _row("CMB-MML", "https://tcia/cmb-mml", "(상업적 이용가능, 출처 표기) CC BY 4.0", None, modality="CT"),
    _row("CMB-MML", "https://tcia/cmb-mml", "(상업적 이용가능, 출처 표기) CC BY 4.0", None, modality="PT"),
    _row("Never-Downloaded-Set", "https://example/nd", "(검수 필요) 데이터셋 라이선스 미확인", "X",
         new_roi="mandible, parotid", country="네덜란드"),
    _row("BTCV (신규 없어서 패스)", None, None, None, roi=None, modality=None, country=None, body=None, new_roi=None,
         all_roi=None, verify=None),
    _row("Restricted-Set", "https://example/r", "(접근 제한 — DUA/승인 필요) 챌린지 목적 외 사용 금지, 상업적 이용 불가", "완료"),
]


def write_list(path, rows=ROWS, extra_sheet=False):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "latest"
    ws.append(HEAD)
    for r in rows:
        ws.append(r)
    if extra_sheet:
        wb.create_sheet("other").append(["x"])
    wb.save(path)
    return path


@pytest.fixture
def nas(tmp_path):
    """Synthetic NAS roots: a public dataset folder with files, an empty one, an internal folder, a converted copy."""
    base = tmp_path / "nas"
    pub, onco, conv = base / "Public_dataset", base / "Onco_dataset", base / "processed_Onconii"
    for d in (pub / "02 Pancreas-CT TCIA" / "case1", pub / "03 Combined Healthy Abdominal Organ Segmentation (CHAOS)",
              pub / "Empty-Set", onco / "Inhouse_HNN", conv / "Pancreas-CT", pub / "Restricted-Set"):
        d.mkdir(parents=True)
    (pub / "02 Pancreas-CT TCIA" / "case1" / "a.dcm").write_bytes(b"x" * 100)
    (pub / "02 Pancreas-CT TCIA" / "b.dcm").write_bytes(b"y" * 50)
    (pub / "03 Combined Healthy Abdominal Organ Segmentation (CHAOS)" / "c.nii.gz").write_bytes(b"z")
    (pub / "Restricted-Set" / "r.dcm").write_bytes(b"r")
    (onco / "Inhouse_HNN" / "p.dcm").write_bytes(b"p" * 10)
    (conv / "Pancreas-CT" / "x.nii.gz").write_bytes(b"c")
    (pub / "dataset_URL.txt").write_text("notes")
    roots = ((str(pub), "source", "public"), (str(onco), "source", "internal"), (str(conv), "converted", "public"),
             (str(base / "missing_root"), "source", "public"))
    return base, roots


def _snapshot(root):
    return sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*"))


# --------------------------------------------------------------------------- unit: no DB
@pytest.mark.parametrize("text, licence, cu", [
    ("(상업적 이용가능, 출처 표기) CC BY 3.0", "CC BY 3.0", "yes"),
    ("(상업적 이용가능, 출처 표기) CC BY 4.0 — Data Access 표", "CC BY 4.0", "yes"),
    ("(상업적 이용불가 — 비영리·동일조건) CC BY-NC-SA 4.0", "CC BY-NC-SA 4.0", "no"),
    ("(상업적 이용불가 — 비영리·파생 금지) CC BY-NC-ND 4.0", "CC BY-NC-ND 4.0", "no"),
    ("(상업적 이용가능) CC0 1.0 — Harvard Dataverse", "CC0 1.0", "yes"),
    ("(접근 제한 — DUA/승인 필요) Data Access Agreement 서명, 챌린지 목적 외 사용 제한. 명시적 상업 허용 없음",
     "custom / DUA", "no"),
    ("(접근 제한 — DUA/승인 필요) NIH Controlled Data Access Policy", "custom / DUA", "unknown"),
    ("(검수 필요) 데이터셋 라이선스 미확인 — CC BY-NC-SA 4.0(논문)", "CC BY-NC-SA 4.0 (unverified)", "unknown"),
    ("(상업적 이용가능, 출처 표기) CC BY 4.0 — ⚠기존값 CC BY-NC-SA(비상업)와 상충 (검수 필요)", "CC BY 4.0", "unknown"),
    ("기존값 CC-BY-NC (검수 필요) — 레코드 판독 불가", "CC BY-NC (unverified)", "unknown"),
    ("혼합 — (상업적 이용가능, 출처 표기·동일조건 공유) CC BY-SA 4.0: IXI / (상업적 이용가능, 출처 표기) CC BY 4.0",
     "mixed (CC BY 4.0 / CC BY-SA 4.0)", "yes"),
    (None, "none stated", "unknown"),
])
def test_licence_classification_never_guesses_yes(text, licence, cu):
    got_licence, got_cu, why = cat.classify_licence(text, "public")
    assert (got_licence, got_cu) == (licence, cu) and why


def test_internal_is_commercially_usable():
    assert cat.classify_licence(None, "internal")[:2] == ("internal", "yes")
    assert cat.classify_licence("(상업적 이용불가) CC BY-NC 4.0", "internal")[1] == "yes"


@pytest.mark.parametrize("value, state, path", [
    ("완료", "downloaded", None), ("진행중(부분적재)", "downloading", None), ("미완(차단·동의필요)", "blocked", None),
    ("미완(보류·실패)", "failed", None), ("X", "not_downloaded", None), ("미착수(추출단계)", "not_downloaded", None),
    ("미상", "unknown", None), (None, None, None), ("/mnt/lsh/public_dataset/raw/X", "downloaded", "/mnt/lsh/public_dataset/raw/X"),
])
def test_list_availability(value, state, path):
    assert cat.list_availability(value) == (state, path)


def test_availability_rule_shows_list_vs_scan_difference():
    have = [{"path": "/n/a", "present": 1, "top_entries": 3}]
    assert cat.decide_availability("downloaded", None, have, [])[0] == "downloaded"
    s, note = cat.decide_availability("not_downloaded", None, have, [])
    assert s == "downloaded" and note.startswith("차이:")
    s, note = cat.decide_availability(None, None, have, [])
    assert s == "downloaded" and note.startswith("차이:")
    s, note = cat.decide_availability("downloaded", None, [], [])
    assert s == "unknown" and note.startswith("차이:")
    s, note = cat.decide_availability("downloaded", None, [], have)          # a proposal keeps the list's word
    assert s == "downloaded" and not note.startswith("차이:")
    assert cat.decide_availability(None, None, [], [])[0] == "not_downloaded"
    assert cat.decide_availability("blocked", None, [], [])[0] == "blocked"
    s, note = cat.decide_availability("downloaded", None, [{"path": "/n/e", "present": 1, "top_entries": 0}], [])
    assert s == "unknown" and "비어 있음" in note


def test_name_matching():
    def f(name, role="source"):
        return {"path": "/r/" + name, "name": name, "role": role}
    folders = [f("02 Pancreas-CT TCIA"), f("Decathlon"), f("Decathlon (Task03_Liver)", "converted"), f("TCIA"),
               f("SegRap2023"), f("Adrenal-ACC-Ki67-Seg"), f("09 Multi-Modality Abdominal Multi-Organ Segmentation (AMOS)"),
               f("TCGA-KIRC"), f("tcga_blca")]
    names = {"02 Pancreas-CT TCIA": "a", "Decathlon (Task03_Liver)": "b", "Decathlon (Task02_Heart)": "c",
             "SegRap 2025 Challenge (GTV dataset)": "d", "Adrenal-ACC-Ki67-Seg (Dicom seg format이라 시간 필요)": "e",
             "AMOS 2022 (CT)": "f", "TCGA-BLCA": "g", "TCGA-READ": "h", "Spinal-SEG (TCIA)": "i"}
    got = cat.propose_matches([{"key": k, "name": n} for n, k in names.items()], folders)
    short = {k: [(p.rsplit("/", 1)[1], m) for p, m, _ in v] for k, v in got.items()}
    assert short["a"] == [("02 Pancreas-CT TCIA", "exact")]
    assert ("Decathlon (Task03_Liver)", "exact") in short["b"]
    assert short["c"] == [("Decathlon", "name")]                         # not Task03_Liver
    assert short["d"] == []                                               # another edition
    assert short["e"] == [("Adrenal-ACC-Ki67-Seg", "exact")]              # a Korean note is not part of the name
    assert short["f"] == [("09 Multi-Modality Abdominal Multi-Organ Segmentation (AMOS)", "name")]
    assert short["g"] == [("tcga_blca", "exact")] and short["h"] == []    # no family-prefix proposals
    assert short["i"] == []                                               # "TCIA" alone is not a name


def test_tags():
    t = cat.tags_of({"Modality": "PET/CT, MR (T1)", "ROI category": "GTV, OAR", "촬영 신체 범위": "head and neck",
                     "데이터 수집 나라": "미국, 캐나다", "신규 ROI 리스트": "parotid", "검증상태": "검증"}, "public")
    assert {"CT", "MR", "PT"} <= set(t["modality"]) and {"GTV", "OAR"} <= set(t["roi_category"])
    assert "두경부" in t["body"] and t["country"] == ["미국", "캐나다"] and t["new_rois"] == "yes"
    assert cat.tags_of({"신규 ROI 리스트": "해당없음 (pancreas는 신규 ROI 아님)"}, "public")["new_rois"] == "no"
    assert cat.tags_of({}, "internal")["country"] == ["한국"]


def test_read_list_keys_repeated_rows_and_keeps_every_column(tmp_path):
    path = write_list(tmp_path / "list.xlsx", extra_sheet=True)
    headers, rows = cat.read_list(path)
    assert headers == HEAD and len(rows) == len(ROWS)
    keyed = cat.keyed_rows(rows)
    assert [r["ordinal"] for r in keyed if r["name"] == "CMB-MML"] == [1, 2]
    assert len({r["key"] for r in keyed}) == len(ROWS)
    with pytest.raises(cat.CatalogueError):
        cat.read_list(path, "nope")


def test_list_folders_reads_only(nas):
    root, roots = nas
    before = _snapshot(root)
    folders = cat.list_folders(roots)
    assert _snapshot(root) == before
    paths = {f["name"]: f for f in folders if f.get("path")}
    assert "dataset_URL.txt" not in paths and paths["Empty-Set"]["top_entries"] == 0
    assert paths["02 Pancreas-CT TCIA"]["top_entries"] == 2
    assert any(f.get("error") and f["root"].endswith("missing_root") for f in folders)
    r = cat.walk_size(str(root / "Public_dataset" / "02 Pancreas-CT TCIA"), max_rate=0)
    assert (r["file_count"], r["size_bytes"], r["complete"]) == (2, 150, True)


# --------------------------------------------------------------------------- DB + web
def _import_and_scan(conn, tmp_path, roots, rows=ROWS):
    rep = cat.import_list(conn, write_list(tmp_path / "list.xlsx", rows))
    sc = cat.scan(conn, folders=cat.list_folders(roots))
    return rep, sc


def test_import_scan_availability_and_reimport(settings, test_db, tmp_path, nas):
    root, roots = nas
    with dbmod.connection(test_db) as conn:
        dbmod.migrate(conn)
        before = _snapshot(root)
        rep, sc = _import_and_scan(conn, tmp_path, roots)
        assert _snapshot(root) == before                                  # the scan wrote nothing
        assert (rep["rows_read"], rep["entries"], rep["markers"], rep["added"]) == (7, 7, 1, 7)
        entries, extra = cat.load(conn)
        by = {(e["name"], e["ordinal"]): e for e in entries}
        p = by[("02 Pancreas-CT TCIA", 1)]
        assert p["eff"]["availability"] == "downloaded" and p["eff"]["commercial_use"] == "yes"
        assert {m["folder"]["path"] for m in p["folders"] if m["state"] == "confirmed"} == \
            {str(root / "Public_dataset" / "02 Pancreas-CT TCIA")}
        chaos = by[("CHAOS-MR", 1)]
        assert chaos["eff"]["commercial_use"] == "no" and chaos["eff"]["licence"] == "CC BY-NC-SA 4.0"
        assert chaos["n_proposed"] == 1 and chaos["eff"]["availability"] == "downloaded"   # proposed, list says done
        assert by[("Never-Downloaded-Set", 1)]["eff"]["availability"] == "not_downloaded"
        assert by[("CMB-MML", 2)]["eff"]["availability"] == "not_downloaded"
        assert by[("BTCV (신규 없어서 패스)", 1)]["is_marker"] == 1
        r = by[("Restricted-Set", 1)]
        assert r["eff"]["commercial_use"] == "no" and r["eff"]["availability"] == "downloaded"
        inh = by[("Inhouse_HNN", 1)]
        assert (inh["kind"], inh["eff"]["origin"], inh["eff"]["commercial_use"], inh["eff"]["availability"]) == \
            ("folder", "internal", "yes", "downloaded")
        assert not (inh["availability_note"] or "").startswith("차이:")
        assert str(root / "Public_dataset" / "Empty-Set") in {f["path"] for f in extra["unmatched"]}

        # filters: commercial use = yes keeps Pancreas, drops CHAOS-MR; counts per value
        rows, counts = cat.apply_filters(entries, {"commercial": ["yes"]})
        names = {e["name"] for e in rows}
        assert "02 Pancreas-CT TCIA" in names and "CHAOS-MR" not in names and "Inhouse_HNN" in names
        assert counts["commercial"]["no"] == 2 and counts["availability"]["downloaded"] >= 2
        rows, _ = cat.apply_filters(entries, {"new_rois": ["yes"]}, "parotid")
        assert [e["name"] for e in rows] == ["Never-Downloaded-Set"]

        # a hand edit is kept over a re-import that changes the same value, and reported
        eid = by[("Never-Downloaded-Set", 1)]["entry_id"]
        with pytest.raises(cat.CatalogueError):
            cat.edit_entry(conn, eid, {"commercial_use": "no"}, "", "lead")
        assert cat.edit_entry(conn, eid, {"commercial_use": "no"}, "license page says NC", "lead") == ["commercial_use"]
        rows2 = [r[:] for r in ROWS if r[0] != "CMB-MML"]
        next(r for r in rows2 if r[0] == "Never-Downloaded-Set")[5] = "(상업적 이용가능, 출처 표기) CC BY 4.0"   # now "yes"
        rows2.append(_row("New-Set", "https://example/new", "(상업적 이용가능) CC0 1.0", None))
        rep2 = cat.import_list(conn, write_list(tmp_path / "list2.xlsx", rows2))
        assert rep2["added"] == 1 and sorted(rep2["gone"]) == ["CMB-MML", "CMB-MML"]
        assert [c["name"] for c in rep2["changed"]] == ["Never-Downloaded-Set"]
        assert rep2["held_hand_edits"] == [{"name": "Never-Downloaded-Set", "field": "commercial_use",
                                            "imported": "yes", "hand_edit": "no"}]
        cat.scan(conn, folders=cat.list_folders(roots))
        entries, _ = cat.load(conn)
        e = next(x for x in entries if x["entry_id"] == eid)
        assert (e["commercial_use"], e["eff"]["commercial_use"]) == ("yes", "no")
        assert "CMB-MML" not in {x["name"] for x in entries}               # gone from the list: flagged, not shown

        # a person confirms the proposed CHAOS folder; the size walk fills sizes
        chaos = next(x for x in entries if x["name"] == "CHAOS-MR")
        fid = chaos["folders"][0]["folder_id"]
        cat.decide_match(conn, chaos["entry_id"], fid, "confirmed", "lead")
        res = cat.size_walk(conn, budget_s=60, max_rate=0)
        assert res["walked"] >= 3 and res["left"] == 0
        entries, _ = cat.load(conn)
        p = next(x for x in entries if x["name"] == "02 Pancreas-CT TCIA")
        assert (p["file_count"], p["size_bytes"]) == (2, 150)
        sm = cat.summary(conn)
        assert sm["list_rows"] == 6 and sm["internal_folders"] == 1 and sm["commercial_by_origin"]["internal"] == {"yes": 1}
        assert _snapshot(root) == before


def test_catalogue_pages_filters_exports_and_edits(settings, test_db, tmp_path, nas):
    root, roots = nas
    settings.db = test_db
    settings.input_roots = (str(root / "Public_dataset"),)
    with dbmod.connection(test_db) as conn:
        dbmod.migrate(conn)
        _import_and_scan(conn, tmp_path, roots)
        entries, _ = cat.load(conn)
    pid = next(e["entry_id"] for e in entries if e["name"] == "02 Pancreas-CT TCIA")
    chaos = next(e["entry_id"] for e in entries if e["name"] == "CHAOS-MR")
    client = TestClient(create_app(settings))
    r = client.get("/catalog")
    assert r.status_code == 200 and "02 Pancreas-CT TCIA" in r.text and "CHAOS-MR" in r.text and "비상업 제외" in r.text
    assert "상업 불가" in r.text and "받음" in r.text and 'href="/catalog?commercial=yes&amp;commercial=unknown"' in r.text
    r = client.get("/catalog?commercial=yes")
    assert "02 Pancreas-CT TCIA" in r.text and ">CHAOS-MR<" not in r.text
    r = client.get("/catalog?commercial=yes&origin=public&q=pancreas")
    assert re.search(r"<b>1</b> / 8 항목", r.text)
    csv_text = client.get("/catalog.csv?commercial=no").text
    assert "CHAOS-MR" in csv_text and "Pancreas" not in csv_text and "라이선스 정보" in csv_text
    x = client.get("/catalog.xlsx?availability=downloaded")
    ws = openpyxl.load_workbook(io.BytesIO(x.content)).active
    names = [row[0].value for row in ws.iter_rows(min_row=2, min_col=len(cat.EXPORT_FIXED) + 1,
                                                  max_col=len(cat.EXPORT_FIXED) + 1)]
    assert "02 Pancreas-CT TCIA" in names and "Never-Downloaded-Set" not in names
    assert ws.max_column == len(cat.EXPORT_FIXED) + len(HEAD)
    r = client.get(f"/catalog/{pid}")
    assert r.status_code == 200 and "/new?dir=" in r.text and "라이선스 정보" in r.text and "(상업적 이용가능" in r.text
    r = client.get(f"/catalog/{chaos}")
    assert "이 폴더가 맞음" in r.text and "/new?dir=" not in r.text
    assert client.get("/catalog/999999").status_code == 404
    # writes need the CSRF token and a note
    assert client.post(f"/catalog/{pid}/edit", data={"commercial_use": "no", "note": "x", "by": "t"}).status_code == 403
    token = client.cookies.get("dataops_csrf")
    r = client.post(f"/catalog/{pid}/edit", data={"csrf": token, "commercial_use": "no", "note": "", "by": "t"},
                    follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    r = client.post(f"/catalog/{pid}/edit", data={"csrf": token, "commercial_use": "no", "note": "checked", "by": "t"},
                    follow_redirects=True)
    assert "사람이 고친 값" in r.text and "checked" in r.text
    assert ">02 Pancreas-CT TCIA<" not in client.get("/catalog?commercial=yes").text
    r = client.post(f"/catalog/{chaos}/match", data={"csrf": token, "folder_id": "abc", "state": "confirmed", "by": "t"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_catalogue_page_without_db_or_tables(settings, test_db):
    client = TestClient(create_app(settings))
    assert client.get("/catalog").status_code == 200
    settings.db = test_db                                            # schema exists, tables do not
    r = TestClient(create_app(settings)).get("/catalog")
    assert r.status_code == 200 and "db-migrate" in r.text


def test_migration_is_additive():
    sql = (cat.Path(dbmod.MIGRATIONS_DIR) / "0008_dataset_catalogue.sql").read_text(encoding="utf-8")
    assert not re.search(r"\b(DROP|ALTER|DELETE|UPDATE|TRUNCATE)\b", sql, re.I)
    assert os.path.basename(sorted(cat.Path(dbmod.MIGRATIONS_DIR).glob("0*.sql"))[-1]) == "0008_dataset_catalogue.sql"
