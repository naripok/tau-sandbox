#!/bin/bash
set -euo pipefail

# Host-state verification for the tau-sandbox launcher.
#
# Checks the surfaces the eMudhra CA compromise remediation and the msb
# upgrade touch: the launcher repo, the msb image cache, TAU_IMAGE overrides,
# the host trust store, one running sandbox, and the unit test suite.
#
# Writes a report to a file (first argument; default: a timestamped
# host-state-report-*.txt next to this script, which is inside the repo, so
# the sandbox can read it later at /workspace). stdout carries only the
# report path and the verdict summary; errors go to stderr. The report never
# contains credential or secret values.
#
# Exit code: 0 when no check FAILED, 1 when at least one did. Network and
# prerequisite problems are WARN, not FAIL: they block verification, not
# security.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPORT="${1:-$SCRIPT_DIR/host-state-report-$(date +%Y%m%d-%H%M%S).txt}"
COMPROMISED_URLS=(
    "https://dv-g3b-emudhra.we-have-a-problem.com/"
    "https://dv-g3b-aaa-emudhra.we-have-a-problem.com/"
)

RESULTS=()
FAILED=0
TRY_RC=""
TRY_OUT=""

record() {
    # record MARK MESSAGE — MARK is OK, WARN, FAIL, or INFO.
    RESULTS+=("$1 $2")
    case "$1" in
        FAIL) FAILED=1 ;;
    esac
}

have() { command -v "$1" >/dev/null 2>&1; }

section() { printf '\n## %s\n' "$*" >> "$REPORT"; }

try_out() {
    # try_out LABEL CMD... — run a command, append its output and exit code
    # to the report, and export the result as TRY_RC and TRY_OUT. Never
    # aborts the script.
    local label="$1"
    shift
    printf '\n### %s\n' "$label" >> "$REPORT"
    local out rc
    if out="$("$@" 2>&1)"; then
        rc=0
    else
        rc=$?
    fi
    TRY_RC=$rc
    TRY_OUT="$out"
    printf '```\n%s\n```\n(exit %s)\n' "$out" "$rc" >> "$REPORT"
}

kvm_available() {
    [ "$(uname -s)" = "Linux" ] && [ -e /dev/kvm ] && [ -r /dev/kvm ] && [ -w /dev/kvm ]
}

# --- Report header ---

printf '# tau-sandbox host-state report\n\n- date: %s\n- host: %s %s\n- script: %s\n' \
    "$(date -Is)" "$(uname -s)" "$(uname -m)" "$SCRIPT_DIR" >> "$REPORT"

# --- Toolchain ---

section "Toolchain"
for tool in msb podman uv python3 curl node; do
    if have "$tool"; then
        record OK "$tool present ($( "$tool" --version 2>/dev/null | head -1 ))"
    else
        record WARN "$tool not found in PATH"
    fi
done
if [ "$(uname -s)" = "Linux" ]; then
    if kvm_available; then
        record OK "/dev/kvm readable and writable"
    else
        record WARN "/dev/kvm missing or not accessible; sandbox boot probe will be skipped"
    fi
fi

# --- Launcher repo ---

section "Launcher repo ($SCRIPT_DIR)"
if git -C "$SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    try_out "git log" git -C "$SCRIPT_DIR" log --oneline -3
    git_log="$(git -C "$SCRIPT_DIR" log --oneline -5)"
    if grep -q "pin patched certifi floor" <<< "$git_log"; then
        record OK "repo contains the certifi-floor commit"
    else
        record FAIL "repo is missing commit a5321b9 (pin patched certifi floor); pull or update the clone"
    fi
    try_out "git status" git -C "$SCRIPT_DIR" status --short
else
    record FAIL "$SCRIPT_DIR is not a git repository"
fi
if grep -q "certifi>=2026.10.19" "$SCRIPT_DIR/Containerfile" 2>/dev/null; then
    record OK "Containerfile pins the patched certifi floor (>=2026.10.19)"
else
    record FAIL "Containerfile does not pin certifi>=2026.10.19"
fi

# --- msb image cache ---

