"""Deterministic half of the agent analysis: check the agent's proposal and turn it into a plan.

Every proposal field is re-checked here (paths inside the input root, regexes, roles, pairing, label
values, prefix); the agent cannot cause anything the checks would not allow from a hand-written spec.
Only NIfTI headers are read (no voxels, no hashes): source files are hashed when they are frozen, and
derived UIDs use the file's path inside the dataset as its identity (`conversion.identity`).
"""
from __future__ import annotations

import fnmatch
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

import nibabel as nib
import numpy as np

from .agent import LABEL_KINDS, PAIRINGS, ROLES, SPLITS
from .inventory import SKIP_DIR_NAMES, _dicom_record, read_dicom_header
from . import nfs
from .legacy import n2d
from .plan import PREFIX_RE, dicom_series, validate_plan
from .rtstruct import RTSTRUCT_VERSION
from .rules import RULES_VERSION
from .scan import file_kind, list_files
from .util import now_iso, ppmap

ADAPTER = "agent-spec"
ADAPTER_VERSION = "1"
MAX_REGEX = 200
GEOMETRY_TOL_MM = 1e-3
SAMPLES = 3                # sample subjects: extracted at analysis, shown, sent to the local SCP


class SpecError(ValueError):
    pass


def posix_parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else "."


def _clean_rel(root: Path, rel: str) -> str:
    rel = (rel or "").strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.rstrip("/") or "."
    pp = PurePosixPath(rel)
    if pp.is_absolute() or ".." in pp.parts:
        raise SpecError(f"경로 {rel!r} 는 입력 폴더 안의 상대 경로여야 합니다")
    target = (root / rel).resolve() if rel != "." else root
    if not target.is_relative_to(root):
        raise SpecError(f"경로 {rel!r} 가 입력 폴더 밖을 가리킵니다")
    if not nfs.exists(target):
        raise SpecError(f"경로 {rel!r} 가 없습니다")
    return rel


def _regex(pattern: str, what: str) -> re.Pattern:
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_REGEX:
        raise SpecError(f"{what}: 정규식은 1..{MAX_REGEX}자여야 합니다")
    try:
        return re.compile(pattern)
    except re.error as e:
        raise SpecError(f"{what}: 정규식 오류 {e}") from None


