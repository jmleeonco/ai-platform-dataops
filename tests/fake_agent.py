"""Test double for `claude -p --output-format json --json-schema ...`: prints the proposal named by
DATAOPS_FAKE_PROPOSAL (a JSON file) in the same envelope, after reading the prompt from stdin."""
import json
import os
import sys

sys.stdin.read()
path = os.environ.get("DATAOPS_FAKE_PROPOSAL")
if not path:
    print(json.dumps({"is_error": True, "subtype": "error", "result": "no proposal"}))
    sys.exit(1)
proposal = json.load(open(path, encoding="utf-8"))
print(json.dumps({"is_error": False, "subtype": "success", "num_turns": 1, "total_cost_usd": 0.0,
                  "session_id": "fake", "structured_output": proposal}))
