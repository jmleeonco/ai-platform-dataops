"""Optional pseudonymisation of what leaves this machine. Off by default: public datasets are already
de-identified and keep their published case IDs (PatientID = <prefix>_<case ID>). Switched on per run (a plan
field, so it is part of the approved plan), it replaces the subject key in PatientID / PatientName /
SeriesDescription and in the DB with a pseudonym number (PatientID = <prefix>_0001, the company naming rule),
drops the source path from ImageComments and stores a keyed hash instead of the source path in the DB. Path
hashes are HMAC-SHA256 under a key derived from a local secret and the dataset UUID, so the same folder gets
the same values in every run and they cannot be reversed without the secret.

Original DICOM exports are rewritten instead of copied (`dicom`): PatientID/PatientName become the pseudonym,
every person name, the birth date and the identifiers listed in REMOVE are emptied, private tags are dropped
and every instance UID is replaced by a keyed one (the same mapping in every file, so an RTSTRUCT still points
at its CT). Study, series and acquisition dates and times are kept (owner's choice, 2026-10-30).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

import pydicom

from .config import CONFIG_DIR

# v2 (owner, 2026-11-01): only what is needed goes; the birth date is kept like the study dates.
PROFILE = "pseudonym-v2"


def secret_path() -> Path:
    return Path(os.environ.get("DATAOPS_DEID_KEY_FILE", CONFIG_DIR / "deid.key"))


def _secret() -> bytes:
    path = secret_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return bytes.fromhex(path.read_text().strip())


def _reset_descriptions(plan: dict) -> None:
    """Converted series take their description from the subject key; validate_plan derives it again."""
    for s in plan.get("series", []):
        if s["kind"] in ("nifti", "rtstruct"):
            s["description"] = None


def enable(plan: dict) -> dict:
    duuid = plan["dataset"]["dataset_uuid"]
    key = hmac.new(_secret(), f"dataset:{duuid}".encode(), hashlib.sha256).hexdigest()
    plan["deid"] = {"enabled": True, "profile": PROFILE, "key": key,
                    "subjects": (plan.get("deid") or {}).get("subjects") or {}}
    _reset_descriptions(plan)
    number(plan)
    return plan


def disable(plan: dict) -> dict:
    """Numbers already given are kept, so switching back on gives the same pseudonyms."""
    plan["deid"] = {"enabled": False, **({"subjects": plan["deid"]["subjects"]}
                                         if (plan.get("deid") or {}).get("subjects") else {})}
    _reset_descriptions(plan)
    return plan


def enabled(plan: dict) -> bool:
    return bool((plan.get("deid") or {}).get("enabled"))


def _mac(plan: dict, text: str) -> str:
    return hmac.new(bytes.fromhex(plan["deid"]["key"]), text.encode(), hashlib.sha256).hexdigest()


mac = _mac          # the keyed hash, for the internal_hash columns of the deid_* maps


def number(plan: dict) -> None:
    """Give every subject of the plan a pseudonym number (0001, 0002, ...), kept in plan["deid"]["subjects"].
    Numbers already given never change; new subjects continue after the highest one, in keyed-hash order so the
    numbering says nothing about the source IDs. The mapping stays in the run folder (it is the re-identification
    key) and is left out of everything exported."""
    if not enabled(plan):
        return
    numbers = plan["deid"].setdefault("subjects", {})
    keys = {s["subject_key"] for s in plan.get("series", [])} - set(numbers)
    for key in sorted(keys, key=lambda k: _mac(plan, "subject:" + k)):
        numbers[key] = max(numbers.values(), default=0) + 1


def subject(plan: dict, key: str) -> str:
    """The subject key as it leaves this machine: unchanged, or its number when pseudonymised."""
    if not enabled(plan):
        return key
    n = (plan["deid"].get("subjects") or {}).get(key)
    if n is None:
        raise ValueError(f"가명 번호가 없는 대상자입니다 — plan을 다시 검증하세요 ({PROFILE})")
    return f"{n:04d}"


def path(plan: dict, rel: str | None) -> str | None:
    """A source path as it is stored in the DB."""
    if rel is None or not enabled(plan):
        return rel
    return f"deid:{_mac(plan, 'path:' + rel)[:16]}"


def uid(plan: dict, value: str) -> str:
    """A source instance UID as it leaves this machine (2.25 form, at most 64 characters)."""
    if not enabled(plan) or not value or value.startswith(STANDARD_UID):
        return value
    return "2.25." + str(int(_mac(plan, "uid:" + value)[:30], 16))


STANDARD_UID = "1.2.840.10008."          # SOP classes, transfer syntaxes, coding schemes: never replaced
# Emptied (type 2) or removed: identifiers of the person, the visit and the institution (PS3.15 Basic Profile,
# abridged). Person names (VR PN) are emptied wherever they occur. Kept as they are (owner, 2026-11-01): dates and
# times, birth date, age, sex, weight, device and station names.
REMOVE = {
    "OtherPatientIDs", "OtherPatientNames", "OtherPatientIDsSequence",
    "PatientBirthName", "PatientMotherBirthName", "PatientAddress", "PatientTelephoneNumbers", "IssuerOfPatientID",
    "MedicalRecordLocator", "AccessionNumber", "StudyID", "InstitutionName", "InstitutionAddress",
    "InstitutionalDepartmentName", "AdmissionID", "RequestAttributesSequence", "PatientComments",
    "AdditionalPatientHistory", "RequestedProcedureID", "ScheduledProcedureStepID", "PerformedProcedureStepID",
    "PlacerOrderNumberImagingServiceRequest", "FillerOrderNumberImagingServiceRequest", "ImageComments",
    "ReferencedPatientSequence", "ResponsibleOrganization", "Occupation", "MilitaryRank", "BranchOfService",
    "CountryOfResidence", "RegionOfResidence", "EthnicGroup", "PatientReligiousPreference",
}
# Kept in the DB's series metadata only when pseudonymisation is off.
METADATA_DROP = {"InstitutionName"}
METADATA_UIDS = {"FrameOfReferenceUID"}      # replaced like every UID of the file


def metadata(plan: dict, meta: dict | None) -> dict | None:
    """Series metadata as it is stored in the DB: UIDs as they leave, institution dropped."""
    if not meta or not enabled(plan):
        return meta
    return {k: (uid(plan, v) if k in METADATA_UIDS and isinstance(v, str) else v)
            for k, v in meta.items() if k not in METADATA_DROP and k not in REMOVE}


def dicom(plan: dict, ds: pydicom.Dataset, out_patient_id: str) -> pydicom.Dataset:
    """Pseudonymise an original DICOM dataset in place (only called when enabled)."""
    ds.remove_private_tags()

    def scrub(d):
        for el in list(d):
            if el.keyword in REMOVE:
                if el.VR == "SQ":
                    del d[el.tag]
                else:
                    el.value = ""
            elif el.VR == "PN":
                el.value = ""
            elif el.VR == "UI" and el.value:
                vals = el.value if isinstance(el.value, pydicom.multival.MultiValue) else [el.value]
                new = [uid(plan, str(v)) for v in vals]
                el.value = new if len(new) > 1 else new[0]
            elif el.VR == "SQ":
                for item in el.value or []:
                    scrub(item)
    scrub(ds)
    ds.PatientID = out_patient_id
    ds.PatientName = out_patient_id
    ds.PatientIdentityRemoved = "YES"
    ds.DeidentificationMethod = f"{PROFILE}: keyed pseudonym, names/IDs removed, dates and birth date retained"
    ds.LongitudinalTemporalInformationModified = "UNMODIFIED"
    meta = getattr(ds, "file_meta", None)
    if meta is not None and "MediaStorageSOPInstanceUID" in meta:
        meta.MediaStorageSOPInstanceUID = uid(plan, str(meta.MediaStorageSOPInstanceUID))
    return ds

