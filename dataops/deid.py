"""Optional pseudonymisation of what leaves this machine. Off by default: public datasets are already
de-identified and keep their published case IDs. Switched on per run (a plan field, so it is part of the
approved plan), it replaces the subject key in PatientID / PatientName / SeriesDescription and in the DB with
a keyed pseudonym, drops the source path from ImageComments and stores a keyed hash instead of the source path
in the DB. Pseudonyms are HMAC-SHA256 under a key derived from a local secret and the dataset UUID, so the
same folder gets the same pseudonyms in every run and they cannot be reversed without the secret.

Original DICOM exports are rewritten instead of copied (`dicom`): PatientID/PatientName become the pseudonym,
every person name, the birth date and the identifiers listed in REMOVE are emptied, private tags are dropped
and every instance UID is replaced by a keyed one (the same mapping in every file, so an RTSTRUCT still points
at its CT). Study, series and acquisition dates and times are kept (owner's choice, 2026-10-30).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

import pydicom

from .config import CONFIG_DIR

PROFILE = "pseudonym-v1"


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
    plan["deid"] = {"enabled": True, "profile": PROFILE, "key": key}
    _reset_descriptions(plan)
    return plan


def disable(plan: dict) -> dict:
    plan["deid"] = {"enabled": False}
    _reset_descriptions(plan)
    return plan


def enabled(plan: dict) -> bool:
    return bool((plan.get("deid") or {}).get("enabled"))


def _mac(plan: dict, text: str) -> str:
    return hmac.new(bytes.fromhex(plan["deid"]["key"]), text.encode(), hashlib.sha256).hexdigest()


def subject(plan: dict, key: str) -> str:
    """The subject key as it leaves this machine."""
    return f"S{_mac(plan, 'subject:' + key)[:10].upper()}" if enabled(plan) else key


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


def patient_id(plan: dict, prefix: str | None, source_id: str) -> str:
    """PatientID of an original DICOM series: unchanged when off, prefix + pseudonym when on."""
    if not enabled(plan):
        return source_id
    pseudo = subject(plan, source_id)
    return f"{prefix}_{pseudo}" if prefix else pseudo


STANDARD_UID = "1.2.840.10008."          # SOP classes, transfer syntaxes, coding schemes: never replaced
# Emptied (type 2) or removed: identifiers of the person, the visit and the institution (PS3.15 Basic Profile,
# abridged). Person names (VR PN) are emptied wherever they occur.
REMOVE = {
    "PatientBirthDate", "PatientBirthTime", "OtherPatientIDs", "OtherPatientNames", "OtherPatientIDsSequence",
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


def metadata(plan: dict, meta: dict | None) -> dict | None:
    if not meta or not enabled(plan):
        return meta
    return {k: v for k, v in meta.items() if k not in METADATA_DROP}


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
    ds.DeidentificationMethod = f"{PROFILE}: keyed pseudonym, names/IDs removed, dates retained"
    ds.LongitudinalTemporalInformationModified = "UNMODIFIED"
    meta = getattr(ds, "file_meta", None)
    if meta is not None and "MediaStorageSOPInstanceUID" in meta:
        meta.MediaStorageSOPInstanceUID = uid(plan, str(meta.MediaStorageSOPInstanceUID))
    return ds


def redact(plan: dict, obj):
    """A copy of a page's data (report, tables) with source paths and subject keys shown as they leave, so the
    web pages do not show identifiers kept in folder names (unchanged when pseudonymisation is off)."""
    if not enabled(plan):
        return obj
    swap = {}
    for s in plan.get("series", []):
        if len(s["subject_key"]) >= 3:
            swap[s["subject_key"]] = subject(plan, s["subject_key"])
        for rel in s.get("sources") or []:
            parts = rel.split("/")
            for n in range(1, len(parts) + 1):
                swap.setdefault("/".join(parts[:n]), path(plan, "/".join(parts[:n])))
    swap.pop(".", None)
    if not swap:
        return obj
    rx = re.compile("|".join(re.escape(k) for k in sorted(swap, key=len, reverse=True)))

    def walk(o):
        if isinstance(o, str):
            return rx.sub(lambda m: swap[m.group(0)], o)
        if isinstance(o, dict):
            return {walk(k) if isinstance(k, str) else k: walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        return o
    return walk(obj)