def check_proposal(root: Path, p: dict) -> dict:
    """Return a normalised copy of the proposal or raise SpecError with the first problem."""
    if not isinstance(p, dict):
        raise SpecError("제안이 JSON 객체가 아닙니다")
    ds = dict(p.get("dataset") or {})
    prefix = str(ds.get("patient_id_prefix") or "").strip().upper()
    if not PREFIX_RE.fullmatch(prefix or "-"):
        raise SpecError(f"patient_id_prefix {prefix!r} 형식 오류 ([A-Za-z0-9_-], 1..32자)")
    if ds.get("commercial_use") not in ("Y", "N", "U"):
        ds["commercial_use"] = "U"
    ds["patient_id_prefix"] = prefix
    sources = []
    for s in p.get("sources") or []:
        if s.get("role") not in ROLES or s.get("split") not in SPLITS:
            raise SpecError(f"source {s.get('path')!r}: role/split 값 오류")
        pattern = str(s.get("pattern") or "").strip()
        if len(pattern) > MAX_REGEX or "/" in pattern:
            raise SpecError(f"source {s.get('path')!r}: pattern은 파일 이름 glob (경로 구분자 없이, {MAX_REGEX}자 이하)")
        sources.append({"path": _clean_rel(root, s["path"]), "role": s["role"], "split": s["split"],
                        "pattern": pattern, "note": str(s.get("note") or "")[:500]})
    seen = set()
    for s in sources:
        if (s["path"], s["pattern"]) in seen:
            raise SpecError(f"source {s['path']!r} 가 같은 pattern {s['pattern']!r} 으로 두 번 나옵니다")
        seen.add((s["path"], s["pattern"]))
    excluded = []
    for e in p.get("excluded") or []:
        try:
            excluded.append({"path": _clean_rel(root, e["path"]), "reason": str(e.get("reason") or "")[:500]})
        except SpecError:
            excluded.append({"path": str(e.get("path"))[:500], "reason": str(e.get("reason") or "")[:500],
                             "missing": True})
    subj = p.get("subject_pattern") or {}
    rx = _regex(subj.get("regex"), "subject_pattern")
    if rx.groups < 1:
        raise SpecError("subject_pattern 정규식에는 대상자 키를 잡는 그룹 1이 있어야 합니다")
    rules = []
    for r in p.get("modality_rules") or []:
        if r.get("modality") not in ("CT", "MR"):
            raise SpecError("modality_rules: modality는 CT 또는 MR")
        _regex(r.get("regex"), "modality_rules")
        rules.append({"regex": r["regex"], "modality": r["modality"], "basis": str(r.get("basis") or "")[:300]})
    names = {}
    for item in p.get("label_names") or []:
        v = int(item["value"])
        if not 1 <= v <= 65535:
            raise SpecError(f"label 값 {v} 범위 오류")
        names[v] = str(item["name"]).strip()[:64] or f"label_{v}"
    if p.get("label_pairing") not in PAIRINGS:
        raise SpecError(f"label_pairing 은 {', '.join(PAIRINGS)} 중 하나")
    kind = p.get("label_kind") or "label_map"
    if kind not in LABEL_KINDS:
        raise SpecError(f"label_kind 는 {', '.join(LABEL_KINDS)} 중 하나")
    roi_rx = str(p.get("roi_name_regex") or "")
    if roi_rx and _regex(roi_rx, "roi_name_regex").groups < 1:
        raise SpecError("roi_name_regex 에는 ROI 이름을 잡는 그룹 1이 있어야 합니다")
    layout = p.get("nifti_layout") or "standard"
    if layout not in ("standard", "onco"):
        raise SpecError("nifti_layout 은 standard 또는 onco")
    return {"summary_ko": str(p.get("summary_ko") or "")[:4000], "dataset": ds, "sources": sources,
            "excluded": excluded, "subject_pattern": {"regex": subj["regex"], "note": str(subj.get("note") or "")},
            "modality_rules": rules, "label_pairing": p["label_pairing"], "label_kind": kind,
            "roi_name_regex": roi_rx, "label_names": names,
            "pilot_subjects": [str(x) for x in (p.get("pilot_subjects") or [])][:4], "nifti_layout": layout,
            "rules_version": str(p.get("rules_version") or ""),
            "findings": ([] if p.get("rules_version") == RULES_VERSION else       # the skill was not read
                         [f"⚠ 에이전트가 규칙(dataset-intake skill, {RULES_VERSION})을 읽었다는 표시가 없습니다 "
                          f"(rules_version={str(p.get('rules_version') or '')[:40]!r}) — 접두어·구성 판단을 확인하세요"])
            + [str(x)[:600] for x in p.get("findings") or []][:30],
            "questions": [str(x)[:600] for x in p.get("questions") or []][:10]}


def header_geometry(path: Path, layout: str) -> dict:
    """Canonical (as written to DICOM) shape and LPS affine from the header only."""
    img = nfs.call(lambda p: nib.load(str(p)), path)
    shape = list(img.shape)
    while len(shape) > 3 and shape[-1] == 1:
        shape.pop()
    if len(shape) > 3:
        raise SpecError(f"{len(shape)}D {tuple(img.shape)} 미지원 (3D만)")
    if len(shape) == 2:
        shape.append(1)
    dummy = np.broadcast_to(np.zeros((), dtype=np.uint8), tuple(shape))
    vol, reason = n2d._volume(img, dummy, 1.0, 0.0, layout)
    if vol is None:
        raise SpecError(reason)
    data, affine, ops = n2d.canonicalize(vol.data, vol.affine_lps)
    zooms = np.linalg.norm(affine[:3, :3], axis=0)
    return {"shape": [int(x) for x in data.shape], "affine": affine.round(6).tolist(),
            "spacing_mm": [round(float(z), 4) for z in zooms], "axcodes": "".join(nib.aff2axcodes(img.affine)),
            "dtype": str(img.header.get_data_dtype()), "index_ops": ops, "notes": vol.notes}


