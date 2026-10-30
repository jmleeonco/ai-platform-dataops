"""The intake rules shared by the code and the analysis agent: the `dataset-intake` skill.

The skill (.claude/skills/dataset-intake) holds the naming rule, vocabulary and layout recipes as text for the
agent, and codes.json as data for the code (prefix check), so both read one copy. The master copy of the
naming rule is in Notion (04-1 Data prefix 규칙, Track P); codes.json says which version it copies.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SKILL_NAME = "dataset-intake"
SKILL_DIR = Path(__file__).resolve().parents[1] / ".claude" / "skills" / SKILL_NAME
CODES = json.loads((SKILL_DIR / "codes.json").read_text(encoding="utf-8"))
RULES_VERSION = CODES["rules_version"]
PREFIX_RULE = re.compile(CODES["prefix_pattern"])
REGIONS = set(CODES["regions"])
TIERS = {k: set(v["commercial_use"]) for k, v in CODES["tiers"].items()}