section "msb image cache"
if have msb; then
    try_out "msb images" bash -c 'msb images -q | grep tau-agent-isolated || true'
    msb_images="$(msb images -q 2>/dev/null)"
    if grep -qx "localhost/tau-agent-isolated:latest" <<< "$msb_images"; then
        record OK "shared base image localhost/tau-agent-isolated:latest is loaded"
    else
        record FAIL "shared base image localhost/tau-agent-isolated:latest missing; run: make build"
    fi
    # Package images embed a hash of the base inputs (Containerfile + config/).
    # Any image whose base hash differs from the current inputs predates the
    # current base and rebuilds (approval-gated) on that project's next run.
    CURRENT_BASE_HASH="$({ sha256sum "$SCRIPT_DIR/Containerfile" 2>/dev/null || true; \
        find "$SCRIPT_DIR/config" -maxdepth 1 -type f -print0 2>/dev/null \
            | LC_ALL=C sort -z | xargs -0 -r sha256sum; } \
        | awk '{ printf "%s", $1 }' | sha256sum | cut -c1-8)"
    stale_projects="$(sed -nE 's|^localhost/tau-agent-isolated-.*-([0-9a-f]{8})-[0-9a-f]{8}:latest$|\1|p' <<< "$msb_images" \
        | grep -v "^$CURRENT_BASE_HASH$" | sort -u | wc -l)"
    if [ "$stale_projects" -eq 0 ]; then
        record OK "all cached package images are built on the current base ($CURRENT_BASE_HASH)"
    else
        record INFO "base hash is now $CURRENT_BASE_HASH; $stale_projects package-image base generation(s) predate it and rebuild on those projects' next interactive run"
    fi
else
    record WARN "msb not found; cannot inspect the image cache"
fi

# --- TAU_IMAGE overrides ---

section "TAU_IMAGE overrides (user-managed images bypass rebuilds)"
# The launcher clone itself documents TAU_IMAGE everywhere; only real
# overrides in user projects and shellrcs matter.
TAU_IMAGE_HITS="$(grep -rIn --exclude-dir=.git 'TAU_IMAGE' \
    "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile" "$HOME/Projects" 2>/dev/null \
    | grep -v "^$SCRIPT_DIR/" || true)"
if [ -n "$TAU_IMAGE_HITS" ]; then
    printf '\n```\n%s\n```\n' "$TAU_IMAGE_HITS" >> "$REPORT"
    record WARN "TAU_IMAGE found; those images are user-managed and must be rebuilt manually (make build)"
else
    record OK "no TAU_IMAGE overrides found"
fi

# --- Host trust store ---

section "Host trust store (the surface tau-login-openai uses)"
if have curl; then
    for url in "${COMPROMISED_URLS[@]}"; do
        curl_rc=0
        curl -sS --max-time 20 -o /dev/null "$url" 2>>"$REPORT" || curl_rc=$?
        printf '\ncurl %s -> exit %s\n' "$url" "$curl_rc" >> "$REPORT"
        case "$curl_rc" in
            0)   record FAIL "curl ACCEPTED the compromised certificate at $url" ;;
            6|7) record WARN "curl could not reach $url (network); rejection unverified" ;;
            28)  record WARN "curl timed out on $url; rejection unverified" ;;
            *)   record OK "curl rejected $url (exit $curl_rc)" ;;
        esac
    done
else
    record WARN "curl not found; host trust store unverified"
fi
if have python3; then
    URL_LIST_PY="$(printf '"%s", ' "${COMPROMISED_URLS[@]}")"
    URL_LIST_PY="${URL_LIST_PY%, }"
    PY_PROBE="
import urllib.request
for u in (${URL_LIST_PY}):
    try:
        urllib.request.urlopen(u, timeout=20)
        print('FAIL accepted', u)
    except Exception as e:
        print('OK rejected', u, type(e).__name__)
"
    try_out "python urllib (the tau-login-openai code path)" python3 -c "$PY_PROBE"
    if [ "$TRY_RC" -ne 0 ]; then
        record WARN "python probe errored; see output above"
    elif grep -q "FAIL accepted" <<< "$TRY_OUT"; then
        record FAIL "python urllib accepted a compromised certificate"
    else
        record OK "python urllib rejected both compromised-certificate endpoints"
    fi
else
    record WARN "python3 not found; urllib check skipped"
fi

# --- Running sandbox probe ---

