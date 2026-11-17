"""The dataset catalogue (CONTRACT §78.7): every known dataset, downloaded or not.

Entries come from the audited dataset list (an .xlsx; every column is kept) and, for in-house data, from the
folders of the internal NAS roots. A read-only scan of the NAS roots tells which entries have files here, where,
how many and how large; the list's own 다운로드 여부 is the starting point and the scan corrects it, showing the
difference. Origin and commercial use follow the owner's rule: internal data is commercially usable; public data
is classified from its licence text, never guessed to "yes".

Nothing here downloads, moves or deletes a file (U156). The scan only lists folders; the size walk reads metadata
(no file contents), throttled, off-hours, cached in the DB.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import nfs

SHEET = "latest"
AVAILABILITY = ("not_downloaded", "downloading", "downloaded", "blocked", "failed", "unknown")
AVAILABILITY_KO = {"not_downloaded": "받지 않음", "downloading": "받는 중", "downloaded": "받음",
                   "blocked": "차단·동의 필요", "failed": "실패·보류", "unknown": "모름"}
COMMERCIAL = ("yes", "no", "unknown")
COMMERCIAL_KO = {"yes": "상업 가능", "no": "상업 불가", "unknown": "확인 필요"}
ORIGINS = ("public", "internal")
ORIGIN_KO = {"public": "공개", "internal": "내부"}

# The NAS roots the scan lists (read only). role: where the original files are, or a converted copy.
ROOTS = (("/mnt2/data/Public_dataset", "source", "public"),
         ("/mnt2/data/Restricted_dataset_need_permission", "source", "public"),
         ("/mnt2/data/Onco_dataset", "source", "internal"),
         ("/mnt2/data/pseudonymized_data", "source", "internal"),
         ("/mnt/lsh/public_dataset/raw", "source", "public"),
         ("/mnt2/data/processed_Public_dataset_dicom", "converted", "public"),
         ("/mnt2/data/processed_Public_dataset_Onconii", "converted", "public"))
SKIP_NAMES = re.compile(r"^(\.|00 |#)|\.(txt|csv|zip|xlsx?|json|md)$", re.I)   # notes and archives, not datasets

# Columns of the list the code reads (the rest are kept as they are in `fields`).
C_NAME, C_SOURCE, C_LINK = "데이터셋 이름", "출처", "데이터셋 링크"
C_ROI_CAT, C_MODALITY, C_LICENCE = "ROI category", "Modality", "라이선스 정보"
C_COUNTRY, C_BODY, C_NEW_ROI, C_ALL_ROI = "데이터 수집 나라", "촬영 신체 범위", "신규 ROI 리스트", "전체 ROI 리스트"
C_DOWNLOAD, C_CONVERSION, C_VERIFY = "다운로드 여부", "변환 여부", "검증상태"


class CatalogueError(Exception):
    pass


# --------------------------------------------------------------------------- reading the list
def read_list(path: Path, sheet: str = SHEET) -> tuple[list[str], list[dict]]:
    """(headers, rows) of the audited list; each row {"row": spreadsheet row, "values": {header: value}}."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:                                   # noqa: BLE001 - any unreadable workbook
        raise CatalogueError(f"목록 파일을 읽을 수 없습니다: {e}") from None
    if sheet not in wb.sheetnames:
        raise CatalogueError(f"시트 {sheet!r}가 없습니다 (있는 시트: {wb.sheetnames})")
    it = wb[sheet].iter_rows(values_only=True)
    headers = [str(h).strip() if h is not None else f"col{i + 1}" for i, h in enumerate(next(it, ()))]
    if C_NAME not in headers:
        raise CatalogueError(f"첫 행에 '{C_NAME}' 열이 없습니다")
    rows = []
    for n, values in enumerate(it, start=2):
        vals = {h: _cell(v) for h, v in zip(headers, values)}
        if not any(v not in (None, "") for v in vals.values()):
            continue
        rows.append({"row": n, "values": vals})
    wb.close()
    return headers, rows


