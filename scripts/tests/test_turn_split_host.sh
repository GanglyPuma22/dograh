#!/usr/bin/env bash
# Focused tests for TURN split-host deployment rendering and local setup.
#
# Covers the dograh-init coturn renderer (an explicit local
# TURN_EXTERNAL_IP public/private NAT mapping must be preserved, the
# default local render must keep using TURN_HOST, and the production
# render must stay pinned to SERVER_IP) plus setup_local.{sh,ps1} parity
# for the optional TURN_INTERNAL_HOST / TURN_EXTERNAL_IP inputs. The
# PowerShell lane runs only when a PowerShell binary is available.

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
    # Line endings are normalized so CRLF output from setup_local.ps1 on
    # Windows matches the same assertions as LF output from setup_local.sh.
    local file=$1 expected=$2 label=$3
    local count
    count=$(tr -d '\r' < "$file" 2>/dev/null | grep -cxF "$expected" || true)
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

assert_absent() {
    local file=$1 pattern=$2 label=$3
    if tr -d '\r' < "$file" 2>/dev/null | grep -q "$pattern"; then
        fail "$label (found '$pattern' in $file)"
    else
        pass "$label"
    fi
}

populate_setup_workspace() {
    local dir=$1
    mkdir -p "$dir/scripts/lib" "$dir/deploy/templates"
    cp "$REPO_ROOT/docker-compose.yaml" "$dir/docker-compose.yaml"
    cp "$REPO_ROOT/scripts/run_dograh_init.sh" "$dir/scripts/run_dograh_init.sh"
    cp "$REPO_ROOT/scripts/lib/setup_common.sh" "$dir/scripts/lib/setup_common.sh"
    cp "$REPO_ROOT/deploy/templates/turnserver.remote.conf.template" \
        "$dir/deploy/templates/turnserver.remote.conf.template"
}

# Env vars forwarded into the setup scripts. WSLENV additionally forwards
# them across the WSL -> Windows boundary for powershell.exe.
SETUP_WSLENV="ENABLE_COTURN:TURN_HOST:TURN_SECRET:TURN_INTERNAL_HOST:TURN_EXTERNAL_IP:FORCE_TURN_RELAY:DOGRAH_SKIP_DOWNLOAD"

run_bash_setup() {
    local workspace=$1 log=$2
    shift 2
    (
        cd "$workspace" &&
            env ENABLE_COTURN=true DOGRAH_SKIP_DOWNLOAD=1 \
                TURN_HOST=10.0.0.133 TURN_SECRET=test-only-secret "$@" \
                bash "$REPO_ROOT/scripts/setup_local.sh"
    ) > "$log" 2>&1
}

