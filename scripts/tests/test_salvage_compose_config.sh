#!/usr/bin/env bash
# Focused contract tests for the Salvage Compose profile's MinIO host ports.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT INT TERM

render_config() {
    local output=$1
    shift
    env -i PATH="$PATH" HOME="${HOME:-/tmp}" \
        OSS_JWT_SECRET=test-only-compose-secret \
        "$@" \
        docker compose \
        --env-file /dev/null \
        -f "$REPO_ROOT/docker-compose.yaml" \
        -f "$REPO_ROOT/docker-compose.salvage.yaml" \
        config --format json > "$output"
}

assert_config() {
    local config=$1 api_port=$2 console_port=$3 public_endpoint=$4
    python3 - "$config" "$api_port" "$console_port" "$public_endpoint" <<'PY'
import json
import sys

config_path, api_port, console_port, public_endpoint = sys.argv[1:]
with open(config_path, encoding="utf-8") as config_file:
    config = json.load(config_file)

services = config["services"]
assert len(services["minio"]["ports"]) == 2
ports = {str(port["target"]): port for port in services["minio"]["ports"]}
assert ports["9000"]["host_ip"] == "127.0.0.1"
assert str(ports["9000"]["published"]) == api_port
assert ports["9000"]["protocol"] == "tcp"
assert ports["9001"]["host_ip"] == "127.0.0.1"
assert str(ports["9001"]["published"]) == console_port
assert ports["9001"]["protocol"] == "tcp"
assert services["api"]["environment"]["MINIO_ENDPOINT"] == "minio:9000"
assert services["api"]["environment"]["MINIO_PUBLIC_ENDPOINT"] == public_endpoint
PY
}

render_config "$TMP_ROOT/default.json"
assert_config "$TMP_ROOT/default.json" 9000 9001 http://localhost:9000
echo "ok - Salvage MinIO ports retain 9000/9001 defaults"

render_config "$TMP_ROOT/alternate.json" \
    MINIO_API_HOST_PORT=19000 \
    MINIO_CONSOLE_HOST_PORT=19001
assert_config "$TMP_ROOT/alternate.json" 19000 19001 http://localhost:19000
echo "ok - Salvage MinIO ports accept loopback host overrides"

render_config "$TMP_ROOT/explicit-endpoint.json" \
    MINIO_API_HOST_PORT=19000 \
    MINIO_PUBLIC_ENDPOINT=https://minio.example.test
assert_config "$TMP_ROOT/explicit-endpoint.json" \
    19000 9001 https://minio.example.test
echo "ok - explicit public MinIO endpoint overrides host-port derivation"