def _cell(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.date().isoformat() if isinstance(v, dt.datetime) and not (v.hour or v.minute or v.second) \
            else v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def entry_key(kind: str, *parts) -> str:
    return hashlib.sha1("\x1f".join([kind, *[str(p or "") for p in parts]]).encode("utf-8")).hexdigest()


def keyed_rows(rows: list[dict]) -> list[dict]:
    """Give each list row its key: name + link + the n-th time that pair occurs (the list repeats a dataset once
    per modality or per weekly source)."""
    seen = Counter()
    out = []
    for r in rows:
        v = r["values"]
        name, link = str(v.get(C_NAME) or "").strip(), (str(v.get(C_LINK)).strip() if v.get(C_LINK) else None)
        if not name:
            continue
        seen[(name, link)] += 1
        out.append(dict(r, name=name, link=link, ordinal=seen[(name, link)],
                        key=entry_key("list", name, link, seen[(name, link)])))
    return out


# --------------------------------------------------------------------------- licence and commercial use
CC_RE = re.compile(r"\bCC[\s-]*(0|ZERO|BY(?:[\s-]*(?:NC|SA|ND))*)(?:[\s-]*(\d\.\d))?", re.I)
NO_PATTERNS = ("상업적 이용불가", "상업적 이용 불가", "상업 이용 불가", "상업적 이용 금지", "상업적 이용은 데이터 소유자",
               "research purposes only", "목적 외 사용 금지", "목적 외 사용 제한", "챌린지 목적 한정",
               "may not be used to train", "cannot be used in another context")
YES_TAGS = ("(상업적 이용가능", "(상업적 이용 가능")
UNSURE_PATTERNS = ("검수 필요", "상충")


def licence_name(text: str | None) -> str | None:
    """The first Creative Commons licence named in the text, normalised (CC BY-NC-SA 4.0, CC0 1.0, …)."""
    m = CC_RE.search(text or "")
    if not m:
        return "CC0 1.0" if re.search(r"public domain|퍼블릭 도메인", text or "", re.I) else None
    body = m.group(1).upper()
    if body in ("0", "ZERO"):
        return "CC0 1.0"
    parts = [p for p in re.split(r"[\s-]+", body) if p]
    name = "CC " + "-".join(parts)
    return f"{name} {m.group(2)}" if m.group(2) else name


def classify_licence(text: str | None, origin: str) -> tuple[str, str, str]:
    """(licence, commercial_use, reason). Internal -> yes (the owner's rule). Public: an explicit non-commercial,
    research-only or challenge-only text -> no; an explicit commercial-use tag without an open question -> yes;
    everything unclear -> unknown, never yes."""
    if origin == "internal":
        return "internal", "yes", "내부 데이터 — 공개 데이터가 아니므로 상업적 이용 가능 (U208)"
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t or t.lower() in ("none", "-"):
        return "none stated", "unknown", "라이선스 정보 없음"
    cc = licence_name(t)
    restricted = t.startswith("(접근 제한") or "DUA" in t[:40]
    if t.startswith("(상업적 이용불가") or t.startswith("(상업적 이용 불가"):
        return cc or "non-commercial", "no", "목록: 상업적 이용불가"
    if restricted:
        lic = "custom / DUA"
        if any(p in t for p in NO_PATTERNS):
            return lic, "no", "접근 제한 + 상업 이용 불가/목적 한정 문언"
        return lic, "unknown", "접근 제한 (DUA/승인) — 상업 이용 가부가 승인 조건에 달림"
    if t.startswith(YES_TAGS):
        if any(p in t for p in UNSURE_PATTERNS):
            return cc or "unverified", "unknown", "목록: 상업적 이용가능이지만 검수 필요/상충 표기"
        return cc or "custom", "yes", "목록: 상업적 이용가능"
    if t.startswith("혼합"):
        tags = re.findall(r"\((상업적 이용[^,)]*)", t)
        if tags and all("가능" in x for x in tags) and not any(p in t for p in UNSURE_PATTERNS + NO_PATTERNS):
            return "mixed (" + " / ".join(sorted({x.group(0) for x in CC_RE.finditer(t)})) + ")", "yes", \
                "목록: 혼합 — 모든 구성 라이선스가 상업적 이용가능"
        return "mixed", "unknown", "목록: 혼합 라이선스"
    if "research" in t.lower() and "only" in t.lower() or "연구 목적 한정" in t:
        return "research-only", "unknown" if "검수 필요" in t else "no", "연구 목적 한정 문언"
    return (cc + " (unverified)") if cc else "unverified", "unknown", "목록: 검수 필요 / 라이선스 미확인"


# --------------------------------------------------------------------------- filter tags
MODALITY_RE = [("CBCT", r"\bCBCT\b"), ("CT", r"(?<![A-Za-z])CT(?![A-Za-z])|\bCTA\b|4DCT"),
               ("MR", r"\bMRI?\b|\bMR[A-Z]|T1|T2|FLAIR|DWI|자기공명"),
               ("PT", r"\bPET\b|\bPT\b"), ("SPECT", r"SPECT"), ("NM", r"\bNM\b"),
               ("US", r"\bUS\b|초음파|ultrasound"), ("MG", r"\bMG\b|mammo"),
               ("X-ray", r"\bCR\b|\bDX\b|X-?ray|엑스선|radiograph"), ("XA/RF", r"\bXA\b|\bRF\b|angiograph"),
               ("RT", r"RTSTRUCT|RTDOSE|RTPLAN|\bdose\b")]
ROI_RE = [("OAR", r"\bOAR|정상 장기|organ"), ("GTV", r"\bGTV"), ("CTV", r"\bCTV"), ("PTV", r"\bPTV"),
          ("종양·병변", r"tumou?r|lesion|종양|병변|nodule|결절|metasta|전이|cancer|암\b|암 "),
          ("없음", r"^해당없음|^해당 없음|^없음|미제공")]
BODY_RE = [("두부·뇌", r"head|brain|뇌|두부|두개|skull|cerebr|intracranial"),
           ("두경부", r"head[- ]?and[- ]?neck|두경부|\bneck\b|경부|\bHN\b|\bHNC|oropharyn|구인두|nasopharyn"),
           ("흉부", r"chest|thora|lung|폐|흉부|breast|유방|heart|심장|cardiac|esophag|식도|mediastin"),
           ("복부", r"abdom|복부|liver|간\b|간 |kidney|신장|pancrea|췌장|spleen|비장|colon|대장|stomach|위\b"),
           ("골반", r"pelvi|골반|prostat|전립선|cervi|자궁|bladder|방광|rect|직장|uter"),
           ("척추", r"spine|spinal|척추|vertebra"), ("전신", r"whole[- ]?body|전신|total[- ]?body|multi[- ]?region|다부위"),
           ("사지", r"\blimb|\bknee|\bhip\b|extremit|팔|다리|무릎")]
COUNTRY_RE = [("미국", r"미국|USA|United States|\bU\.S\."), ("네덜란드", r"네덜란드|Netherlands|Dutch"),
              ("중국", r"중국|China|Chinese"), ("영국", r"영국|\bUK\b|United Kingdom|England"),
              ("독일", r"독일|German"), ("프랑스", r"프랑스|France|French"), ("캐나다", r"캐나다|Canada"),
              ("한국", r"한국|Korea"), ("일본", r"일본|Japan"), ("스위스", r"스위스|Switzerland"),
              ("슬로베니아", r"슬로베니아|Slovenia"), ("튀르키예", r"터키|튀르키예|Turkey|Türkiye"),
              ("스웨덴", r"스웨덴|Sweden"), ("키프로스", r"키프로스|Cyprus"), ("브라질", r"브라질|Brazil"),
              ("이탈리아", r"이탈리아|Ital"), ("스페인", r"스페인|Spain"), ("벨기에", r"벨기에|Belgi"),
              ("오스트리아", r"오스트리아|Austria"), ("인도", r"인도\b|India"), ("호주", r"호주|Australia"),
              ("덴마크", r"덴마크|Denmark"), ("노르웨이", r"노르웨이|Norway"), ("핀란드", r"핀란드|Finland"),
              ("이란", r"이란\b|Iran"), ("대만", r"대만|Taiwan"), ("싱가포르", r"싱가포르|Singapore"),
              ("이스라엘", r"이스라엘|Israel"), ("포르투갈", r"포르투갈|Portugal"), ("헝가리", r"헝가리|Hungar"),
              ("폴란드", r"폴란드|Poland"), ("그리스", r"그리스|Greece"), ("러시아", r"러시아|Russia"),
              ("이집트", r"이집트|Egypt"), ("사우디", r"사우디|Saudi"), ("멕시코", r"멕시코|Mexico"),
              ("유럽", r"유럽|Europe"), ("다국가", r"다국|다기관·국제|국제|전 세계|worldwide|international")]
NO_NEW_ROI = re.compile(r"^(해당\s*없음|없음|-|none|n/?a)|신규\s*(ROI\s*)?(없음|아님)|신규 ROI가 없", re.I)


def _tags(text, table) -> list[str]:
    s = str(text or "")
    return [name for name, rx in table if re.search(rx, s, re.I)]


def tags_of(values: dict, origin: str) -> dict:
    """Normalised filter values of one entry (the raw texts stay in `fields`)."""
    modality = _tags(values.get(C_MODALITY), MODALITY_RE) or (["기타"] if values.get(C_MODALITY) else ["미기재"])
    roi = _tags(values.get(C_ROI_CAT), ROI_RE) or (["기타"] if values.get(C_ROI_CAT) else ["미기재"])
    if "없음" in roi and len(roi) > 1 and str(values.get(C_ROI_CAT) or "").startswith("해당없음"):
        roi = ["없음"]
    body = _tags(values.get(C_BODY), BODY_RE) or (["기타"] if values.get(C_BODY) else ["미기재"])
    if origin == "internal" and not values.get(C_COUNTRY):
        country = ["한국"]
    else:
        country = _tags(values.get(C_COUNTRY), COUNTRY_RE) or (["미상"] if values.get(C_COUNTRY) else ["미기재"])
    new_roi = str(values.get(C_NEW_ROI) or "").strip()
    return {"modality": modality, "roi_category": roi, "body": body, "country": country,
            "verification": str(values.get(C_VERIFY) or "없음"),
            "new_rois": "yes" if new_roi and not NO_NEW_ROI.search(new_roi) else "no"}


# --------------------------------------------------------------------------- availability from the list
def list_availability(value) -> tuple[str | None, str | None]:
    """(availability the list states, a path it names). None when the list says nothing."""
    v = str(value or "").strip()
    if not v:
        return None, None
    if v.startswith("/"):
        return "downloaded", v
    if v.startswith("완료"):
        return "downloaded", None
    if v.startswith("진행중"):
        return "downloading", None
    if "차단" in v or "동의" in v:
        return "blocked", None
    if "보류" in v or "실패" in v:
        return "failed", None
    if v in ("X", "x", "미착수") or v.startswith("미착수"):
        return "not_downloaded", None
    return "unknown", None


# --------------------------------------------------------------------------- folders on the NAS (read only)
def norm(name: str) -> str:
    s = re.sub(r"^\s*\d{1,3}\s+", "", str(name))          # the "02 " index of Public_dataset folders
    return re.sub(r"[^0-9a-z가-힣]+", "", s.lower())


HANGUL = re.compile(r"[가-힣]")
# Parenthesised parts that are a note, not a different dataset: a Korean team note, a modality, the archive.
NOTE_PAREN = re.compile(r"^\s*((CT|MRI?|PET|PT|US|CBCT|SPECT|NM|DX|CR|MG|XA|NIfTI|DICOM|3D|2D)(\s*[/,+&]\s*)?)+\s*$"
                        r"|^\s*(TCIA|TCIA collection|Kaggle|Zenodo|Synapse)\s*$", re.I)
GENERIC = {"tcia", "data", "dataset", "datasets", "challenge", "collection", "kaggle", "zenodo", "synapse",
           "grandchallenge", "medical", "imaging", "segmentation", "public", "task", "brain", "lung", "liver"}


def _is_note(part: str) -> bool:
    return bool(HANGUL.search(part) or NOTE_PAREN.match(part))


def core(name: str) -> str:
    """The normalised name without its notes: "Adrenal-ACC-Ki67-Seg (Dicom seg format이라 …)" and
    "Soft-tissue-Sarcoma (MR)" are the folders "Adrenal-ACC-Ki67-Seg" and "Soft-tissue-Sarcoma"."""
    s = str(name)
    s = re.sub(r"\(([^()]*)\)", lambda m: " " if _is_note(m.group(1)) else m.group(0), s)
    s = re.sub(r"\([^()]*$", lambda m: " " if HANGUL.search(m.group(0)) else m.group(0), s)   # unclosed note
    return norm(s)


def _qualifiers(name: str) -> set[str]:
    """Parenthesised parts that name a variant (Task03_Liver, task1), not a note."""
    return {norm(p) for p in re.findall(r"\(([^)]*)\)", str(name)) if not _is_note(p) and 0 < len(p) <= 80}


def aliases(name: str) -> dict[str, str]:
    """{alias: kind} a dataset or folder goes by: `full` the name without notes; `base` without any parenthesised
    part; `paren` a parenthesised part; `acronym` a short upper-case / numbered token (AMOS, KiTS-2023, HaN-Seg)
    with and without its year or number. Generic words (TCIA, dataset, …) are never an alias."""
    s = re.sub(r"^\s*\d{1,3}\s+", "", str(name))
    out = {}

    def add(a, kind):
        if len(a) >= 4 and a not in out and a not in GENERIC:
            out[a] = kind
    add(core(s), "full")
    add(norm(re.sub(r"\([^)]*\)?", " ", s)), "base")
    for p in re.findall(r"\(([^)]*)\)", s):
        if not _is_note(p):
            add(norm(p), "paren")
    toks = re.split(r"[\s(),:—]+", s)
    for i, tok in enumerate(toks[:3]):
        variants = {tok}
        head, _, rest = tok.partition("-")
        if rest and (NOTE_PAREN.match(rest) or re.fullmatch(r"\d{2,4}", rest)):   # CHAOS-MR, KiTS-2023
            variants.add(head)
        for t in variants:
            if 3 <= len(t) <= 14 and (sum(ch.isupper() for ch in t) >= 2 or re.search(r"\d", t)) \
                    and re.search(r"[A-Za-z]{2}", t):
                n = norm(t)
                year = re.search(r"(?:19|20)?(\d{2})$", n)
                nxt = toks[i + 1] if i + 1 < len(toks) else ""
                if not year and re.fullmatch(r"(19|20)?\d{2}", nxt):                # "AMOS 2022", "SegRap 2025"
                    year = re.search(r"(\d{2})$", nxt)
                yy = year.group(1) if year else ""
                add(n, "acronym:" + yy)
                add(re.sub(r"(19|20)?\d{2}$", "", n), "acronym:" + yy)
    return out


def _alias_match(ka: str, kb: str) -> bool:
    """Two acronym stems with different edition numbers (SegRap 2025 vs SegRap2023) are different datasets."""
    ya, yb = ka.partition(":")[2], kb.partition(":")[2]
    return not (ya and yb and ya != yb)


def list_folders(roots=ROOTS) -> list[dict]:
    """Top-level folders of each root: one directory listing per root plus one per folder (to tell an empty
    folder). No file is opened; nothing is written."""
    out = []
    for root, role, origin in roots:
        try:
            names = nfs.listdir(root)
        except OSError as e:
            out.append({"root": root, "error": f"{type(e).__name__}: {e}"})
            continue
        for name in sorted(names):
            if SKIP_NAMES.search(name):
                continue
            path = os.path.join(root, name)
            try:
                if not nfs.is_dir(path):
                    continue
                top = len(nfs.listdir(path))
                err = None
            except OSError as e:
                top, err = None, f"{type(e).__name__}: {e}"[:500]
            out.append({"root": root, "role": role, "origin": origin, "name": name, "path": path,
                        "top_entries": top, "error": err})
    return out


def propose_matches(entries: list[dict], folders: list[dict]) -> dict[str, list[tuple[str, str, float]]]:
    """{entry key: [(folder path, method, score)]}. `exact`: the names are equal once normalised and their notes
    dropped; `name`: an alias of one equals or contains an alias of the other (a proposal a person confirms). Two names whose variant qualifiers differ (Decathlon (Task02_Heart) vs
    Decathlon (Task03_Liver)) are not proposed."""
    fal = [(f, core(re.sub(r"^\s*\d{1,3}\s+", "", f["name"])), aliases(f["name"]), _qualifiers(f["name"]))
           for f in folders if f.get("path")]
    out = {}
    for e in entries:
        en, ea, eq = core(e["name"]), aliases(e["name"]), _qualifiers(e["name"])
        found = []
        for f, fn, fa, fq in fal:
            if en and en == fn:
                found.append((f, "exact", 1.0))
                continue
            if eq and fq and not eq & fq:
                continue
            best = 0.0
            for a, ka in ea.items():
                for b, kb in fa.items():
                    acr = ka.startswith("acronym") or kb.startswith("acronym")
                    if a == b:
                        if _alias_match(ka, kb):
                            best = max(best, 0.8 if acr else 0.9)
                    elif len(a) >= 6 and len(b) >= 6 and (a in b or b in a) and not acr:
                        best = max(best, 0.5 + 0.4 * min(len(a), len(b)) / max(len(a), len(b)))
            if best >= 0.6:
                found.append((f, "name", round(best, 3)))
        if any(x[1] == "exact" and x[0]["role"] == "source" for x in found):
            found = [x for x in found if x[1] == "exact"]
        elif any(x[1] == "exact" for x in found):            # exact only in a converted tree: keep source proposals
            found = [x for x in found if x[1] == "exact" or (x[0]["role"] == "source" and x[2] >= 0.8)]
        found.sort(key=lambda x: (-x[2], x[0]["role"] != "source"))
        out[e["key"]] = [(f["path"], m, sc) for f, m, sc in found[:5]]
    return out


# --------------------------------------------------------------------------- the availability rule
def decide_availability(listed: str | None, list_path: str | None, confirmed: list[dict],
                        proposed: list[dict]) -> tuple[str, str]:
    """(availability, note). Files in a confirmed folder win over the list. A note starting with "차이:" marks an
    entry where the list and the scan disagree (list says done and no folder, or a folder the list does not
    know). A folder only proposed by name keeps the list's value until a person confirms it."""
    have = [f for f in confirmed if f.get("present", 1) and (f.get("top_entries") or 0) > 0]
    empty = [f for f in confirmed if f.get("present", 1) and f.get("top_entries") == 0]
    where = ", ".join(f["path"] for f in have)
    ko = AVAILABILITY_KO.get(listed, "기록 없음") if listed else "기록 없음"
    if have:
        if listed == "downloading":
            return "downloading", f"목록=받는 중, 폴더 있음: {where}"
        if listed == "downloaded":
            return "downloaded", f"폴더: {where}"
        return "downloaded", f"차이: 목록={ko}, 폴더에 파일 있음: {where}"
    notes = []
    if list_path:
        notes.append(f"목록의 경로 {list_path} 없음")
    if empty:
        notes.append("폴더는 있으나 비어 있음: " + ", ".join(f["path"] for f in empty))
    cands = [f for f in proposed if (f.get("top_entries") or 0) > 0]
    if cands:
        notes.append(f"이름이 비슷한 폴더 {len(cands)}개 — 사람이 확인: " + ", ".join(f["path"] for f in cands[:3]))
    tail = ("; " + "; ".join(notes)) if notes else ""
    if listed == "downloaded":
        if cands and not list_path:
            return "downloaded", "목록=받음, 폴더 후보 확인 필요" + tail
        return "unknown", "차이: 목록=받음, 확인된 폴더 없음" + tail
    if listed is None:
        return "not_downloaded", ("목록에 다운로드 기록 없음, 확인된 폴더 없음" + tail)
    return listed, f"목록={ko}, 확인된 폴더 없음" + tail


def effective(e: dict, field: str):
    """A field's value with a hand edit applied."""
    return e.get(f"{field}_override") or e.get(field)


# --------------------------------------------------------------------------- DB
def _q(cur, sql, args=()):
    cur.execute(sql, args)
    return list(cur.fetchall())


def upsert_folders(cur, folders: list[dict]) -> dict[str, dict]:
    """Record the listed folders; a folder not listed this time is marked absent. {path: row}."""
    seen = set()
    for f in folders:
        if not f.get("path"):
            continue
        sha = hashlib.sha1(f["path"].encode("utf-8")).hexdigest()
        seen.add(sha)
        cur.execute("""INSERT INTO catalog_folders (path_sha, path, root, role, name, present, top_entries, error)
                       VALUES (%s,%s,%s,%s,%s,1,%s,%s)
                       ON DUPLICATE KEY UPDATE present=1, top_entries=VALUES(top_entries), error=VALUES(error),
                                               role=VALUES(role), seen_at=CURRENT_TIMESTAMP""",
                    (sha, f["path"], f["root"], f["role"], f["name"], f.get("top_entries"), f.get("error")))
    scanned_roots = {f["root"] for f in folders if f.get("path") or not f.get("error")}
    for r in _q(cur, "SELECT folder_id, path_sha, root FROM catalog_folders WHERE present=1"):
        if r["path_sha"] not in seen and r["root"] in scanned_roots:
            cur.execute("UPDATE catalog_folders SET present=0 WHERE folder_id=%s", (r["folder_id"],))
    return {r["path"]: r for r in _q(cur, "SELECT * FROM catalog_folders")}


ENTRY_COLS = ("kind", "name", "link", "ordinal", "list_row", "list_source", "is_marker", "fields", "tags", "origin",
              "licence", "licence_text", "commercial_use", "commercial_reason", "list_download", "list_availability",
              "conversion")
COMPARED = ("name", "link", "list_source", "is_marker", "fields", "origin", "licence", "commercial_use",
            "list_download")


def list_entry(r: dict) -> dict:
    v = r["values"]
    origin = "public"
    licence, cu, why = classify_licence(v.get(C_LICENCE), origin)
    listed, _ = list_availability(v.get(C_DOWNLOAD))
    return {"key": r["key"], "kind": "list", "name": r["name"][:1024], "link": r["link"], "ordinal": r["ordinal"],
            "list_row": r["row"], "list_source": (str(v.get(C_SOURCE) or "") or None),
            "is_marker": 0 if r["link"] else 1, "fields": json.dumps(v, ensure_ascii=False, default=str),
            "tags": json.dumps(tags_of(v, origin), ensure_ascii=False), "origin": origin, "licence": licence,
            "licence_text": v.get(C_LICENCE), "commercial_use": cu, "commercial_reason": why,
            "list_download": (str(v.get(C_DOWNLOAD))[:255] if v.get(C_DOWNLOAD) is not None else None),
            "list_availability": listed, "conversion": (str(v.get(C_CONVERSION))[:255] if v.get(C_CONVERSION) else None)}


def folder_entry(f: dict) -> dict:
    licence, cu, why = classify_licence(None, "internal")
    return {"key": entry_key("folder", f["path"]), "kind": "folder", "name": f["name"], "link": None, "ordinal": 1,
            "list_row": None, "list_source": f"폴더 {f['root']}", "is_marker": 0,
            "fields": json.dumps({"폴더": f["path"]}, ensure_ascii=False),
            "tags": json.dumps(tags_of({}, "internal"), ensure_ascii=False), "origin": "internal",
            "licence": licence, "licence_text": None, "commercial_use": cu, "commercial_reason": why,
            "list_download": None, "list_availability": None, "conversion": None}


def _upsert_entries(cur, rows: list[dict], import_id: int, kind: str) -> dict:
    existing = {r["entry_key"]: r for r in _q(cur, "SELECT * FROM catalog_entries WHERE kind=%s", (kind,))}
    added, changed, unchanged, held = [], [], [], []
    for row in rows:
        old = existing.get(row["key"])
        vals = {c: row[c] for c in ENTRY_COLS}
        if old is None:
            cols = ", ".join(["entry_key", *ENTRY_COLS, "availability", "import_id"])
            cur.execute(f"INSERT INTO catalog_entries ({cols}) VALUES ({', '.join(['%s'] * (len(ENTRY_COLS) + 3))})",
                        [row["key"], *vals.values(), "unknown", import_id])
            added.append(row["name"])
            continue
        diff = [c for c in COMPARED if _same(old.get(c)) != _same(vals[c])]
        for c in ("origin", "licence", "commercial_use"):
            if c in diff and old.get(f"{c}_override"):
                held.append({"name": row["name"], "field": c, "imported": vals[c], "hand_edit": old[f"{c}_override"]})
        if diff or old.get("gone_at") or old.get("list_row") != vals["list_row"]:
            sets = ", ".join(f"`{c}`=%s" for c in vals)
            cur.execute(f"UPDATE catalog_entries SET {sets}, import_id=%s, gone_at=NULL,"
                        f" updated_at=CURRENT_TIMESTAMP WHERE entry_id=%s", [*vals.values(), import_id, old["entry_id"]])
        (changed if diff else unchanged).append({"name": row["name"], "fields": diff} if diff else row["name"])
    keys = {r["key"] for r in rows}
    gone = [o["name"] for k, o in existing.items() if k not in keys and not o.get("gone_at")]
    for k, o in existing.items():
        if k not in keys and not o.get("gone_at"):
            cur.execute("UPDATE catalog_entries SET gone_at=CURRENT_TIMESTAMP WHERE entry_id=%s", (o["entry_id"],))
    return {"added": added, "changed": changed, "unchanged": unchanged, "gone": gone, "held_hand_edits": held}


def _same(v):
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.dumps(json.loads(v), sort_keys=True, ensure_ascii=False)
        except ValueError:
            return v
    return v


def import_list(conn, path: Path, sheet: str = SHEET, by: str = "cli", dry_run: bool = False) -> dict:
    """Import (or re-import) the audited list. Entries are matched by name + link (+ occurrence); a newer list
    reports added / changed / gone; hand edits (the *_override columns) are never touched and a changed imported
    value under a hand edit is reported. Then the folders are listed and availability recomputed."""
    path = Path(path)
    headers, raw = read_list(path, sheet)
    rows = [list_entry(r) for r in keyed_rows(raw)]
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO catalog_imports (kind, file_name, file_sha256, sheet, rows_read, run_by)"
                    " VALUES ('list', %s, %s, %s, %s, %s)", (path.name, sha, sheet, len(raw), by))
        import_id = cur.lastrowid
        res = _upsert_entries(cur, rows, import_id, "list")
        report = {"file": path.name, "sha256": sha, "sheet": sheet, "columns": len(headers), "headers": headers,
                  "rows_read": len(raw), "entries": len(rows), "markers": sum(r["is_marker"] for r in rows),
                  "added": len(res["added"]), "changed": res["changed"], "gone": res["gone"],
                  "held_hand_edits": res["held_hand_edits"]}
        cur.execute("UPDATE catalog_imports SET added=%s, changed=%s, gone=%s, unchanged=%s, report=%s"
                    " WHERE import_id=%s", (len(res["added"]), len(res["changed"]), len(res["gone"]),
                                            len(res["unchanged"]), json.dumps(report, ensure_ascii=False),
                                            import_id))
    if dry_run:
        conn.rollback()
        return dict(report, dry_run=True)
    conn.commit()
    return report