find_powershell() {
    local candidate
    for candidate in pwsh pwsh.exe powershell.exe powershell \
        /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe; do
        if command -v "$candidate" >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# Windows PowerShell cannot write files from a \\wsl.localhost UNC working
# directory, so interop runs are staged in the Windows TEMP directory.
powershell_workspace_root() {
    local bin=$1 win_temp=""
    if [[ "$bin" == *.exe ]] && command -v wslpath >/dev/null 2>&1; then
        win_temp="$("$bin" -NoProfile -Command 'Write-Host $env:TEMP' 2>/dev/null | tr -d '\r')"
        [[ -n "$win_temp" ]] || return 1
        wslpath -u "$win_temp" 2>/dev/null
        return
    fi
    printf '%s\n' "$TMP_ROOT"
}

powershell_script_path() {
    local bin=$1
    if [[ "$bin" == *.exe ]] && command -v wslpath >/dev/null 2>&1; then
        wslpath -w "$REPO_ROOT/scripts/setup_local.ps1"
        return
    fi
    printf '%s\n' "$REPO_ROOT/scripts/setup_local.ps1"
}

run_powershell_setup() {
    local bin=$1 script=$2 workspace=$3 log=$4
    shift 4
    (
        cd "$workspace" &&
            env ENABLE_COTURN=true DOGRAH_SKIP_DOWNLOAD=1 \
                TURN_HOST=10.0.0.133 TURN_SECRET=test-only-secret \
                WSLENV="$SETUP_WSLENV" "$@" \
                "$bin" -NoProfile -ExecutionPolicy Bypass -File "$script"
    ) > "$log" 2>&1
}

assert_setup_env_split_host() {
    local env_file=$1 log=$2 lane=$3
    assert_line_once "$env_file" "TURN_HOST=10.0.0.133" \
        "$lane setup writes client-visible TURN_HOST once"
    assert_line_once "$env_file" "TURN_INTERNAL_HOST=host.docker.internal" \
        "$lane setup writes TURN_INTERNAL_HOST once"
    assert_line_once "$env_file" "TURN_EXTERNAL_IP=10.0.0.133/172.28.0.8" \
        "$lane setup writes TURN_EXTERNAL_IP once"
    assert_absent "$log" "test-only-secret" \
        "$lane setup never prints the TURN secret"
}

assert_setup_env_compat() {
    local env_file=$1 log=$2 lane=$3
    assert_line_once "$env_file" "TURN_HOST=10.0.0.133" \
        "$lane compat setup writes TURN_HOST once"
    assert_absent "$env_file" "^TURN_INTERNAL_HOST=" \
        "$lane compat setup omits TURN_INTERNAL_HOST"
    assert_absent "$env_file" "^TURN_EXTERNAL_IP=" \
        "$lane compat setup omits TURN_EXTERNAL_IP"
    assert_absent "$log" "test-only-secret" \
        "$lane compat setup never prints the TURN secret"
}

test_bash_setup_writes_split_host_keys() {
    local workspace="$TMP_ROOT/setup-bash-split"
    populate_setup_workspace "$workspace"
    if ! run_bash_setup "$workspace" "$workspace/setup.log" \
        TURN_INTERNAL_HOST=host.docker.internal \
        TURN_EXTERNAL_IP=10.0.0.133/172.28.0.8; then
        fail "bash setup with split-host inputs exited non-zero"
        sed 's/^/  setup: /' "$workspace/setup.log" >&2 || true
        return
    fi
    assert_setup_env_split_host "$workspace/.env" "$workspace/setup.log" "bash"
}

test_bash_setup_stays_backward_compatible() {
    local workspace="$TMP_ROOT/setup-bash-compat"
    populate_setup_workspace "$workspace"
    if ! run_bash_setup "$workspace" "$workspace/setup.log"; then
        fail "bash setup without advanced inputs exited non-zero"
        sed 's/^/  setup: /' "$workspace/setup.log" >&2 || true
        return
    fi
    assert_setup_env_compat "$workspace/.env" "$workspace/setup.log" "bash"
}

test_bash_setup_rejects_invalid_external_ip() {
    local workspace="$TMP_ROOT/setup-bash-invalid"
    populate_setup_workspace "$workspace"
    if run_bash_setup "$workspace" "$workspace/setup.log" \
        TURN_EXTERNAL_IP=not-an-ip; then
        fail "bash setup accepted an invalid TURN_EXTERNAL_IP"
    else
        pass "bash setup rejects an invalid TURN_EXTERNAL_IP"
    fi
}

test_powershell_setup_parity() {
    local bin script ps_root
    if ! bin="$(find_powershell)"; then
        echo "skip - PowerShell lane (no PowerShell binary found)"
        return
    fi

    # Cheapest parity gate: the script must at least parse. Content is piped
    # via stdin so no WSL<->Windows path translation is needed.
    if "$bin" -NoProfile -Command \
        '$null = [scriptblock]::Create(($input | Out-String))' \
        < "$REPO_ROOT/scripts/setup_local.ps1" >/dev/null 2>&1; then
        pass "powershell setup script parses"
    else
        fail "powershell setup script does not parse"
        return
    fi

    if ! ps_root="$(powershell_workspace_root "$bin")" || [[ ! -d "$ps_root" ]]; then
        echo "skip - PowerShell full-run lane (no usable workspace root)"
        return
    fi
    ps_root="$ps_root/dograh-turn-split-test-$$"
    PS_CLEANUP_DIR="$ps_root"
    script="$(powershell_script_path "$bin")"

    local workspace="$ps_root/split"
    populate_setup_workspace "$workspace"
    if run_powershell_setup "$bin" "$script" "$workspace" "$workspace/setup.log" \
        TURN_INTERNAL_HOST=host.docker.internal \
        TURN_EXTERNAL_IP=10.0.0.133/172.28.0.8; then
        assert_setup_env_split_host "$workspace/.env" "$workspace/setup.log" "powershell"
    else
        fail "powershell setup with split-host inputs exited non-zero"
        sed 's/^/  setup: /' "$workspace/setup.log" >&2 || true
    fi

    workspace="$ps_root/compat"
    populate_setup_workspace "$workspace"
    if run_powershell_setup "$bin" "$script" "$workspace" "$workspace/setup.log"; then
        assert_setup_env_compat "$workspace/.env" "$workspace/setup.log" "powershell"
    else
        fail "powershell setup without advanced inputs exited non-zero"
        sed 's/^/  setup: /' "$workspace/setup.log" >&2 || true
    fi

    workspace="$ps_root/invalid"
    populate_setup_workspace "$workspace"
    if run_powershell_setup "$bin" "$script" "$workspace" "$workspace/setup.log" \
        TURN_EXTERNAL_IP=not-an-ip; then
        fail "powershell setup accepted an invalid TURN_EXTERNAL_IP"
    else
        pass "powershell setup rejects an invalid TURN_EXTERNAL_IP"
    fi
}

PS_CLEANUP_DIR=""
cleanup() {
    rm -rf "$TMP_ROOT"
    [[ -n "$PS_CLEANUP_DIR" ]] && rm -rf "$PS_CLEANUP_DIR"
}
trap cleanup EXIT

test_local_render_preserves_explicit_external_ip
test_local_render_defaults_external_ip_to_turn_host
test_remote_render_stays_pinned_to_server_ip
test_bash_setup_writes_split_host_keys
test_bash_setup_stays_backward_compatible
test_bash_setup_rejects_invalid_external_ip
test_powershell_setup_parity

if [[ "$FAILURES" -gt 0 ]]; then
    echo "$FAILURES test(s) failed" >&2
    exit 1
fi
echo "all TURN split-host script tests passed"
