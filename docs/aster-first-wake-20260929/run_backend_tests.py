"""Run offline regressions against the owned candidate, never the live service."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
tests = root / "artifacts" / "isolated-tests"
tests.mkdir(parents=True, exist_ok=True)
for source in (root / "backend/api/tests").glob("test_game_companion*.py"):
    shutil.copy2(source, tests / source.name)
shutil.copytree(root / "backend/api/tests/fixtures", tests / "fixtures", dirs_exist_ok=True)
base = [
    "docker", "run", "--rm", "--network", "none", "--read-only",
    "--cpus", "1", "--memory", "512m",
    "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "-w", "/candidate",
    "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/candidate",
    "-e", "DATABASE_URL=postgresql://test:test@127.0.0.1:1/test",
    "-e", "REDIS_URL=redis://127.0.0.1:1/0",
    "-v", f"{root}/backend/api:/candidate/api:ro",
    "-v", f"{tests}:/tmp/aster-tests:ro",
    "-v", f"{root}/backend/api/routes/main.py:/tmp/routes/main.py:ro",
    "--entrypoint", "python", "dograh-api:aster-event-tests",
]
command = base + [
    "-m", "pytest", "--confcutdir=/tmp/aster-tests", "-p", "no:cacheprovider",
    "--asyncio-mode=auto", "-q", "/tmp/aster-tests",
]
print("Running all companion tests in a network-disabled, disposable container.", flush=True)
log = root / "artifacts/backend-tests.log"
if log.exists():
    raise SystemExit("Refusing to overwrite existing test evidence")
provenance = subprocess.run(base + ["-c", (
    "import pytest,pytest_asyncio; from api.services.game_companion import protocol,session,persona; "
    "from api.routes import game_companion; "
    "print('pytest',pytest.__version__,'pytest-asyncio',pytest_asyncio.__version__); "
    "print(*(m.__file__ for m in (protocol,session,persona,game_companion)),sep='\\n')"
)], capture_output=True, text=True, timeout=30)
with log.open("w") as output:
    output.write(provenance.stdout + provenance.stderr)
    if provenance.returncode:
        raise SystemExit(provenance.returncode)
    result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=90)
receipt = {"command": command, "exit_code": result.returncode, "log": str(log)}
(root / "artifacts/backend-test-result.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(log.read_text())
sys.exit(result.returncode)