def scan(conn, folders: list[dict] | None = None, by: str = "cli") -> dict:
    """List the NAS roots (read only), record the folders, create entries for internal folders, propose and
    confirm matches, recompute every entry's availability. Cheap: directory listings only."""
    folders = list_folders() if folders is None else folders
    with conn.cursor() as cur:
        cur.execute("INSERT INTO catalog_imports (kind, run_by) VALUES ('scan', %s)", (by,))
        import_id = cur.lastrowid
        by_path = upsert_folders(cur, folders)
        internal = [f for f in folders if f.get("path") and f.get("origin") == "internal" and f["role"] == "source"]
        fres = _upsert_entries(cur, [folder_entry(f) for f in internal], import_id, "folder")
        entries = _q(cur, "SELECT * FROM catalog_entries WHERE gone_at IS NULL")
        decided = {(m["entry_id"], m["folder_id"]): m for m in _q(cur, "SELECT * FROM catalog_matches")}
        # internal folder entries own their folder
        for e in entries:
            if e["kind"] == "folder":
                f = by_path.get(json.loads(e["fields"])["폴더"])
                if f and (e["entry_id"], f["folder_id"]) not in decided:
                    cur.execute("INSERT INTO catalog_matches (entry_id, folder_id, method, state, score)"
                                " VALUES (%s,%s,'internal','confirmed',1)", (e["entry_id"], f["folder_id"]))
        listed = [e for e in entries if e["kind"] == "list"]
        public_folders = [f for f in folders if f.get("path") and f.get("origin") == "public"]
        props = propose_matches([{"key": e["entry_key"], "name": e["name"]} for e in listed], public_folders)
        for e in listed:
            _, lpath = list_availability(e["list_download"])
            cands = list(props.get(e["entry_key"], []))
            if lpath and lpath in by_path:
                cands.insert(0, (lpath, "list_path", 1.0))
            for path, method, score in cands:
                f = by_path[path]
                if (e["entry_id"], f["folder_id"]) in decided:
                    continue
                state = "confirmed" if method in ("exact", "list_path") else "proposed"
                cur.execute("INSERT INTO catalog_matches (entry_id, folder_id, method, state, score)"
                            " VALUES (%s,%s,%s,%s,%s)", (e["entry_id"], f["folder_id"], method, state, score))
                decided[(e["entry_id"], f["folder_id"])] = {"state": state}
        summary = recompute(cur)
        report = {"roots": sorted({f["root"] for f in folders}), "folders": sum(1 for f in folders if f.get("path")),
                  "root_errors": [f for f in folders if not f.get("path")],
                  "internal_entries_added": fres["added"], "internal_entries_gone": fres["gone"], **summary}
        cur.execute("UPDATE catalog_imports SET rows_read=%s, added=%s, gone=%s, report=%s WHERE import_id=%s",
                    (report["folders"], len(fres["added"]), len(fres["gone"]),
                     json.dumps(report, ensure_ascii=False, default=str), import_id))
    conn.commit()
    return report