def _excluded_by(rel: str, excluded: list[dict]) -> tuple[str, str] | None:
    """(excluded path, reason) of the most specific exclusion covering rel."""
    best = None
    for e in excluded:
        p = e["path"]
        if (p == "." or rel == p or rel.startswith(p + "/")) and (best is None or len(p) > len(best[0])):
            best = (p, e["reason"] or "에이전트가 제외")
    return best


def _source_of(rel: str, sources: list[dict]) -> dict | None:
    """The most specific source covering rel whose file-name pattern (if any) matches; at the same path a source
    with a pattern wins over one without."""
    best, name = None, rel.rsplit("/", 1)[-1]
    for s in sources:
        p = s["path"]
        if (p == "." or rel == p or rel.startswith(p + "/")) and (not s["pattern"] or fnmatch.fnmatchcase(name, s["pattern"])):
            rank = (len(p), bool(s["pattern"]))
            if best is None or rank > (len(best["path"]), bool(best["pattern"])):
                best = s
    return best


def roi_name(spec: dict, name: str) -> str:
    """ROI name of a binary mask file: group 1 of roi_name_regex, or the file name without its extension."""
    m = re.search(spec["roi_name_regex"], name) if spec["roi_name_regex"] else None
    stem = re.sub(r"\.nii(\.gz)?$", "", name)
    return ((m.group(1) if m else stem).strip() or stem)[:64]


def _walked(root: Path, rel: str) -> bool:
    """Whether a walk of the root reaches rel (no hidden, backup or symlinked directory on the way)."""
    parts = [] if rel in ("", ".") else rel.split("/")
    return not any(part.startswith(".") or part.lower() in SKIP_DIR_NAMES or (root / "/".join(parts[:i + 1])).is_symlink()
                   for i, part in enumerate(parts))


def _probe(item: tuple) -> dict:
    """stat, kind and header of one input file, for build_spec_plan. Runs in worker processes (ppmap), so it
    is module-level and returns errors as text."""
    root, rel, role, layout, size = item
    path = root / rel
    out = {"kind": file_kind(path), "size": size}
    if size is None:                          # not stat'ed by the walk (a symlink)
        try:
            out["size"] = nfs.stat(path).st_size
        except OSError as e:
            return {**out, "error": str(e)}
    if role == "ignore":
        return out
    if out["kind"] == "nifti":
        try:
            out["geo"] = header_geometry(path, layout)
        except Exception as e:  # noqa: BLE001 - any reader failure is reported per file
            out["geo_error"] = str(e)
    elif out["kind"] == "dicom?" and role == "image":
        ds = read_dicom_header(path)
        out["dicom"] = _dicom_record(ds) if ds is not None else None
    return out