section "Running sandbox probe (image freshness and runtime TLS CA)"
if have msb && kvm_available; then
    GUEST_PROBE='echo "NODE: $(node --version 2>&1)"; echo "CERTIFI: $(/opt/tau/bin/python -c "import certifi; print(certifi.__version__)" 2>&1)"; echo "CA_DIR: $(ls /usr/local/share/ca-certificates/ 2>&1)"; echo "NSSDB: $(ls ~/.pki/nssdb 2>&1)"; echo "CURL: $(curl -sS --max-time 20 -o /dev/null -w "%{http_code}" https://example.com 2>&1)"'
    pushd "$SCRIPT_DIR" >/dev/null
    try_out "run.sh boot probe" ./run.sh bash -c "$GUEST_PROBE"
    popd >/dev/null
    if [ "$TRY_RC" -ne 0 ]; then
        record WARN "sandbox boot probe failed; see output above"
    else
        # msb streams guest console output with CRLF endings; strip the CR
        # so parsed probe values compare cleanly.
        probe="${TRY_OUT//$'\r'/}"
        node_version="$(sed -n 's/^NODE: \(.*\)$/\1/p' <<< "$probe" | head -1)"
        certifi_version="$(sed -n 's/^CERTIFI: \(.*\)$/\1/p' <<< "$probe" | head -1)"
        ca_dir_line="$(sed -n 's/^CA_DIR: \(.*\)$/\1/p' <<< "$probe" | head -1)"
        guest_http="$(sed -n 's/^CURL: \(.*\)$/\1/p' <<< "$probe" | head -1)"
        record INFO "guest Node.js: ${node_version:-unknown} (patched releases: 22.23.4 / 24.22.1 / 26.12.1 or later)"
        if [ -n "$certifi_version" ] \
            && [ "$certifi_version" = "$(printf '%s\n2026.10.19\n' "$certifi_version" | sort -V | tail -1)" ]; then
            record OK "guest certifi $certifi_version satisfies the 2026.10.19 floor"
        else
            record FAIL "guest certifi ($certifi_version) is missing or below 2026.10.19; rebuild the image: make build"
        fi
        if grep -q "microsandbox-ca.crt" <<< "$ca_dir_line"; then
            record OK "runtime TLS CA present; sync-browser-ca.sh will manage it"
        elif [ -z "$ca_dir_line" ] || grep -qE "cannot access|No such file" <<< "$ca_dir_line"; then
            record OK "runtime TLS CA absent; consistent with an msb upgrade that no longer MITMs TLS"
        else
            record WARN "runtime TLS CA state unclear; see probe output above"
        fi
        if [ "$guest_http" = "200" ]; then
            record OK "guest HTTPS to the internet works"
        else
            record FAIL "guest HTTPS probe returned '$guest_http'"
        fi
    fi
else
    record WARN "skipped (needs msb plus /dev/kvm)"
fi

# --- Test suite ---

section "Unit test suite (integration tests excluded: they build a test image)"
if have uv; then
    # A project venv created inside the sandbox does not work on the host
    # (guest interpreter paths), and an ambient VIRTUAL_ENV or TAU_LAN_HOSTS
    # would skew the run. Use a throwaway environment instead.
    PYTEST_HOME="$(mktemp -d)"
    try_out "pytest" env -u VIRTUAL_ENV -u TAU_LAN_HOSTS \
        UV_PROJECT_ENVIRONMENT="$PYTEST_HOME/venv" \
        uv run --directory "$SCRIPT_DIR" pytest -q --ignore="$SCRIPT_DIR/tests/test_integration.py"
    rm -rf -- "$PYTEST_HOME"
    if [ "$TRY_RC" -eq 0 ]; then
        record OK "unit test suite passed"
    else
        record FAIL "unit test suite failed; see output above"
    fi
else
    record WARN "uv not found; test suite skipped"
fi

# --- Summary ---

{
    printf '\n## Verdicts\n\n'
    for line in "${RESULTS[@]}"; do
        printf -- '- %s\n' "$line"
    done
    printf '\nEND OF REPORT\n'
} >> "$REPORT"

ok_count=0
warn_count=0
for line in "${RESULTS[@]}"; do
    case "$line" in
        OK*)   ok_count=$((ok_count + 1)) ;;
        WARN*) warn_count=$((warn_count + 1)) ;;
    esac
done

echo "Report: $REPORT"
echo "Verdicts: $ok_count ok, $warn_count warnings, $FAILED failures"
if [ "$FAILED" -eq 1 ]; then
    for line in "${RESULTS[@]}"; do
        case "$line" in
            FAIL*|WARN*) echo "  $line" ;;
        esac
    done
fi
exit "$FAILED"