def recompute(cur) -> dict:
    """Availability of every entry from its list value and its confirmed folders."""
    folders = {r["folder_id"]: r for r in _q(cur, "SELECT * FROM catalog_folders")}
    matches = defaultdict(list)
    for m in _q(cur, "SELECT * FROM catalog_matches WHERE state<>'rejected'"):
        matches[m["entry_id"]].append(m)
    counts = Counter()
    for e in _q(cur, "SELECT entry_id, kind, list_download, list_availability, availability, availability_note"
                     " FROM catalog_entries WHERE gone_at IS NULL"):
        ms = matches.get(e["entry_id"], [])
        conf = [folders[m["folder_id"]] for m in ms if m["state"] == "confirmed"]
        prop = [folders[m["folder_id"]] for m in ms if m["state"] == "proposed" and folders[m["folder_id"]]["present"]]
        src = [f for f in conf if f["role"] == "source"] or conf
        _, lpath = list_availability(e["list_download"])
        if lpath and any(f["path"] == lpath for f in conf):
            lpath = None
        if e["kind"] == "folder":                       # an internal folder is its own record: no list to differ
            have = [f for f in src if f["present"] and (f["top_entries"] or 0) > 0]
            state = "downloaded" if have else "unknown"
            note = ("내부 폴더: " + ", ".join(f["path"] for f in have)) if have else "내부 폴더가 비었거나 없음"
        else:
            state, note = decide_availability(e["list_availability"], lpath, src, prop)
        counts[state] += 1
        if (state, note[:1024]) != (e["availability"], e["availability_note"]):
            cur.execute("UPDATE catalog_entries SET availability=%s, availability_note=%s WHERE entry_id=%s",
                        (state, note[:1024], e["entry_id"]))
    return {"availability": dict(counts)}


