"""Central SCP publication: a separate adapter that consumes only approved, frozen artifacts.

It does not use (or loosen) the local sender's host guard; it builds its own destination from the
DATAOPS_CENTRAL_* settings and refuses unless all of these hold:
  - publication is enabled (DATAOPS_CENTRAL_ENABLED=true) and the target is fully configured,
  - the run carries an approval for exactly the current plan (plan sha256) given after a successful pilot,
  - the full manifest was frozen from that approved plan and every artifact re-hashes equal.
Each instance's result is journalled (transfers/central_*.json); a retry sends only what is not accepted.
"""
from __future__ import annotations

from .config import CentralScp, Settings
from .legacy import sender
from .local_send import send_with_journal
from .runs import Run, RunError
from .util import now_iso, sha256_json

PUBLISH_STATES = {"PREPARED", "LOCAL_SENT", "LOCAL_PARTIAL", "LOCAL_FAILED", "CENTRAL_SENT", "CENTRAL_PARTIAL",
                  "CENTRAL_FAILED"}


def central_ready(central: CentralScp) -> tuple[bool, str]:
    if not central.configured:
        return False, "Central SCP 미설정 (DATAOPS_CENTRAL_HOST/PORT/CALLED_AE)"
    if not central.enabled:
        return False, "Central 전송 비활성 (DATAOPS_CENTRAL_ENABLED=true 필요)"
    return True, central.key


def received_samples(run: Run, pilot: dict) -> list[tuple[str, str]]:
    """(target, subject) for every sample of the current pilot manifest whose files the local SCP all accepted."""
    from .local_send import journals
    out = []
    uid_subject = {se["series_uid"]: se["subject_key"] for se in pilot["series"]}
    for j in journals(run):
        if j.get("scope") != "pilot" or j["manifest_sha256"] != pilot["manifest_sha256"]:
            continue
        by_subject = {}
        for i in pilot["instances"]:
            ok = j["instances"].get(i["sop_uid"], {}).get("status") in ("accepted", "warning")
            subj = uid_subject[i["series_uid"]]
            by_subject[subj] = by_subject.get(subj, True) and ok
        out += [(j["target_key"], subj) for subj, ok in sorted(by_subject.items()) if ok]
    return out


def approve(run: Run, approver: str, confirm: str, note: str = "") -> dict:
    """Record the owner's approval of the current plan after at least one sample was fully received."""
    approver = (approver or "").strip()
    if not approver or len(approver) > 64:
        raise RunError("승인자 이름을 입력하세요 (1..64자)")
    with run.lock():
        meta, plan = run.meta, run.load("plan.json")
        if meta["status"] != "PILOT_SENT":
            raise RunError(f"파일럿이 모두 수신된 뒤에만 승인할 수 있습니다 (현재 {meta['status']})")
        if confirm.strip() != plan["dataset"]["name"].strip():
            raise RunError("확인 문구가 데이터셋 이름과 다릅니다")
        if plan["dataset"].get("commercial_use") not in ("Y", "N"):     # Track P: U = not yet usable
            raise RunError("라이선스(상업적 이용)가 미확정(U)이면 편입할 수 없습니다 — Y 또는 N으로 정한 뒤 승인하세요")
        pilot = run.load("manifest-pilot.json")
        if not pilot or pilot["plan_sha256"] != meta["plan_sha256"]:
            raise RunError("파일럿이 현재 plan으로 만들어지지 않았습니다 — 파일럿을 다시 보내세요")
        received = received_samples(run, pilot)
        if not received:
            raise RunError("현재 샘플 중 로컬 SCP에 모든 파일이 수신된 샘플이 없습니다")
        if sha256_json(plan) != meta["plan_sha256"]:
            raise RunError("plan.json이 검증 이후 수정되었습니다")
        approval = {"approved_by": approver, "approved_at": now_iso(), "plan_sha256": meta["plan_sha256"],
                    "plan_revision": meta["revision"], "pilot_manifest_sha256": pilot["manifest_sha256"],
                    "pilot_target": received[0][0], "pilot_subjects": [subj for _, subj in received],
                    "note": (note or "")[:1000]}
        run.set_status("APPROVED", f"승인: {approver}", approval=approval)
        return approval


def publish_central(run: Run, settings: Settings, dry_run: bool = False) -> dict:
    ok, why = central_ready(settings.central)
    if not ok:
        raise RunError(why)
    c = settings.central
    dest = sender.Destination(host=c.host, port=int(c.port), called_ae=sender._check_ae("central.called_ae", c.called_ae),
                              calling_ae=sender._check_ae("central.calling_ae", c.calling_ae))
    with run.lock():
        meta = run.meta
        if meta["status"] not in PUBLISH_STATES:
            raise RunError(f"Central 전송은 전체 준비(PREPARED) 이후에만 가능합니다 (현재 {meta['status']})")
        approval = meta.get("approval")
        manifest = run.load("manifest.json")
        if not approval or not manifest or manifest["plan_sha256"] != approval["plan_sha256"]:
            raise RunError("승인된 plan으로 고정된 manifest가 아닙니다 — Central 전송 거부")
        target = {"host": c.host, "port": int(c.port), "called_ae": c.called_ae, "calling_ae": c.calling_ae,
                  "kind": "central", "approved_by": approval["approved_by"]}
        r = send_with_journal(run, "full", c.key, target, dest, dry_run)
        if dry_run:
            return r
        new = f"CENTRAL_{r['outcome']}"
        run.set_status(new, f"central → {c.key}: {r['counts']}")
        return {**r, "status": new}
