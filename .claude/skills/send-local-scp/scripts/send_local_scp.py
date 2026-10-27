#!/usr/bin/env python
"""send-local-scp: send a directory to a *local* DICOM Storage SCP (C-STORE).

DICOM files are sent as they are; NIfTI images are converted to CT/MR DICOM
series first (see nifti_to_dicom.py). Source files are only read. Converted
files and a summary are kept in a run directory for later inspection.

  python send_local_scp.py send <dir> [--modality CT|MR] [--dry-run] ...
  python send_local_scp.py echo
  python send_local_scp.py inspect <file-or-dir>
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import ipaddress
import json
import logging
import socket
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pydicom
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian, UID
from pynetdicom import AE
from pynetdicom.sop_class import Verification

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nifti_to_dicom as n2d  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = SKILL_DIR.parents[2]
DEFAULT_CONFIG = SKILL_DIR / "local_scp.json"
DEFAULT_OUTPUT = PROJECT_DIR / "send_local_scp_runs"

UNCOMPRESSED = [ExplicitVRLittleEndian, ImplicitVRLittleEndian]
MAX_CONTEXTS = 120
DICOMDIR_SOP = "1.2.840.10008.1.3.10"
NON_DICOM_EXT = {
    ".json", ".csv", ".tsv", ".txt", ".md", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".bmp", ".gif",
    ".tif", ".tiff", ".pdf", ".zip", ".gz", ".tar", ".7z", ".py", ".nrrd", ".mha", ".mhd", ".raw", ".npy",
    ".npz", ".h5", ".hdf5", ".xml", ".html", ".log", ".ini", ".yaml", ".yml", ".hdr", ".img", ".pkl", ".mat",
}
WARNING_STATUSES = {0x0001, 0xB000, 0xB006, 0xB007}


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
@dataclass
class Destination:
    host: str
    port: int
    called_ae: str
    calling_ae: str
    timeout_s: float = 30.0

    def label(self) -> str:
        return f"{self.called_ae}@{self.host}:{self.port} (calling AE {self.calling_ae})"


def load_config(path: Path | None) -> tuple[dict, Path]:
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.exists():
        return {}, path
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), path
    except json.JSONDecodeError as e:
        raise ConfigError(f"설정 파일 JSON 오류 {path}: {e}") from e


def _check_ae(name: str, value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 16 or "\\" in value \
            or not all(32 <= ord(c) < 127 for c in value):
        raise ConfigError(f"{name} 값 {value!r} 이 올바른 AE Title이 아닙니다 (ASCII 1~16자)")
    return value


def local_addresses() -> set[str]:
    addrs = set()
    for name in (socket.gethostname(), socket.getfqdn(), "localhost"):
        try:
            addrs |= {i[4][0] for i in socket.getaddrinfo(name, None)}
        except OSError:
            pass
    return addrs


def check_local_host(host: str) -> None:
    """Refuse anything that is not this machine: the tool is for a local SCP only."""
    try:
        resolved = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except OSError as e:
        raise ConfigError(f"host {host!r} 이름 해석 실패: {e}") from e
    local = local_addresses()
    for a in resolved:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_unspecified or not (ip.is_loopback or a in local):
            raise ConfigError(
                f"host {host!r} ({a}) 는 이 PC의 주소가 아닙니다. send-local-scp 는 로컬 SCP 전용이며 "
                "Central/원격 SCP로는 보내지 않습니다. 로컬 SCP 주소(예: 127.0.0.1)를 설정하세요.")


def destination_from(cfg: dict, cfg_path: Path) -> Destination:
    d = cfg.get("destination") or {}
    missing = [k for k in ("host", "port", "called_ae", "calling_ae") if d.get(k) in (None, "")]
    if missing:
        where = cfg_path if cfg_path.exists() else f"{cfg_path} (없음)"
        raise ConfigError(f"로컬 SCP 설정 누락: destination.{', destination.'.join(missing)} — 설정 파일 {where}. "
                          f"예시: {SKILL_DIR / 'config.example.json'}")
    try:
        port = int(d["port"])
    except (TypeError, ValueError):
        raise ConfigError(f"destination.port {d['port']!r} 가 숫자가 아닙니다") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"destination.port {port} 범위 오류")
    dest = Destination(host=str(d["host"]).strip(), port=port,
                       called_ae=_check_ae("destination.called_ae", d["called_ae"]),
                       calling_ae=_check_ae("destination.calling_ae", d["calling_ae"]),
                       timeout_s=float(d.get("timeout_s", 30)))
    check_local_host(dest.host)
    return dest


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #
@dataclass
class SendItem:
    path: Path
    sop_class: str
    sop_uid: str
    ts: str
    series_key: str
    status: str = "pending"      # ok | warning | failed | pending
    detail: str = ""


@dataclass
class SeriesRecord:
    kind: str                    # DICOM | NIfTI->DICOM
    patient_id: str
    patient_name: str
    study_uid: str
    study_date: str
    series_uid: str
    series_number: str
    modality: str
    series_description: str
    source: str
    items: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def counts(self) -> tuple[int, int, int]:
        ok = sum(i.status in ("ok", "warning") for i in self.items)
        failed = sum(i.status == "failed" for i in self.items)
        return ok, failed, len(self.items)


def _read_dicom_header(path: Path):
    try:
        with open(path, "rb") as f:
            head = f.read(132)
        force = head[128:132] != b"DICM"
        ds = pydicom.dcmread(path, stop_before_pixels=True, force=force)
    except Exception:  # noqa: BLE001 - not DICOM
        return None
    if "SOPClassUID" not in ds or "SOPInstanceUID" not in ds:
        return None
    return ds


def scan(root: Path):
    dicom, nifti, ignored = [], [], Counter()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if n2d.is_nifti(p):
            nifti.append(p)
            continue
        ext = p.suffix.lower()
        if ext in NON_DICOM_EXT:
            ignored[ext] += 1
            continue
        ds = _read_dicom_header(p)
        if ds is None:
            ignored[ext or "(확장자 없음)"] += 1
        elif str(ds.SOPClassUID) == DICOMDIR_SOP:
            ignored["DICOMDIR"] += 1
        else:
            dicom.append((p, ds))
    return dicom, nifti, ignored


def dicom_series(root: Path, dicom) -> tuple[dict, list]:
    series, dup = {}, []
    seen = set()
    for p, ds in dicom:
        sop_uid = str(ds.SOPInstanceUID)
        if sop_uid in seen:
            dup.append(p.relative_to(root).as_posix())
            continue
        seen.add(sop_uid)
        key = str(ds.get("SeriesInstanceUID", "")) or f"(no series uid) {p.parent}"
        if key not in series:
            series[key] = SeriesRecord(
                kind="DICOM", patient_id=str(ds.get("PatientID", "")), patient_name=str(ds.get("PatientName", "")),
                study_uid=str(ds.get("StudyInstanceUID", "")), study_date=str(ds.get("StudyDate", "")),
                series_uid=str(ds.get("SeriesInstanceUID", "")), series_number=str(ds.get("SeriesNumber", "")),
                modality=str(ds.get("Modality", "")), series_description=str(ds.get("SeriesDescription", "")),
                source=p.parent.relative_to(root).as_posix() or ".")
        ts = str(ds.file_meta.get("TransferSyntaxUID", ImplicitVRLittleEndian)) if hasattr(ds, "file_meta") \
            else str(ImplicitVRLittleEndian)
        series[key].items.append(SendItem(p, str(ds.SOPClassUID), sop_uid, ts, key))
    return series, dup


# --------------------------------------------------------------------------- #
# NIfTI
# --------------------------------------------------------------------------- #
def group_label(rel: str, group_by: str, root: Path) -> str:
    parts = rel.split("/")
    if group_by == "file":
        return "_".join(parts[:-1] + [n2d.nifti_stem(Path(parts[-1]))])
    return "_".join(parts[:-1]) or root.name


def convert_nifti(root: Path, paths: list, opts: dict, run_dir: Path) -> tuple[dict, list, list]:
    """Returns (series records, skipped [(rel, kind, reason)], errors)."""
    layout, force = opts["layout"], opts["force_image"]
    images, skipped = [], []
    for p in paths:
        item, vol = n2d.classify(p, root, layout, force)
        if item.kind != "image":
            skipped.append((item.rel, item.kind, item.reason))
            continue
        if opts["modality"]:
            item.modality, item.modality_basis = opts["modality"], "사용자 지정(--modality/설정)"
        else:
            item.modality, item.modality_basis = n2d.infer_modality(item, vol)
            if item.modality is None:
                skipped.append((item.rel, "ambiguous", f"modality 판단 불가: {item.modality_basis} "
                                                       "— --modality CT 또는 MR 로 지정"))
                continue
        images.append(item)

    groups = defaultdict(list)
    for item in images:
        groups[group_label(item.rel, opts["group_by"], root)].append(item)
    limit = int(opts["max_series_per_study"])
    for label, items in list(groups.items()):
        if opts["group_by"] == "dir" and len(items) > limit:
            for it in items:
                skipped.append((it.rel, "ambiguous",
                                f"디렉토리 그룹 '{label}'에 영상 NIfTI {len(items)}개 (> max_series_per_study {limit}). "
                                "여러 환자가 한 폴더에 있을 수 있음 — --group-by file 사용 또는 한도 조정"))
            del groups[label]

    series, errors = {}, []
    out_root = run_dir / "nifti_dicom"
    for label, items in sorted(groups.items()):
        for number, item in enumerate(sorted(items, key=lambda i: i.rel), start=1):
            try:
                item2, vol = n2d.classify(item.path, root, item.layout, force)
                ids = n2d.make_series_ids(label, opts["prefix"], item.rel, n2d.file_sha256(item.path), number)
                sdir = out_root / _safe(ids.patient_id) / f"S{number:03d}_{_safe(ids.series_description)[:40]}"
                report = n2d.write_series(vol, ids, item.modality, item.rel, sdir, item.modality_basis)
            except Exception as e:  # noqa: BLE001 - keep going with other files
                errors.append((item.rel, f"변환 실패: {type(e).__name__}: {e}"))
                continue
            rec = SeriesRecord(
                kind="NIfTI->DICOM", patient_id=ids.patient_id, patient_name=ids.patient_name,
                study_uid=ids.study_uid, study_date="", series_uid=ids.series_uid,
                series_number=str(number), modality=item.modality, series_description=ids.series_description,
                source=item.rel)
            files = report.pop("files")
            rec.items = [SendItem(f, report["sop_class"], "", str(ExplicitVRLittleEndian), ids.series_uid)
                         for f in files]
            rec.extra = {"layout": item2.layout, "modality_basis": item.modality_basis, "notes": item2.notes,
                         "converted_dir": str(sdir), **report}
            series[ids.series_uid] = rec
    return series, skipped, errors


def _safe(s: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s)
    return keep.strip("._") or "x"


# --------------------------------------------------------------------------- #
# C-STORE
# --------------------------------------------------------------------------- #
def _context_batches(items: list) -> list:
    by_sop = defaultdict(set)
    for it in items:
        by_sop[it.sop_class].add(it.ts)
    batches, cur, n = [], [], 0
    for sop in sorted(by_sop):
        extra = sorted(by_sop[sop] - set(UNCOMPRESSED))
        need = 1 + len(extra)
        if cur and n + need > MAX_CONTEXTS:
            batches.append(cur)
            cur, n = [], 0
        cur.append((sop, extra))
        n += need
    if cur:
        batches.append(cur)
    return batches


def _assoc_failure(assoc, dest: Destination) -> str:
    if assoc.is_rejected:
        return "association 거부 (called/calling AE 또는 SCP 설정 확인)"
    try:
        socket.create_connection((dest.host, dest.port), timeout=3).close()
    except OSError:
        return f"연결 실패: {dest.host}:{dest.port} 에 열려 있는 SCP가 없음 (OncoStudio 등 로컬 SCP 실행 여부 확인)"
    if assoc.is_aborted:
        return "association abort (SCP가 연결을 끊음 — AE Title/SCP 설정 확인)"
    return "연결 실패 (타임아웃)"


def _send_one(assoc, it: SendItem) -> None:
    ds = pydicom.dcmread(it.path, force=True)
    if "TransferSyntaxUID" not in ds.file_meta:      # raw dataset without file meta (in memory only)
        ds.file_meta.TransferSyntaxUID = it.ts
    try:
        status = assoc.send_c_store(ds)
    except ValueError as e:
        ts = UID(it.ts)
        if ts.is_compressed:
            try:
                ds.decompress(generate_instance_uid=False)   # same instance, only the transfer syntax changes
                status = assoc.send_c_store(ds)
                it.detail = f"압축 해제 후 전송 ({ts.name})"
            except Exception as e2:  # noqa: BLE001
                it.status, it.detail = "failed", f"SCP가 {ts.name} 미수락, 압축 해제 실패: {e2}"
                return
        else:
            it.status, it.detail = "failed", f"presentation context 없음: {e}"
            return
    if not status or "Status" not in status:
        it.status, it.detail = "failed", "응답 없음 (타임아웃/연결 끊김)"
        return
    code = int(status.Status)
    if code == 0x0000:
        it.status = "ok"
    elif code in WARNING_STATUSES:
        it.status, it.detail = "warning", f"warning 0x{code:04X}"
    else:
        comment = str(status.get("ErrorComment", ""))
        it.status, it.detail = "failed", f"status 0x{code:04X} {comment}".strip()


def send_items(dest: Destination, items: list) -> None:
    logging.getLogger("pynetdicom").setLevel(logging.CRITICAL)
    by_sop = defaultdict(list)
    for it in items:
        by_sop[it.sop_class].append(it)
    for batch in _context_batches(items):
        ae = AE(ae_title=dest.calling_ae)
        ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = dest.timeout_s
        ae.connection_timeout = min(dest.timeout_s, 10)
        for sop, extra in batch:
            ae.add_requested_context(sop, UNCOMPRESSED)
            for ts in extra:
                ae.add_requested_context(sop, [ts])
        pending = [it for sop, _ in batch for it in by_sop[sop]]
        attempts = 0
        while pending and attempts < 3:
            attempts += 1
            assoc = ae.associate(dest.host, dest.port, ae_title=dest.called_ae)
            if not assoc.is_established:
                reason = _assoc_failure(assoc, dest)
                for it in pending:
                    it.status, it.detail = "failed", reason
                break
            try:
                for it in pending:
                    if not assoc.is_established:
                        break
                    _send_one(assoc, it)
            finally:
                if assoc.is_established:
                    assoc.release()
            pending = [it for it in pending if it.status == "pending"]
        for it in pending:
            it.status, it.detail = "failed", "연결이 반복해서 끊김"


def echo(dest: Destination) -> tuple[bool, str]:
    logging.getLogger("pynetdicom").setLevel(logging.CRITICAL)
    ae = AE(ae_title=dest.calling_ae)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = dest.timeout_s
    ae.connection_timeout = min(dest.timeout_s, 10)
    ae.add_requested_context(Verification)
    assoc = ae.associate(dest.host, dest.port, ae_title=dest.called_ae)
    if not assoc.is_established:
        return False, _assoc_failure(assoc, dest)
    try:
        status = assoc.send_c_echo()
    finally:
        assoc.release()
    if status and int(status.get("Status", -1)) == 0:
        return True, "C-ECHO 성공"
    return False, f"C-ECHO 응답 이상: {status}"


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #
def _short(uid: str, n: int = 12) -> str:
    return uid if len(uid) <= n + 1 else "…" + uid[-n:]


def print_summary(res: dict) -> None:
    series = res["series"]
    ok = sum(r["sent_ok"] for r in series)
    failed = sum(r["failed"] for r in series)
    total = sum(r["files"] for r in series)
    print("== send-local-scp 결과 ==")
    print(f"입력      : {res['input']}")
    print(f"수신지    : {res['destination'] or '(dry-run: 전송 안 함)'}")
    if res["dry_run"]:
        print(f"인스턴스  : {total}개 준비 (dry-run, 전송 안 함)")
    else:
        print(f"전송      : 성공 {ok} / 실패 {failed} / 전체 {total} 인스턴스")
    print(f"건너뜀    : NIfTI {len(res['skipped'])}개, 변환 오류 {len(res['errors'])}개, "
          f"DICOM 중복 {len(res['duplicates'])}개, 기타 파일 {sum(res['ignored'].values())}개")
    if series:
        wid = max(9, *(len(r["patient_id"]) for r in series))
        wname = max(11, *(len(r["patient_name"]) for r in series))
        print("\n[시리즈]")
        print(f"{'구분':<13} {'PatientID':<{wid}} {'PatientName':<{wname}} {'StudyUID':<14} {'Ser#':>4} {'Mod':<4} "
              f"{'성공/전체':>9}  SeriesDescription | SeriesInstanceUID")
        for r in series:
            print(f"{r['kind']:<13} {r['patient_id']:<{wid}} {r['patient_name']:<{wname}} "
                  f"{_short(r['study_uid']):<14} {r['series_number']:>4} {r['modality']:<4} "
                  f"{(str(r['sent_ok']) if not res['dry_run'] else '-') + '/' + str(r['files']):>9}  "
                  f"{r['series_description'][:40]} | {r['series_uid']}")
            if r["kind"] != "DICOM":
                g = r["extra"]
                vals = "값 무손실" if g["values_exact"] else f"값 양자화(최대오차 {g['max_abs_error']:.4g})"
                if g.get("nonfinite_voxels"):
                    vals += f", NaN/Inf {g['nonfinite_voxels']}개 대체"
                print(f"{'':<13} └ {r['source']} | {r['modality']} ← {g['modality_basis']} | {g['layout']} | "
                      f"{g['plane']} {g['shape_xyz']} spacing {g['spacing_mm']} | {vals}")
    fails = [(r, f) for r in series for f in r["failures"]]
    if fails:
        print("\n[실패]")
        for r, f in fails[:20]:
            print(f"  {f['path']}: {f['detail']}")
        if len(fails) > 20:
            print(f"  … 외 {len(fails) - 20}건 (summary.json 참조)")
    if res["skipped"] or res["errors"]:
        print("\n[건너뛴 NIfTI]")
        for rel, kind, reason in res["skipped"]:
            print(f"  [{kind}] {rel}: {reason}")
        for rel, reason in res["errors"]:
            print(f"  [error] {rel}: {reason}")
    if res["duplicates"]:
        print(f"\n[중복 SOPInstanceUID — 한 번만 전송] {', '.join(res['duplicates'][:5])}"
              + (" …" if len(res["duplicates"]) > 5 else ""))
    if res["ignored"]:
        print("\n기타 파일(전송 안 함): " + ", ".join(f"{k} {v}" for k, v in res["ignored"].most_common()))
    print(f"\n실행 기록 : {res['run_dir']} (summary.json, series.csv"
          + (", nifti_dicom/ 변환본)" if any(r["kind"] != "DICOM" for r in series) else ")"))


def build_result(root, dest, dry_run, series, skipped, errors, dup, ignored, run_dir) -> dict:
    rows = []
    for rec in series:
        ok, failed, total = rec.counts()
        rows.append({
            "kind": rec.kind, "patient_id": rec.patient_id, "patient_name": rec.patient_name,
            "study_uid": rec.study_uid, "study_date": rec.study_date, "series_uid": rec.series_uid,
            "series_number": rec.series_number, "modality": rec.modality,
            "series_description": rec.series_description, "source": rec.source,
            "files": total, "sent_ok": ok, "failed": failed,
            "warnings": [{"path": str(i.path), "detail": i.detail} for i in rec.items if i.status == "warning"],
            "failures": [{"path": str(i.path), "detail": i.detail} for i in rec.items if i.status == "failed"],
            "extra": rec.extra,
        })
    return {"input": str(root), "destination": dest.label() if dest else None, "dry_run": dry_run,
            "series": rows, "skipped": skipped, "errors": errors, "duplicates": dup,
            "ignored": ignored, "run_dir": str(run_dir)}


def write_result(res: dict, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    data = dict(res, ignored=dict(res["ignored"]))
    (run_dir / "summary.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                                          encoding="utf-8")
    cols = ["kind", "patient_id", "patient_name", "study_uid", "series_number", "modality",
            "series_description", "series_uid", "files", "sent_ok", "failed", "source"]
    with open(run_dir / "series.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(res["series"])


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def run_send(args) -> int:
    root = Path(args.input_dir).expanduser().resolve()
    if not root.is_dir():
        raise ConfigError(f"입력 디렉토리가 없습니다: {root}")
    cfg, cfg_path = load_config(args.config)
    dest = None if args.dry_run else destination_from(cfg, cfg_path)
    ncfg = cfg.get("nifti") or {}
    modality = (args.modality or ncfg.get("modality") or "auto").upper()
    opts = {
        "modality": None if modality == "AUTO" else modality,
        "prefix": ncfg.get("patient_id_prefix") or "LOCALREVIEW",
        "group_by": args.group_by or ncfg.get("group_by") or "dir",
        "layout": args.nifti_layout or ncfg.get("layout") or "auto",
        "max_series_per_study": ncfg.get("max_series_per_study", 16),
        "force_image": args.force_image or [],
    }
    if opts["modality"] and opts["modality"] not in n2d.SOP_CLASS_BY_MODALITY:
        raise ConfigError(f"modality {opts['modality']} 미지원 — v1은 CT, MR 또는 auto")
    out_base = Path(args.output_dir or cfg.get("output_dir") or DEFAULT_OUTPUT).expanduser().resolve()
    if _is_within(out_base, root):
        raise ConfigError(f"출력 위치 {out_base} 가 입력 디렉토리 안에 있습니다. 원본 폴더에는 쓰지 않습니다.")
    run_dir = out_base / f"{_dt.datetime.now():%Y%m%d_%H%M%S}_{_safe(root.name)[:40]}"

    dicom, nifti, ignored = scan(root)
    d_series, dup = dicom_series(root, dicom)
    n_series, skipped, errors = convert_nifti(root, nifti, opts, run_dir)
    all_series = list(d_series.values()) + list(n_series.values())
    items = [it for rec in all_series for it in rec.items]
    if items and not args.dry_run:
        send_items(dest, items)
    res = build_result(root, dest, args.dry_run, all_series, skipped, errors, dup, ignored, run_dir)
    write_result(res, run_dir)
    print_summary(res)
    if not items:
        print("\n전송할 DICOM/NIfTI 영상이 없습니다.")
    failed = any(r["failed"] for r in res["series"])
    return 1 if failed or errors else 0


def run_echo(args) -> int:
    cfg, cfg_path = load_config(args.config)
    dest = destination_from(cfg, cfg_path)
    ok, msg = echo(dest)
    print(f"{dest.label()}: {msg}")
    return 0 if ok else 1


INSPECT_TAGS = ["PatientID", "PatientName", "StudyInstanceUID", "StudyDescription", "SeriesInstanceUID",
                "SeriesNumber", "SeriesDescription", "Modality", "SOPClassUID", "SOPInstanceUID", "InstanceNumber",
                "FrameOfReferenceUID", "ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing",
                "SliceThickness", "Rows", "Columns", "BitsStored", "PixelRepresentation", "RescaleSlope",
                "RescaleIntercept", "WindowCenter", "WindowWidth", "ImageType", "ImageComments"]


def run_inspect(args) -> int:
    target = Path(args.path).expanduser().resolve()
    files = [target] if target.is_file() else [p for p in sorted(target.rglob("*")) if p.is_file()]
    by_series = defaultdict(list)
    for p in files:
        if p.suffix.lower() in NON_DICOM_EXT or n2d.is_nifti(p):
            continue
        ds = _read_dicom_header(p)
        if ds is not None:
            by_series[str(ds.get("SeriesInstanceUID", ""))].append((int(ds.get("InstanceNumber", 0) or 0), p, ds))
    if not by_series:
        print(f"DICOM 파일 없음: {target}")
        return 1
    for uid, lst in by_series.items():
        lst.sort(key=lambda t: (t[0], str(t[1])))
        picks = sorted({0, len(lst) // 2, len(lst) - 1})[: args.max_files]
        print(f"=== Series {uid} ({len(lst)} files)")
        for idx in picks:
            _, p, ds = lst[idx]
            print(f"--- {p}")
            print(f"    TransferSyntaxUID: {ds.file_meta.get('TransferSyntaxUID', '') if hasattr(ds, 'file_meta') else ''}")
            for tag in INSPECT_TAGS:
                if tag in ds:
                    print(f"    {tag}: {ds.get(tag)}")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    ap = argparse.ArgumentParser(prog="send_local_scp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help=f"설정 JSON (기본 {DEFAULT_CONFIG})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="디렉토리의 DICOM/NIfTI를 로컬 SCP로 전송")
    s.add_argument("input_dir")
    s.add_argument("--modality", type=str.upper, choices=["AUTO", "CT", "MR"],
                   help="NIfTI modality. 기본 auto: 사이드카 JSON > 경로 단어 > CT HU 값 분포, 근거 없으면 건너뜀")
    s.add_argument("--group-by", choices=["dir", "file"], help="NIfTI 환자/스터디 묶음 기준 (기본 dir)")
    s.add_argument("--nifti-layout", choices=["auto", "standard", "onco"], help="NIfTI 규약 (기본 auto)")
    s.add_argument("--force-image", action="append", metavar="GLOB",
                   help="mask/모호 판정 파일을 영상으로 처리 (상대경로 또는 파일명 glob, 반복 가능)")
    s.add_argument("--output-dir", help=f"실행 기록/변환본 위치 (기본 {DEFAULT_OUTPUT})")
    s.add_argument("--dry-run", action="store_true", help="변환/요약만, 네트워크 전송 안 함")
    sub.add_parser("echo", help="로컬 SCP C-ECHO 확인")
    i = sub.add_parser("inspect", help="DICOM 파일/폴더의 주요 메타데이터 출력")
    i.add_argument("path")
    i.add_argument("--max-files", type=int, default=3, help="시리즈당 출력 파일 수 (처음/중간/끝)")
    args = ap.parse_args(argv)
    try:
        return {"send": run_send, "echo": run_echo, "inspect": run_inspect}[args.cmd](args)
    except ConfigError as e:
        print(f"[설정/입력 오류] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