# --------------------------------------------------------------------------- the size walk (off-hours, throttled)
def walk_size(path: str, max_rate: float = 1000.0, deadline: float | None = None) -> dict:
    """Files and bytes under `path`, reading directory entries and their metadata only, at most `max_rate`
    entries per second. Symlinks are not followed. Stops early (complete=False) at `deadline`."""
    files = size = seen = 0
    t0 = time.monotonic()
    stack = [path]
    complete = True
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for ent in it:
                seen += 1
                if max_rate and seen % 100 == 0:
                    ahead = seen / max_rate - (time.monotonic() - t0)
                    if ahead > 0:
                        time.sleep(ahead)
                try:
                    if ent.is_dir(follow_symlinks=False):
                        stack.append(ent.path)
                    elif ent.is_file(follow_symlinks=False):
                        files += 1
                        size += ent.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        if deadline and time.monotonic() > deadline:
            complete = not stack
            break
    return {"file_count": files, "size_bytes": size, "complete": complete,
            "seconds": int(time.monotonic() - t0)}


def size_walk(conn, budget_s: int = 3 * 3600, max_rate: float = 1000.0, max_age_days: int = 30,
              only_matched: bool = True, by: str = "cli") -> dict:
    """Walk the folders whose size is missing or older than `max_age_days`, oldest first, until `budget_s` is
    spent. Each result is stored as soon as it is known (the cache the pages read)."""
    end = time.monotonic() + budget_s
    with conn.cursor() as cur:
        rows = _q(cur, f"""SELECT f.* FROM catalog_folders f WHERE f.present=1
                  {"AND EXISTS (SELECT 1 FROM catalog_matches m WHERE m.folder_id=f.folder_id AND m.state<>'rejected')"
                   if only_matched else ""}
                  AND (f.size_scanned_at IS NULL OR f.size_complete=0
                       OR f.size_scanned_at < NOW() - INTERVAL %s DAY)
                  ORDER BY f.size_scanned_at IS NOT NULL, f.size_scanned_at, f.top_entries""", (max_age_days,))
    done = []
    for f in rows:
        if time.monotonic() > end:
            break
        r = walk_size(f["path"], max_rate=max_rate, deadline=end)
        with conn.cursor() as cur:
            cur.execute("UPDATE catalog_folders SET file_count=%s, size_bytes=%s, size_complete=%s,"
                        " size_scanned_at=CURRENT_TIMESTAMP, size_seconds=%s WHERE folder_id=%s",
                        (r["file_count"], r["size_bytes"], int(r["complete"]), r["seconds"], f["folder_id"]))
        conn.commit()
        done.append({"path": f["path"], **r})
    with conn.cursor() as cur:
        cur.execute("INSERT INTO catalog_imports (kind, rows_read, run_by, report) VALUES ('sizes', %s, %s, %s)",
                    (len(done), by, json.dumps({"walked": done, "left": len(rows) - len(done)}, ensure_ascii=False)))
    conn.commit()
    return {"walked": len(done), "left": len(rows) - len(done),
            "files": sum(d["file_count"] for d in done), "bytes": sum(d["size_bytes"] for d in done)}


