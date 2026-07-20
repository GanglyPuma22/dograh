#!/usr/bin/env bash
# Focused tests for TURN split-host deployment rendering.
#
# Covers the dograh-init coturn renderer: an explicit local
# TURN_EXTERNAL_IP public/private NAT mapping must be preserved, the
# default local render must keep using TURN_HOST, and the production
# render must stay pinned to SERVER_IP.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FAILURES=0

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

pass() { echo "ok - $1"; }

fail() {
    echo "not ok - $1" >&2
    FAILURES=$((FAILURES + 1))
}

assert_line_once() {
    local file=$1 expected=$2 label=$3
    local count
    count=$(grep -cxF "$expected" "$file" 2>/dev/null || true)
    if [[ "$count" == "1" ]]; then
        pass "$label"
    else
        fail "$label (expected exactly one '$expected' line, got ${count:-0})"
        sed -n 's/^/  rendered: /p' <(grep '^external-ip=' "$file" 2>/dev/null) >&2 || true
    fi
}

run_dograh_init() {
    local out=$1
    shift
    env -i PATH="$PATH" HOME="${HOME:-/tmp}" \
        DOGRAH_INIT_WORKSPACE_DIR="$REPO_ROOT" \
        DOGRAH_INIT_OUTPUT_ROOT="$out" \
        "$@" \
        bash "$REPO_ROOT/scripts/run_dograh_init.sh" >/dev/null
}

test_local_render_preserves_explicit_external_ip() {
    local out="$TMP_ROOT/local-explicit"
    run_dograh_init "$out" \
        ENVIRONMENT=local \
        TURN_HOST=10.0.0.133 \
        TURN_INTERNAL_HOST=host.docker.internal \
        TURN_EXTERNAL_IP=10.0.0.133/172.28.0.8 \
        TURN_SECRET=test-only-secret
    assert_line_once "$out/coturn/turnserver.conf" \
        "external-ip=10.0.0.133/172.28.0.8" \
        "local render preserves explicit TURN_EXTERNAL_IP NAT mapping"
}

test_local_render_defaults_external_ip_to_turn_host() {
    local out="$TMP_ROOT/local-default"
    run_dograh_init "$out" \
        ENVIRONMENT=local \
        TURN_HOST=10.0.0.133 \
        TURN_SECRET=test-only-secret
    assert_line_once "$out/coturn/turnserver.conf" \
        "external-ip=10.0.0.133" \
        "local render without TURN_EXTERNAL_IP falls back to TURN_HOST"
}

test_remote_render_stays_pinned_to_server_ip() {
    local out="$TMP_ROOT/remote"
    local certs="$TMP_ROOT/certs"
    mkdir -p "$certs"
    : > "$certs/local.crt"
    : > "$certs/local.key"
    # Even with a stray TURN_EXTERNAL_IP in the environment, production
    # rendering must keep advertising SERVER_IP.
    run_dograh_init "$out" \
        DOGRAH_INIT_CERTS_DIR="$certs" \
        ENVIRONMENT=production \
        SERVER_IP=203.0.113.10 \
        PUBLIC_HOST=dograh.example.com \
        PUBLIC_BASE_URL=https://dograh.example.com \
        BACKEND_API_ENDPOINT=https://dograh.example.com \
        MINIO_PUBLIC_ENDPOINT=https://dograh.example.com \
        TURN_HOST=dograh.example.com \
        TURN_EXTERNAL_IP=198.51.100.7/172.28.0.9 \
        TURN_SECRET=test-only-secret \
        FASTAPI_WORKERS=1
    assert_line_once "$out/coturn/turnserver.conf" \
        "external-ip=203.0.113.10" \
        "remote render stays pinned to SERVER_IP"
}

test_local_render_preserves_explicit_external_ip
test_local_render_defaults_external_ip_to_turn_host
test_remote_render_stays_pinned_to_server_ip

if [[ "$FAILURES" -gt 0 ]]; then
    echo "$FAILURES test(s) failed" >&2
    exit 1
fi
echo "all TURN split-host script tests passed"
