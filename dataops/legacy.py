"""Import the inherited local sender and NIfTI converter from the send-local-scp skill.

They stay where they are (the skill keeps working on its own); dataops reuses their
local-host guard, C-STORE loop and converter instead of copying them.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "send-local-scp" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import nifti_to_dicom as n2d  # noqa: E402
import send_local_scp as sender  # noqa: E402

__all__ = ["n2d", "sender"]