# --------------------------------------------------------------------------- hand edits
EDITABLE = {"origin": ORIGINS, "licence": None, "commercial_use": COMMERCIAL, "availability": AVAILABILITY}


def edit_entry(conn, entry_id: int, changes: dict, note: str, by: str) -> list[str]:
    """Set (or clear, with "") hand-edit overrides of an entry, with a note; every change is logged."""
    if not note.strip():
        raise CatalogueError("수정 사유(메모)를 적어 주세요")
    if not by.strip():
        raise CatalogueError("누가 고쳤는지 적어 주세요")
    done = []
    with conn.cursor() as cur:
        rows = _q(cur, "SELECT * FROM catalog_entries WHERE entry_id=%s FOR UPDATE", (entry_id,))
        if not rows:
            raise CatalogueError("항목이 없습니다")
        e = rows[0]
        for field, value in changes.items():
            if field not in EDITABLE:
                raise CatalogueError(f"고칠 수 없는 항목: {field}")
            value = (value or "").strip() or None
            allowed = EDITABLE[field]
            if value is not None and allowed and value not in allowed:
                raise CatalogueError(f"{field} 값은 {', '.join(allowed)} 중 하나")
            if value is not None and len(value) > 64:
                raise CatalogueError(f"{field} 값이 너무 깁니다")
            if value == e[f"{field}_override"]:
                continue
            cur.execute(f"UPDATE catalog_entries SET {field}_override=%s, override_note=%s, override_by=%s,"
                        f" override_at=CURRENT_TIMESTAMP WHERE entry_id=%s", (value, note[:4000], by[:128], entry_id))
            cur.execute("INSERT INTO catalog_edits (entry_id, field, old_value, new_value, note, edited_by)"
                        " VALUES (%s,%s,%s,%s,%s,%s)",
                        (entry_id, field, e[f"{field}_override"] or e[field], value or f"(목록 값 {e[field]})",
                         note[:4000], by[:128]))
            done.append(field)
    conn.commit()
    return done


