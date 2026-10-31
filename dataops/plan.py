"""Versioned plan (schema 1): subjects / studies / series built from an inventory, and its validator.

The default adapter is deterministic: DICOM grouped by SeriesInstanceUID (passed through
unchanged), NIfTI images grouped by directory and converted to CT/MR DICOM. An agent or the
user may edit the plan JSON (modality, description, exclusions, NIfTI grouping key) and
`validate_plan` re-derives every identity that depends on those edits.
Dataset text and sidecars are data: nothing in them is executed.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from uuid import UUID

from . import deid
from .legacy import n2d
from .rtstruct import rtstruct_ids
from .rules import PREFIX_RULE, REGIONS, TIERS
from .util import now_iso, sha256_file, valid_uid

SCHEMA_VERSION = 1
ADAPTER = "default"
ADAPTER_VERSION = "1"
NIFTI_MODALITIES = ("CT", "MR")
PREFIX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")     # what is stored; the naming rule is prefix_problems
# Company naming rule (Notion "04-1. Data prefix 규칙" and "Track P", copied in the dataset-intake skill):
# <source>_<region>[_<modality>][_<sub>]_<tier>. The last token is the usability tier, which has to agree with
# commercial_use. PatientID = PatientName = <prefix>_<source ID> (or _NNNN when pseudonymised).


def prefix_problems(prefix: str | None, commercial_use: str) -> list[str]:
    """Why a PatientID prefix does not follow the company naming rule (empty when it does)."""
    if not prefix:
        return []
    if not PREFIX_RULE.fullmatch(prefix):
        return [f"접두어 {prefix!r}: 대문자·숫자·_ 만, 3~20자, 영문으로 시작, _로 끝나지 않음 (예: AMOS_ABD_MRCT_NC)"]
    parts = prefix.split("_")
    out = []
    if len(parts) < 3 or parts[1] not in REGIONS:
        out.append(f"접두어 {prefix!r}: 두 번째 칸은 부위 코드 {'/'.join(sorted(REGIONS))} (형식 <출처>_<부위>_…_<PUB|NC|IO>)")
    tier = parts[-1]
    if tier not in TIERS:
        out.append(f"접두어 {prefix!r}: 마지막 칸은 사용 등급 PUB / NC / IO 중 하나")
    elif commercial_use in ("Y", "N") and commercial_use not in TIERS[tier]:
        out.append(f"접두어 {prefix!r}: 등급 {tier}는 상업적 이용 {commercial_use}와 맞지 않음 (PUB=Y, NC=N, IO=내부 전용)")
    return out


def validate_dataset(dataset: dict) -> dict:
    if not isinstance(dataset, dict):
        raise ValueError("dataset metadata must be a JSON object")
    try:
        uuid = str(UUID(str(dataset.get("dataset_uuid"))))
    except ValueError:
        raise ValueError("dataset.dataset_uuid must be a UUID") from None
    name = dataset.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        raise ValueError("dataset.name must be 1..256 characters")
    for key, limit in (("license", 128), ("source_url", 512)):     # the lengths oncolabdb.lab_datasets takes
        value = dataset.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise ValueError(f"dataset.{key} must be at most {limit} characters")
    if dataset.get("commercial_use", "U") not in ("Y", "N", "U"):
        raise ValueError("dataset.commercial_use must be Y, N or U")
    prefix = dataset.get("patient_id_prefix")
    if prefix is not None and (not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix)):
        raise ValueError("dataset.patient_id_prefix must be 1..32 of [A-Za-z0-9_-]")
    out = {k: dataset.get(k) for k in ("name", "source_url", "license", "description")}
    out.update(dataset_uuid=uuid, commercial_use=dataset.get("commercial_use", "U"), patient_id_prefix=prefix)
    return out


def content_key(file_sha256: str, modality: str, layout: str) -> str:
    """Everything that changes the converted bytes; part of the derived Series UID."""
    return "|".join([file_sha256, modality or "", layout or "", n2d.CONVERTER_VERSION, "dataops-conv-1"])


def nifti_ids(prefix: str, group_label: str, rel: str, file_sha256: str, modality: str, layout: str, number: int):
    return n2d.make_series_ids(group_label, prefix, rel, content_key(file_sha256, modality, layout), number)


def _group_label(rel: str, group_by: str, root_name: str) -> str:
    parts = rel.split("/")
    if group_by == "file":
        return "_".join(parts[:-1] + [n2d.nifti_stem(Path(parts[-1]))])
    return "_".join(parts[:-1]) or root_name


def dicom_series(files: list[dict], root: Path) -> tuple[dict, dict, list[dict]]:
    """(subjects, studies, series) for the original DICOM files of an inventory, one series per Series UID.
    source_ids keeps the identity read from the files; validate_plan derives the outgoing one from it."""
    subjects, studies, series = {}, {}, []
    by_series = defaultdict(list)
    for f in files:
        if f["kind"] == "dicom":
            d = f["dicom"]
            key = d["series_uid"] or f"(missing series uid) {Path(f['rel']).parent.as_posix()}"
            by_series[key].append(f)
    for key, group in sorted(by_series.items()):
        group.sort(key=lambda f: f["rel"])
        first = group[0]["dicom"]
        seen, duplicates, conflicts, sources = {}, [], [], []
        for f in group:
            sop = f["dicom"]["sop_uid"]
            if sop in seen:
                a, b = sha256_file(root / seen[sop]), sha256_file(root / f["rel"])
                (duplicates if a == b else conflicts).append({"sop_uid": sop, "rel": f["rel"], "first": seen[sop]})
                continue
            seen[sop] = f["rel"]
            sources.append(f["rel"])
        patients = sorted({f["dicom"]["patient_id"] for f in group})
        studies_here = sorted({f["dicom"]["study_uid"] for f in group})
        meta = dict(first["series"])
        subject_key = first["patient_id"]
        subjects.setdefault(subject_key, {"source_subject_key": subject_key, "patient_id": first["patient_id"],
                                          "origin": "source:PatientID"})
        studies.setdefault(first["study_uid"], {"study_uid": first["study_uid"], "subject_key": subject_key,
                                                 "origin": "source", "metadata": first["study"]})
        s = {"series_key": f"dicom:{key}", "kind": "dicom", "excluded": False,
             "subject_key": subject_key, "patient_id": first["patient_id"],
             "study_uid": first["study_uid"], "series_uid": first["series_uid"],
             "source_ids": {"patient_id": first["patient_id"], "study_uid": first["study_uid"],
                            "series_uid": first["series_uid"]},
             "modality": meta.get("Modality", ""), "modality_basis": "DICOM Modality tag",
             "needs_review": False, "description": str(meta.get("SeriesDescription", "")),
             "series_number": meta.get("SeriesNumber"), "instance_count": len(sources),
             "sop_class_uids": sorted({f["dicom"]["sop_class_uid"] for f in group}),
             "sources": sources, "duplicates": duplicates, "conflicts": conflicts,
             "metadata": meta, "conversion": None}
        if len(patients) > 1 or len(studies_here) > 1:
            s["conflicts"].append({"reason": "series spans several PatientID/Study UID values",
                                   "patients": patients, "studies": studies_here})
        series.append(s)
    return subjects, studies, series


def build_plan(inv: dict, dataset: dict, run_id: str, options: dict | None = None) -> dict:
    options = {"nifti_group_by": "dir", "nifti_layout": "auto", "modality_override": None,
               "max_series_per_study": 16, **(options or {})}
    dataset = validate_dataset(dataset)
    root = Path(inv["input_root"])
    subjects, studies, series, skipped, unresolved = {}, {}, [], [], []

    # ---------------- DICOM pass-through ----------------
    subjects, studies, series = dicom_series(inv["files"], root)

    # ---------------- NIfTI conversion proposals ----------------
    sidecars = {f["rel"] for f in inv["files"] if f["kind"] == "sidecar"}
    images = []
    for f in inv["files"]:
        if f["kind"] != "nifti":
            continue
        item, vol = n2d.classify(root / f["rel"], root, options["nifti_layout"])
        if item.kind != "image":
            reason = item.reason
            if item.kind == "mask":
                reason = f"마스크는 아직 변환하지 않음(차단): {item.reason}"
            skipped.append({"rel": f["rel"], "kind": item.kind, "reason": reason})
            continue
        if options["modality_override"]:
            modality, basis, review = options["modality_override"], "plan option modality_override", False
        else:
            modality, basis = n2d.infer_modality(item, vol)
            review = True
        data, _, _ = n2d.canonicalize(vol.data, vol.affine_lps)
        stem = n2d.nifti_stem(Path(f["rel"]))
        sidecar = next((s for s in (f["rel"][: -len(Path(f["rel"]).name)] + stem + ".json",) if s in sidecars), None)
        images.append({"rel": f["rel"], "layout": item.layout, "modality": modality, "basis": basis,
                       "needs_review": review, "slices": int(data.shape[2]), "shape": [int(x) for x in data.shape],
                       "notes": item.notes, "sidecar": sidecar, "sha256": sha256_file(root / f["rel"])})
    groups = defaultdict(list)
    for im in images:
        groups[_group_label(im["rel"], options["nifti_group_by"], root.name)].append(im)
    if any(groups.values()) and not dataset["patient_id_prefix"]:
        unresolved.append({"code": "patient_id_prefix_missing", "blocking": True,
                           "message": "NIfTI 대상자 PatientID 접두어(dataset.patient_id_prefix)가 필요합니다. "
                                      "LOCALREVIEW 같은 임시 ID는 쓰지 않습니다."})
    for label, items in sorted(groups.items()):
        if options["nifti_group_by"] == "dir" and len(items) > options["max_series_per_study"]:
            unresolved.append({"code": "too_many_series_in_group", "blocking": True,
                               "message": f"그룹 '{label}'에 NIfTI 영상 {len(items)}개 — 여러 대상자가 한 폴더에 있을 수 "
                                          "있습니다. nifti_group_by=file 로 다시 계획하세요.",
                               "series_keys": [f"nifti:{i['rel']}" for i in items]})
        for number, im in enumerate(sorted(items, key=lambda i: i["rel"]), start=1):
            s = {"series_key": f"nifti:{im['rel']}", "kind": "nifti", "excluded": False,
                 "subject_key": label, "patient_id": None, "study_uid": None, "series_uid": None,
                 "modality": im["modality"], "modality_basis": im["basis"], "needs_review": im["needs_review"],
                 "description": None, "series_number": number, "instance_count": im["slices"],
                 "sop_class_uids": [], "sources": [im["rel"]] + ([im["sidecar"]] if im["sidecar"] else []),
                 "duplicates": [], "conflicts": [],
                 "metadata": {"shape_xyz": im["shape"], "notes": im["notes"]},
                 "conversion": {"converter": n2d.CONVERTER_VERSION, "layout": im["layout"], "group_label": label,
                                "source_sha256": im["sha256"], "deterministic": True}}
            series.append(s)
            if im["modality"] is None:
                unresolved.append({"code": "modality_ambiguous", "blocking": True, "series_keys": [s["series_key"]],
                                   "message": f"{im['rel']}: modality 판단 불가 ({im['basis']}) — plan에서 CT/MR 지정"})
    for f in inv["files"]:
        if f["kind"] in ("other", "unreadable", "dicomdir", "skipped"):
            skipped.append({"rel": f["rel"], "kind": f["kind"], "reason": f.get("reason", "지원하지 않는 파일")})

    plan = {"schema_version": SCHEMA_VERSION, "adapter": ADAPTER, "adapter_version": ADAPTER_VERSION,
            "run_id": run_id, "created_at": now_iso(), "dataset": dataset, "input_root": inv["input_root"],
            "options": options, "subjects": list(subjects.values()), "studies": list(studies.values()),
            "series": series, "skipped": skipped, "unresolved": unresolved,
            "skipped_dirs": inv.get("skipped_dirs", [])}
    normalized, _ = validate_plan(plan, inv)
    return normalized


def validate_plan(plan: dict, inv: dict) -> tuple[dict, list[str]]:
    """Return (plan with derived identities recomputed, blocking errors)."""
    errors: list[str] = []
    if plan.get("schema_version") != SCHEMA_VERSION:
        return plan, [f"plan schema_version {plan.get('schema_version')!r} 미지원 (지원: {SCHEMA_VERSION})"]
    try:
        plan["dataset"] = validate_dataset(plan.get("dataset"))
    except ValueError as e:
        errors.append(str(e))
        return plan, errors
    prefix = plan["dataset"]["patient_id_prefix"]
    errors += prefix_problems(prefix, plan["dataset"]["commercial_use"])
    deid.number(plan)
    if deid.enabled(plan):
        plan["deid"]["profile"] = deid.PROFILE      # a new profile changes the plan, so older artifacts are not reused
    inv_files = {f["rel"]: f for f in inv["files"]}
    subjects, studies = {}, {}
    all_by_key = {s["series_key"]: s for s in plan["series"]}
    agent_spec = plan.get("adapter") == "agent-spec"

    # Images first: an RTSTRUCT takes its patient/study/UID from the image it is drawn on.
    for s in sorted(plan["series"], key=lambda x: x["kind"] == "rtstruct"):
        key = s["series_key"]
        if s["kind"] == "rtstruct":
            conv, rel = s["conversion"], s["sources"][0]
            ref = all_by_key.get(s.get("reference"))
            if ref is None or ref["kind"] != "nifti":
                errors.append(f"{key}: 참조 영상 시리즈 {s.get('reference')!r} 가 없습니다")
                continue
            if ref.get("excluded"):
                if not s.get("excluded"):
                    s["excluded"], s["excluded_by_reference"] = True, True
            elif s.pop("excluded_by_reference", False):
                s["excluded"] = False
            if not ref.get("series_uid"):
                if not s.get("excluded"):
                    errors.append(f"{key}: 참조 영상의 UID가 아직 정해지지 않았습니다 (영상 시리즈 오류 먼저 해결)")
                continue
            uid, _ = rtstruct_ids(ref["series_uid"], conv["identity"])
            s.update(subject_key=ref["subject_key"], patient_id=ref["patient_id"], study_uid=ref["study_uid"],
                     series_uid=uid, series_number=1000 + int(ref["series_number"]),
                     description=s.get("description") or n2d._clip(f"{ref['description']} labels"))
            if s.get("excluded"):
                continue
            for r in s["sources"]:
                f = inv_files.get(r)
                if f is None or f["kind"] != "nifti":
                    errors.append(f"{key}: 입력 목록에 없는 라벨 NIfTI {r}")
        elif s["kind"] == "nifti":
            conv = s["conversion"]
            rel = s["sources"][0]
            if s.get("modality") not in NIFTI_MODALITIES:
                if not s.get("excluded"):
                    errors.append(f"{key}: NIfTI modality는 CT 또는 MR 이어야 합니다 (현재 {s.get('modality')!r})")
                continue
            if not prefix:
                if not s.get("excluded"):
                    errors.append(f"{key}: dataset.patient_id_prefix 없음 — PatientID를 정할 수 없습니다")
                continue
            ids = nifti_ids(prefix, deid.subject(plan, conv["group_label"]), rel,
                            conv.get("source_sha256") or conv["identity"], s["modality"], conv["layout"],
                            int(s["series_number"]))
            derived = (f"{deid.subject(plan, conv['group_label'])} s{int(s['series_number'])}"   # no file name
                       if deid.enabled(plan) else ids.series_description)
            s.update(subject_key=conv["group_label"], patient_id=ids.patient_id, study_uid=ids.study_uid,
                     series_uid=ids.series_uid, description=s.get("description") or derived)
            conv["patient_name"] = ids.patient_id          # PatientName = PatientID (naming rule)
            conv["study_description"] = n2d._clip(plan["dataset"]["name"]) if agent_spec else ids.study_description
            conv["frame_of_reference_uid"] = ids.frame_of_reference_uid
            if s.get("excluded"):
                continue
            f = inv_files.get(rel)
            if f is None or f["kind"] != "nifti":
                errors.append(f"{key}: 입력 목록에 없는 NIfTI {rel}")
        else:
            src = s.setdefault("source_ids", {k: s[k] for k in ("patient_id", "study_uid", "series_uid")})
            if not prefix:
                if not s.get("excluded"):
                    errors.append(f"{key}: dataset.patient_id_prefix 없음 — PatientID를 정할 수 없습니다")
                continue
            s.update(patient_id=f"{prefix}_{deid.subject(plan, src['patient_id'])}",
                     study_uid=deid.uid(plan, src["study_uid"]), series_uid=deid.uid(plan, src["series_uid"]))
            if s.get("excluded"):
                continue
            for rel in s["sources"]:
                f = inv_files.get(rel)
                if f is None or f["kind"] != "dicom" or f["dicom"]["series_uid"] != src["series_uid"]:
                    errors.append(f"{key}: 입력 목록과 맞지 않는 DICOM 파일 {rel}")
                    break
                d = f["dicom"]
                if d["patient_id"] != src["patient_id"] or d["study_uid"] != src["study_uid"]:
                    errors.append(f"{key}: 파일의 PatientID/Study UID가 계획과 다릅니다 ({rel})")
                    break
            if s["conflicts"]:
                errors.append(f"{key}: 같은 SOP UID에 다른 바이트 또는 여러 환자/검사 — 확인 필요 {s['conflicts'][:3]}")
        if s.get("excluded"):
            continue
        for field, value in (("study_uid", s["study_uid"]), ("series_uid", s["series_uid"])):
            if not valid_uid(value):
                errors.append(f"{key}: {field} {value!r} 가 올바른 DICOM UID가 아닙니다")
        pid = s.get("patient_id")
        if not isinstance(pid, str) or not pid.strip() or len(pid) > 64:
            errors.append(f"{key}: PatientID는 1..64자여야 합니다 (LAB 호환)")
        if not isinstance(s.get("modality"), str) or not 1 <= len(s["modality"]) <= 16:
            errors.append(f"{key}: modality 필요")
        if subjects.setdefault(s["subject_key"], pid) != pid:
            errors.append(f"{key}: 대상자 {s['subject_key']!r} 에 PatientID가 여러 개")
        if studies.setdefault(s["study_uid"], s["subject_key"]) != s["subject_key"]:
            errors.append(f"{key}: Study UID {s['study_uid']} 가 여러 대상자에 속함")
    active = [s for s in plan["series"] if not s.get("excluded")]
    pids = defaultdict(set)
    for skey, pid in subjects.items():
        pids[pid].add(skey)
    errors += [f"PatientID {pid!r} 를 여러 대상자가 공유 ({sorted(keys)})" for pid, keys in pids.items() if len(keys) > 1]
    uids = defaultdict(list)
    for s in active:
        uids[s["series_uid"]].append(s["series_key"])
    errors += [f"Series UID {u} 중복: {keys}" for u, keys in uids.items() if len(keys) > 1]
    if not active:
        errors.append("등록할 시리즈가 없습니다")

    # Subjects/studies are views of the active series, rebuilt so edits can't leave orphans.
    plan["subjects"] = [{"source_subject_key": k, "patient_id": v,
                         "origin": "derived:prefix+group" if any(s["kind"] == "nifti" and s["subject_key"] == k
                                                                 for s in active) else "source:PatientID"}
                        for k, v in sorted(subjects.items())]
    # a source study keeps its metadata when pseudonymisation switches its UID
    old_meta = {st.get("source_study_uid", st["study_uid"]): st.get("metadata", {}) for st in plan.get("studies", [])}
    source_uid = {s["study_uid"]: s["source_ids"]["study_uid"] for s in plan["series"] if s.get("source_ids")}
    plan["studies"] = []
    for u, k in sorted(studies.items()):
        src = source_uid.get(u, u)
        plan["studies"].append({"study_uid": u, "subject_key": k,
                                "origin": "source" if old_meta.get(src) else "generated",
                                "metadata": old_meta.get(src, {}), **({"source_study_uid": src} if src != u else {})})
    by_key = {s["series_key"]: s for s in active}
    group_sizes = defaultdict(int)
    for s in active:
        if s["kind"] == "nifti":
            group_sizes[s["conversion"]["group_label"]] += 1
    limit = int(plan.get("options", {}).get("max_series_per_study", 16))
    if plan.get("options", {}).get("nifti_group_by", "dir") == "dir":
        errors += [f"NIfTI 그룹 {g!r} 에 시리즈 {n}개 (> {limit}) — 여러 대상자일 수 있음" for g, n in group_sizes.items()
                   if n > limit]

    def still_open(u):
        refs = [by_key[k] for k in u.get("series_keys", []) if k in by_key]
        if u["code"] == "patient_id_prefix_missing":
            return not prefix and any(s["kind"] == "nifti" for s in active)
        if u["code"] == "modality_ambiguous":
            return any(s.get("modality") not in NIFTI_MODALITIES for s in refs)
        if u["code"] == "too_many_series_in_group":
            return any(group_sizes[s["conversion"]["group_label"]] > limit for s in refs)
        if u["code"] == "no_image":
            return not has_image
        return bool(refs) or not u.get("series_keys")
    has_image = any(s["kind"] in ("nifti", "dicom") for s in active)
    if not has_image:
        errors.append("변환할 영상 시리즈가 없습니다 — 에이전트의 질문·제외 사유를 보고 폴더나 구성을 정한 뒤 다시 분석하세요")
    plan["unresolved"] = [u for u in plan.get("unresolved", []) if still_open(u)]
    return plan, errors
