"""Optional pseudonymisation of what leaves this machine. Off by default: public datasets are already
de-identified and keep their published case IDs. Switched on per run (a plan field, so it is part of the
approved plan), it replaces the subject key in PatientID / PatientName / SeriesDescription and in the DB with
a keyed pseudonym, drops the source path from ImageComments and stores a keyed hash instead of the source path
in the DB. Pseudonyms are HMAC-SHA256 under a key derived from a local secret and the dataset UUID, so the
same folder gets the same pseudonyms in every run and they cannot be reversed without the secret.

Original DICOM exports passed through unchanged are not covered yet: with pseudonymisation on, such a plan
is refused (validate_plan) rather than sent with identifiers.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

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