def decide_match(conn, entry_id: int, folder_id: int, state: str, by: str) -> None:
    """A person confirms or rejects a proposed folder (or links one by hand); availability follows."""
    if state not in ("confirmed", "rejected", "proposed"):
        raise CatalogueError("confirmed / rejected 중 하나")
    with conn.cursor() as cur:
        if not _q(cur, "SELECT 1 AS x FROM catalog_entries WHERE entry_id=%s", (entry_id,)) or \
                not _q(cur, "SELECT 1 AS x FROM catalog_folders WHERE folder_id=%s", (folder_id,)):
            raise CatalogueError("항목이나 폴더가 없습니다")
        cur.execute("""INSERT INTO catalog_matches (entry_id, folder_id, method, state, decided_by, decided_at)
                       VALUES (%s,%s,'person',%s,%s,CURRENT_TIMESTAMP)
                       ON DUPLICATE KEY UPDATE state=VALUES(state), decided_by=VALUES(decided_by),
                                               decided_at=CURRENT_TIMESTAMP""",
                    (entry_id, folder_id, state, by[:128]))
        recompute(cur)
    conn.commit()


# --------------------------------------------------------------------------- reading for the web / exports
def load(conn) -> tuple[list[dict], dict]:
    """Every current entry with its effective values, tags and folders; and the unmatched folders."""
    with conn.cursor() as cur:
        entries = _q(cur, "SELECT * FROM catalog_entries WHERE gone_at IS NULL ORDER BY kind=%s, list_row, name", ("folder",))
        folders = {r["folder_id"]: r for r in _q(cur, "SELECT * FROM catalog_folders")}
        matches = defaultdict(list)
        used = set()
        for m in _q(cur, "SELECT * FROM catalog_matches ORDER BY state, score DESC"):
            matches[m["entry_id"]].append(dict(m, folder=folders[m["folder_id"]]))
            if m["state"] != "rejected":
                used.add(m["folder_id"])
    for e in entries:
        e["tags"] = json.loads(e["tags"] or "{}")
        e["fields"] = json.loads(e["fields"] or "{}")
        e["eff"] = {f: effective(e, f) for f in EDITABLE}
        e["folders"] = matches.get(e["entry_id"], [])
        e["edited"] = [f for f in EDITABLE if e.get(f"{f}_override")]
        conf = [m["folder"] for m in e["folders"] if m["state"] == "confirmed"]
        e["size_bytes"] = sum(f["size_bytes"] or 0 for f in conf) if conf and all(
            f["size_bytes"] is not None for f in conf) else None
        e["file_count"] = sum(f["file_count"] or 0 for f in conf) if e["size_bytes"] is not None else None
        e["n_proposed"] = sum(1 for m in e["folders"] if m["state"] == "proposed")
    unmatched = [f for fid, f in folders.items() if fid not in used and f["present"]]
    return entries, {"unmatched": sorted(unmatched, key=lambda f: (f["root"], f["name"])),
                     "all_folders": list(folders.values())}


