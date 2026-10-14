"""Syntax parsing only: no Godot engine, scene import, or execution."""
import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "tools/python-deps"))
from gdtoolkit.parser import parser

parser._cache_dirpath = str(root / "tools/parser-cache")
paths = [
    "scripts/companion/companion_client.gd",
    "scripts/companion/lilt/lilt_encounter.gd",
    "tests/test_companion_client.gd",
    "tests/test_lilt_encounter_integration.gd",
    "tests/aster_event_check.gd",
]
results = []
for relative in paths:
    path = root / "game-candidate/game" / relative
    data = path.read_bytes()
    parser.parse(data.decode("utf-8").replace("\r\n", "\n"))
    results.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "syntax": "pass"})
receipt = {"parser": "gdtoolkit", "version": version("gdtoolkit"), "files": results,
           "engine_execution": "not_run", "type_and_signal_checks": "not_run"}
output = root / "artifacts/gdscript-parse.json"
if output.exists():
    raise SystemExit("Refusing to overwrite existing parsing evidence")
output.write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt, indent=2))