def build_spec_plan(root: Path, proposal: dict, run_id: str, dataset_uuid: str, agent_meta: dict | None = None,
                    log=print) -> tuple[dict, dict]:
    """(plan, inventory) from a checked proposal. Raises SpecError for an unusable proposal."""
    root = root.resolve()
    spec = check_proposal(root, proposal)
    subj_rx = re.compile(spec["subject_pattern"]["regex"])
    rules = [(re.compile(r["regex"]), r) for r in spec["modality_rules"]]
    layout = spec["nifti_layout"]

    sizes = dict(list_files(root, ".", sizes=True))     # one walk of the whole root, reused below
    included, inv_files = {}, []
    for s in spec["sources"]:
        p = s["path"]
        rels = [r for r in sizes if p == "." or r == p or r.startswith(p + "/")] if _walked(root, p) \
            else list_files(root, p)
        for rel in rels:
            src = _source_of(rel, spec["sources"])
            if src is s:
                included[rel] = s
    skipped, unresolved, series = [], [], []
    images, labels = [], []
    log(f"spec: {len(included)} files under {len(spec['sources'])} sources")

    items = sorted(included.items())
    probes = ppmap(_probe, [(root, rel, src["role"], layout, sizes.get(rel)) for rel, src in items])
    for (rel, src), pr in zip(items, probes):
        path = root / rel
        kind = pr["kind"]
        rec = {"rel": rel, "size": None, "kind": "other", "source": src["path"]}
        if "error" in pr:
            rec.update(kind="unreadable", reason=pr["error"][:200])
            inv_files.append(rec)
            skipped.append({"rel": rel, "kind": "unreadable", "reason": pr["error"][:200]})
            continue
        rec["size"] = pr["size"]
        if src["role"] == "ignore":
            rec["kind"] = "ignored"
            inv_files.append(rec)
            skipped.append({"rel": rel, "kind": "ignored", "reason": f"source 역할 ignore: {src['note']}"})
            continue
        if kind == "nifti":
            rec["kind"] = "nifti"
            if "geo_error" in pr:
                rec.update(kind="unreadable", reason=pr["geo_error"][:200])
                skipped.append({"rel": rel, "kind": "unsupported", "reason": f"NIfTI 헤더: {pr['geo_error'][:200]}"})
                inv_files.append(rec)
                continue
            geo = pr["geo"]
            m = subj_rx.search(path.name)
            entry = {"rel": rel, "name": path.name, "split": src["split"], "geo": geo,
                     "subject": m.group(1) if m else None}
            (images if src["role"] == "image" else labels).append(entry)
        elif kind in ("dicom?",) and src["role"] == "image":
            if pr["dicom"] is not None:
                rec.update(kind="dicom", dicom=pr["dicom"])
            else:
                skipped.append({"rel": rel, "kind": "other", "reason": "DICOM이 아닌 파일"})
        else:
            skipped.append({"rel": rel, "kind": kind, "reason": f"{src['role']} source 안의 영상이 아닌 파일"})
        inv_files.append(rec)

    # ---- images: subject + modality from the rules ----
    by_subject = defaultdict(list)
    for im in images:
        if not im["subject"]:
            skipped.append({"rel": im["rel"], "kind": "nifti",
                            "reason": f"파일명이 subject 규칙 {spec['subject_pattern']['regex']!r} 과 맞지 않음"})
            continue
        rule = next((r for rx, r in rules if rx.search(im["rel"])), None)
        im["modality"], im["basis"] = (rule["modality"], f"에이전트 규칙 /{rule['regex']}/: {rule['basis']}") if rule \
            else (None, "어떤 modality 규칙과도 맞지 않음")
        by_subject[im["subject"]].append(im)
    image_series = {}
    for subject, items in sorted(by_subject.items()):
        names = [i["name"] for i in items]
        for number, im in enumerate(sorted(items, key=lambda i: i["rel"]), start=1):
            key = f"nifti:{im['rel']}"
            s = {"series_key": key, "kind": "nifti", "excluded": False, "split": im["split"],
                 "subject_key": subject, "patient_id": None, "study_uid": None, "series_uid": None,
                 "modality": im["modality"], "modality_basis": im["basis"], "needs_review": False,
                 "description": None, "series_number": number, "instance_count": im["geo"]["shape"][2],
                 "sop_class_uids": [], "sources": [im["rel"]], "duplicates": [], "conflicts": [],
                 "metadata": {"split": im["split"], "shape_xyz": im["geo"]["shape"],
                              "spacing_mm": im["geo"]["spacing_mm"], "axcodes": im["geo"]["axcodes"],
                              "dtype": im["geo"]["dtype"], "notes": im["geo"]["notes"]},
                 "conversion": {"converter": n2d.CONVERTER_VERSION, "layout": layout, "group_label": subject,
                                "source_sha256": None, "identity": f"path:{im['rel']}", "deterministic": True}}
            if len(set(names)) != len(names):
                s["conflicts"].append({"reason": f"대상자 {subject} 에 같은 파일명의 영상이 여러 개"})
            series.append(s)
            image_series[key] = (s, im)
            if im["modality"] is None:
                unresolved.append({"code": "modality_ambiguous", "blocking": True, "series_keys": [key],
                                   "message": f"{im['rel']}: modality 규칙 없음 — CT/MR 지정 필요"})

    # ---- labels: pair with an image, same grid ----
    by_name = defaultdict(list)
    for key, (s, im) in image_series.items():
        by_name[im["name"]].append(key)
    by_subj, by_dir = defaultdict(list), defaultdict(list)
    for key, (s, im) in image_series.items():
        by_subj[s["subject_key"]].append(key)
        by_dir[posix_parent(im["rel"])].append(key)
    masks = defaultdict(list)            # binary_masks: image series key -> its mask files
    for lb in labels:
        if spec["label_pairing"] == "same_name":
            cands = by_name.get(lb["name"], [])
        elif spec["label_pairing"] == "same_folder":
            cands = by_dir.get(posix_parent(lb["rel"]), [])
        else:
            cands = by_subj.get(lb["subject"], []) if lb["subject"] else []
        if len(cands) != 1:
            skipped.append({"rel": lb["rel"], "kind": "label",
                            "reason": "짝 영상 없음" if not cands else f"짝 영상 후보 {len(cands)}개 (모호)"})
            continue
        img_s, img = image_series[cands[0]]
        if lb["geo"]["shape"] != img["geo"]["shape"] or not np.allclose(
                np.asarray(lb["geo"]["affine"]), np.asarray(img["geo"]["affine"]), atol=GEOMETRY_TOL_MM):
            skipped.append({"rel": lb["rel"], "kind": "label",
                            "reason": f"영상 {img['rel']} 과 격자(크기/위치/방향)가 달라 RTSTRUCT로 만들 수 없음"})
            continue
        if spec["label_kind"] == "binary_masks":
            masks[cands[0]].append(lb)
            continue
        series.append({"series_key": f"rtstruct:{lb['rel']}", "kind": "rtstruct", "excluded": False,
                       "split": img_s["split"], "reference": img_s["series_key"],
                       "subject_key": img_s["subject_key"], "patient_id": None, "study_uid": None, "series_uid": None,
                       "modality": "RTSTRUCT", "modality_basis": "라벨 맵 → RTSTRUCT", "needs_review": False,
                       "description": None, "series_number": None, "instance_count": 1, "sop_class_uids": [],
                       "sources": [lb["rel"]], "duplicates": [], "conflicts": [],
                       "metadata": {"split": img_s["split"], "label_of": img["rel"]},
                       "conversion": {"converter": RTSTRUCT_VERSION, "layout": layout,
                                      "label_names": {str(k): v for k, v in spec["label_names"].items()},
                                      "source_sha256": None, "identity": f"path:{lb['rel']}", "deterministic": True}})

    for key, lbs in sorted(masks.items()):       # one RTSTRUCT per image, one ROI per mask file
        img_s, img = image_series[key]
        lbs = sorted(lbs, key=lambda x: x["rel"])
        names, used = {}, set()
        for i, lb in enumerate(lbs, start=1):
            base = n = roi_name(spec, lb["name"])
            k = 2
            while n in used:
                n, k = f"{base}_{k}"[:64], k + 1
            used.add(n)
            names[str(i)] = n
        series.append({"series_key": f"rtstruct:masks:{img['rel']}", "kind": "rtstruct", "excluded": False,
                       "split": img_s["split"], "reference": img_s["series_key"],
                       "subject_key": img_s["subject_key"], "patient_id": None, "study_uid": None, "series_uid": None,
                       "modality": "RTSTRUCT", "modality_basis": f"이진 마스크 {len(lbs)}개 → RTSTRUCT",
                       "needs_review": False, "description": None, "series_number": None, "instance_count": 1,
                       "sop_class_uids": [], "sources": [lb["rel"] for lb in lbs], "duplicates": [], "conflicts": [],
                       "metadata": {"split": img_s["split"], "label_of": img["rel"], "mask_files": len(lbs)},
                       "conversion": {"converter": RTSTRUCT_VERSION, "layout": layout, "masks": True,
                                      "label_names": names, "source_sha256": None,
                                      "identity": f"masks:{img['rel']}", "deterministic": True}})

    # ---- original DICOM: passed through (pseudonymised when switched on), one series per Series UID ----
    _, _, dicom = dicom_series(inv_files, root)
    for s in dicom:
        s["split"] = included[s["sources"][0]]["split"]
        s["metadata"]["split"] = s["split"]
    series += dicom

    # ---- everything else in the folder ----
    not_included = defaultdict(lambda: {"files": 0, "bytes": 0, "reason": None})
    for rel, size in sizes.items():
        if rel in included:
            continue
        hit = _excluded_by(rel, spec["excluded"])
        under = [x for x in spec["sources"] if x["pattern"] and (x["path"] == "." or rel.startswith(x["path"] + "/"))]
        top, reason = hit if hit else (rel.split("/")[0] if "/" in rel else rel,
                                       "source 파일 패턴과 맞지 않음" if under else "어느 source에도 속하지 않음")
        g = not_included[(top, reason)]
        g["files"] += 1
        try:
            g["bytes"] += size if size is not None else nfs.stat(root / rel).st_size
        except OSError:
            pass
    left_out = [{"path": top, "reason": reason, **{k: v for k, v in g.items() if k != "reason"}}
                for (top, reason), g in sorted(not_included.items())]

    if not any(x["kind"] in ("nifti", "dicom") for x in series):
        unresolved.append({"code": "no_image", "blocking": True, "series_keys": [],
                           "message": "변환할 영상이 없습니다 — 에이전트 질문과 제외 사유를 확인하세요"
                                      + ("" if any(x["role"] == "image" for x in spec["sources"])
                                         else " (image 역할의 source 없음)")})
    image_kinds = ("nifti", "dicom")
    subjects = sorted({s["subject_key"] for s in series if s["kind"] in image_kinds})
    with_label = {s["subject_key"] for s in series if s["kind"] == "rtstruct" or s["modality"] in ("RTSTRUCT", "SEG")}
    pilot = [x for x in spec["pilot_subjects"] if x in subjects][:SAMPLES]
    for labelled in (True, False):          # subjects with labels first; images only when none has a label
        for mod in ("CT", "MR", None):
            for s in series:
                if s["kind"] in image_kinds and s["subject_key"] not in pilot and len(pilot) < SAMPLES \
                        and (s["subject_key"] in with_label or not labelled) and (mod is None or s["modality"] == mod):
                    pilot.append(s["subject_key"])
                    break
        if pilot:
            break
    ds = spec["dataset"]
    dataset = {"dataset_uuid": dataset_uuid, "name": ds.get("name") or root.name, "source_url": ds.get("source_url"),
               "license": ds.get("license"), "description": ds.get("description"),
               "commercial_use": ds.get("commercial_use", "U"), "patient_id_prefix": ds.get("patient_id_prefix")}
    plan = {"schema_version": 1, "adapter": ADAPTER, "adapter_version": ADAPTER_VERSION, "run_id": run_id,
            "created_at": now_iso(), "dataset": dataset, "input_root": str(root),
            "options": {"nifti_group_by": "subject", "nifti_layout": layout, "modality_override": None,
                        "max_series_per_study": 64},
            "subjects": [], "studies": [], "series": series, "skipped": skipped, "unresolved": unresolved,
            "skipped_dirs": [], "pilot": {"subjects": pilot},
            "analysis": {"summary_ko": spec["summary_ko"], "findings": spec["findings"],
                         "questions": spec["questions"], "sources": spec["sources"], "excluded": spec["excluded"],
                         "subject_pattern": spec["subject_pattern"], "modality_rules": spec["modality_rules"],
                         "rules_version": spec["rules_version"],
                         "label_pairing": spec["label_pairing"], "label_kind": spec["label_kind"],
                         "roi_name_regex": spec["roi_name_regex"],
                         "label_names": {str(k): v for k, v in spec["label_names"].items()},
                         "citation": ds.get("citation"), "left_out": left_out, "agent": agent_meta or {}}}
    inventory = {"schema_version": 1, "input_root": str(root), "created_at": now_iso(),
                 "counts": dict(_count(inv_files)), "skipped_dirs": [], "files": inv_files}
    plan, _ = validate_plan(plan, inventory)
    return plan, inventory


def _count(files):
    out = defaultdict(int)
    for f in files:
        out[f["kind"]] += 1
    return out