FACETS = {  # param -> (label, value of an entry: list of strings)
    "commercial": ("상업적 이용", lambda e: [e["eff"]["commercial_use"]]),
    "licence": ("라이선스", lambda e: [e["eff"]["licence"] or "none stated"]),
    "origin": ("출처 구분", lambda e: [e["eff"]["origin"]]),
    "availability": ("보유 상태", lambda e: [e["eff"]["availability"]]),
    "modality": ("모달리티", lambda e: e["tags"].get("modality", [])),
    "roi": ("ROI 범주", lambda e: e["tags"].get("roi_category", [])),
    "body": ("촬영 부위", lambda e: e["tags"].get("body", [])),
    "country": ("수집 국가", lambda e: e["tags"].get("country", [])),
    "verification": ("검증 상태", lambda e: [e["tags"].get("verification", "없음")]),
    "new_rois": ("신규 ROI", lambda e: [e["tags"].get("new_rois", "no")]),
    "diff": ("목록과 스캔", lambda e: ["차이" if (e["availability_note"] or "").startswith("차이:") else "일치"]),
    "proposed": ("폴더 후보", lambda e: ["확인 필요" if e["n_proposed"] else "없음"]),
    "kind": ("행 종류", lambda e: ["marker" if e["is_marker"] else e["kind"]]),
}


def search_text(e: dict) -> str:
    f = e["fields"]
    return " ".join(str(x) for x in (e["name"], f.get(C_ALL_ROI), f.get(C_NEW_ROI), f.get(C_ROI_CAT), e["link"],
                                     *(m["folder"]["path"] for m in e["folders"]))).lower()


def apply_filters(entries: list[dict], params: dict[str, list[str]], q: str = "") -> tuple[list[dict], dict]:
    """(filtered entries, {facet: Counter}). Values inside one facet are OR; facets are AND. Each facet's counts
    are taken over the entries the *other* facets and the text search leave, so a count says what ticking that
    value would add."""
    words = [w for w in q.lower().split() if w]

    def keep(e, skip=None):
        if words and not all(w in e["_text"] for w in words):
            return False
        for k, vals in params.items():
            if k == skip or not vals or k not in FACETS:
                continue
            if not set(FACETS[k][1](e)) & set(vals):
                return False
        return True

    for e in entries:
        e.setdefault("_text", search_text(e))
    counts = {}
    for k, (_, fn) in FACETS.items():
        c = Counter()
        for e in entries:
            if keep(e, skip=k):
                c.update(set(fn(e)))
        counts[k] = c
    return [e for e in entries if keep(e)], counts


EXPORT_FIXED = [("entry_id", "항목 ID"), ("kind", "행 종류"), ("origin", "출처 구분"), ("licence", "라이선스(정규화)"),
                ("commercial_use", "상업적 이용"), ("availability", "보유 상태"), ("availability_note", "보유 상태 근거"),
                ("folders", "폴더"), ("file_count", "파일 수"), ("size_bytes", "용량(byte)"),
                ("override_note", "수정 메모")]


def export_rows(entries: list[dict], headers: list[str]) -> list[list]:
    """Header row + one row per entry: the catalogue's own columns, then every imported column."""
    out = [[label for _, label in EXPORT_FIXED] + headers]
    for e in entries:
        fixed = {"entry_id": e["entry_id"], "kind": "marker" if e["is_marker"] else e["kind"],
                 "folders": " | ".join(m["folder"]["path"] for m in e["folders"] if m["state"] == "confirmed"),
                 **{f: e["eff"][f] for f in EDITABLE}}
        out.append([fixed.get(k, e.get(k)) for k, _ in EXPORT_FIXED] + [e["fields"].get(h) for h in headers])
    return out


def last_headers(conn) -> list[str]:
    with conn.cursor() as cur:
        r = _q(cur, "SELECT report FROM catalog_imports WHERE kind='list' ORDER BY import_id DESC LIMIT 1")
    if not r:
        return []
    return json.loads(r[0]["report"] or "{}").get("headers", [])


def summary(conn) -> dict:
    """What the MILESTONE / the page header reports: counts, where list and scan disagree, what stays unclassified."""
    entries, extra = load(conn)
    by = lambda f: dict(Counter(e["eff"][f] for e in entries))          # noqa: E731
    lst = [e for e in entries if e["kind"] == "list"]
    disagree = [{"entry_id": e["entry_id"], "name": e["name"], "list": e["list_download"],
                 "availability": e["eff"]["availability"], "note": e["availability_note"]}
                for e in entries if (e["availability_note"] or "").startswith("차이:")]
    return {"entries": len(entries), "list_rows": len(lst), "markers": sum(e["is_marker"] for e in lst),
            "internal_folders": sum(1 for e in entries if e["kind"] == "folder"),
            "availability": by("availability"), "commercial_use": by("commercial_use"), "origin": by("origin"),
            "commercial_by_origin": {o: dict(Counter(e["eff"]["commercial_use"] for e in entries
                                                     if e["eff"]["origin"] == o)) for o in ORIGINS},
            "disagree": disagree,
            "commercial_unknown": [{"entry_id": e["entry_id"], "name": e["name"], "licence": e["eff"]["licence"],
                                    "why": e["commercial_reason"]}
                                   for e in entries if e["eff"]["commercial_use"] == "unknown"],
            "availability_unknown": [e["name"] for e in entries if e["eff"]["availability"] == "unknown"],
            "proposed_folders": sum(e["n_proposed"] for e in entries),
            "unmatched_folders": [f["path"] for f in extra["unmatched"]],
            "sized_folders": sum(1 for f in extra["all_folders"] if f["size_scanned_at"]),
            "folders": len(extra["all_folders"])}
