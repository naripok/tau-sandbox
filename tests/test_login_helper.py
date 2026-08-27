"""Unit tests for lib/tau-login-openai.

These tests prove the host helper derives the same per-project volume as
run.sh, isolates projects from each other, writes the exact credential
document guest Tau reads, validates pasted redirect state, targets the
credential write at the concrete host directory the msb CLI reports, and
never puts token values on stdout or stderr. No browser, network, or
microsandbox runtime is required: a fake ``msb`` executable on PATH backs
``volume inspect`` and ``volume create`` (as conftest does for run.sh),
and the token exchange is stubbed.
"""
import base64
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
from importlib.machinery import SourceFileLoader

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
HELPER = REPO_ROOT / "lib" / "tau-login-openai"

def _run_sh_derivation_script() -> str:
    """Assemble a bash snippet from run.sh's own volume-derivation lines.

    Copies run.sh text verbatim from the PROJECT_PATH assignment through
    the PERSIST_VOLUME assignment and then prints PERSIST_VOLUME, so the
    comparison runs the real run.sh derivation. If run.sh drifts, the
    markers or the comparison fail loudly at test time.
    """
    text = (REPO_ROOT / "run.sh").read_text(encoding="utf-8")
    start_marker = 'PROJECT_PATH="$(realpath "$(pwd)")' + '"'
    end_marker = 'PERSIST_VOLUME="tau-persist-${PROJECT_NAME}-${PROJECT_HASH}"'
    try:
        start = text.index(start_marker)
        end = text.index("\n", text.index(end_marker)) + 1
    except ValueError as error:
        raise AssertionError(
            "run.sh no longer contains the volume-derivation fragment "
            f"({error}); update the extraction markers in this test"
        ) from error
    return text[start:end] + "\nprintf '%s' \"$PERSIST_VOLUME\"\n"

ACCESS_JWT_PAYLOAD = {
    "exp": 1893456000,
    "https://api.openai.com/auth": {"chatgpt_account_id": "acct_42"},
}


def _jwt(payload=None):
    """Build a three-part JWT whose payload matches a real access token."""
    def enc(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    body = json.dumps(payload if payload is not None else ACCESS_JWT_PAYLOAD)
    return f"{enc(b'{}')}.{enc(body.encode('utf-8'))}.{enc(b'signature')}"


FAKE_MSB = """#!/bin/bash
# Fake msb CLI for tau-login-openai tests: volume inspect/create backed
# by a real temp root of named volume directories.
#   MSB_VOLUME_ROOT          directory holding the fake volumes
#   MSB_VOLUME_LOG           path that records every volume create call
#   MSB_VOLUME_CREATE_STATUS exit code for volume create (default 0)
#   MSB_VOLUME_INSPECT_NOPATH=1 omits the Path: line from inspect output
root="${MSB_VOLUME_ROOT:?}"
log="${MSB_VOLUME_LOG:-/dev/null}"
case "${1:-}" in
    volume)
        case "${2:-}" in
            inspect)
                name="${3:-}"
                if [ -d "$root/$name" ]; then
                    if [ "${MSB_VOLUME_INSPECT_NOPATH:-0}" = "1" ]; then
                        printf 'Name: %s\\nKind: dir\\n' "$name"
                    else
                        printf 'Name: %s\\nKind: dir\\nPath: %s\\n' \\
                            "$name" "$root/$name"
                    fi
                    exit 0
                fi
                echo "error: volume not found: $name" >&2
                exit 1
                ;;
            create)
                name="${3:-}"
                echo "create:$name" >> "$log"
                if [ "${MSB_VOLUME_CREATE_STATUS:-0}" != "0" ]; then
                    echo "error: volume create failed: $name" >&2
                    exit "$MSB_VOLUME_CREATE_STATUS"
                fi
                mkdir -p "$root/$name"
                exit 0
                ;;
        esac
        ;;
esac
exit 0
"""


def _install_fake_msb(monkeypatch, tmp_path, volume_root):
    """Put a fake msb CLI on PATH backed by a real temp volume root.

    Returns the log Path that records ``msb volume create`` calls.
    Mirrors the fake-binaries-on-PATH pattern conftest and test_run use.
    """
    volume_root.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    msb = fake_bin / "msb"
    msb.write_text(FAKE_MSB)
    msb.chmod(0o755)
    log = tmp_path / "msb-volume.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MSB_VOLUME_ROOT", str(volume_root))
    monkeypatch.setenv("MSB_VOLUME_LOG", str(log))
    return log


@pytest.fixture
def helper():
    """Import lib/tau-login-openai fresh per test, without running its CLI."""
    loader = SourceFileLoader("tau_login_openai", str(HELPER))
    spec = importlib.util.spec_from_loader("tau_login_openai", loader)
    module = importlib.util.module_from_spec(spec)
    # Slotted dataclasses resolve their module's __dict__ through sys.modules.
    sys.modules["tau_login_openai"] = module
    try:
        loader.exec_module(module)
    finally:
        del sys.modules["tau_login_openai"]
    return module


# --- Volume-name derivation mirrors run.sh ---


def test_volume_names_match_run_sh_derivation(tmp_path, helper):
    """The helper derives the identical volume name run.sh derives, for
    plain legal names and names that need sanitization."""
    script = _run_sh_derivation_script()
    projects = [
        tmp_path / "plain-project",
        tmp_path / "dotted.project-name_v2",
        tmp_path / "My Project (v2)!!!",
        tmp_path / "UPPER",
        tmp_path / ("long" * 60),
    ]
    for project in projects:
        project.mkdir()
        result = subprocess.run(
            ["bash", "-c", script, "derive"],
            cwd=project,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert helper.volume_name_for(str(project)) == result.stdout


def test_distinct_projects_get_distinct_volume_names(tmp_path, helper):
    """Two different project paths never share a credential volume."""
    first = tmp_path / "alpha"
    second = tmp_path / "beta"
    first.mkdir()
    second.mkdir()
    assert helper.volume_name_for(str(first)) != helper.volume_name_for(str(second))


# --- Credential document shape matches guest Tau ---


def test_credential_json_matches_guest_tau_shape(helper):
    """The document parses to the exact provider object guest Tau reads:
    type/access/refresh/expires(int)/account_id, indent 2, sorted keys,
    trailing newline."""
    doc = helper.credential_json(
        access="access-token-value",
        refresh="refresh-token-value",
        expires=1_758_908_400_123,
        account_id="acct_123",
    )
    expected = (
        "{\n"
        '  "openai-codex": {\n'
        '    "access": "access-token-value",\n'
        '    "account_id": "acct_123",\n'
        '    "expires": 1758908400123,\n'
        '    "refresh": "refresh-token-value",\n'
        '    "type": "oauth"\n'
        "  }\n"
        "}\n"
    )
    assert doc == expected
    data = json.loads(doc)
    assert set(data) == {"openai-codex"}
    credential = data["openai-codex"]
    assert credential["type"] == "oauth"
    assert credential["access"] == "access-token-value"
    assert credential["refresh"] == "refresh-token-value"
    assert isinstance(credential["expires"], int)
    assert credential["expires"] == 1_758_908_400_123
    assert credential["account_id"] == "acct_123"


# --- Paste validation ---


def test_paste_validates_state_and_rejects_mismatch(helper):
    """The paste path accepts a redirect carrying the flow's own state and
    rejects a redirect carrying any other state before any exchange."""
    flow = helper.create_openai_codex_authorization_flow()
    redirect = f"http://localhost:1455/auth/callback?code=abc123&state={flow.state}"
    assert helper.code_from_paste(redirect, flow.state) == "abc123"

    tampered = f"http://localhost:1455/auth/callback?code=abc123&state=not-the-state"
    with pytest.raises(helper.OAuthError, match="state mismatch"):
        helper.code_from_paste(tampered, flow.state)


def test_parse_authorization_input_accepts_redirect_forms(helper):
    """Pasted input parsing matches the fork: full URL, bare query,
    code#state, raw code, and empty input."""
    assert helper.parse_authorization_input(
        "http://localhost:1455/auth/callback?code=c&state=s"
    ) == helper.AuthorizationCode(code="c", state="s")
    assert helper.parse_authorization_input("code=c&state=s") == helper.AuthorizationCode(
        code="c", state="s"
    )
    assert helper.parse_authorization_input("c#s") == helper.AuthorizationCode(
        code="c", state="s"
    )
    assert helper.parse_authorization_input("rawcode") == helper.AuthorizationCode(
        code="rawcode"
    )
    assert helper.parse_authorization_input("   ") == helper.AuthorizationCode()


# --- Credential write targets the project volume's host directory ---


def _volume_credential_file(volume_root, volume_name):
    """The host path the helper writes inside the named volume."""
    return volume_root / volume_name / "home" / "tau" / ".tau" / "credentials.json"


def test_volume_host_path_creates_missing_volume_and_resolves(helper, monkeypatch, tmp_path):
    """volume_host_path creates a missing volume through msb and parses
    the Path: line into the volume's host directory."""
    volume = "tau-persist-proj-12345678"
    volume_root = tmp_path / "volumes"
    create_log = _install_fake_msb(monkeypatch, tmp_path, volume_root)

    host_path = helper.volume_host_path(volume)

    assert host_path == volume_root / volume
    assert create_log.read_text(encoding="utf-8") == f"create:{volume}\n"


def test_write_credential_creates_missing_volume_and_writes_nested_file(
    helper, monkeypatch, tmp_path
):
    """A missing volume is created through msb first; the document lands
    at <host_path>/home/tau/.tau/credentials.json with the full directory
    chain created inside the volume."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    volume_root = tmp_path / "volumes"
    create_log = _install_fake_msb(monkeypatch, tmp_path, volume_root)
    content = helper.credential_json(
        "access-token", "refresh-token", 1_758_908_400_123, "acct_1"
    )

    helper.write_credential(volume, content)

    credential_file = _volume_credential_file(volume_root, volume)
    assert credential_file.read_text(encoding="utf-8") == content
    assert create_log.read_text(encoding="utf-8") == f"create:{volume}\n"


def test_write_credential_reuses_existing_volume_without_create(
    helper, monkeypatch, tmp_path
):
    """An existing volume is used as-is: msb volume create never runs
    (inspect-first behavior)."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    volume_root = tmp_path / "volumes"
    create_log = _install_fake_msb(monkeypatch, tmp_path, volume_root)
    (volume_root / volume).mkdir(parents=True)
    content = helper.credential_json(
        "access-token", "refresh-token", 1_758_908_400_123, "acct_1"
    )

    helper.write_credential(volume, content)

    assert create_log.read_text(encoding="utf-8") == ""
    credential_file = _volume_credential_file(volume_root, volume)
    assert credential_file.read_text(encoding="utf-8") == content


def test_write_credential_writes_mode_0600(helper, monkeypatch, tmp_path):
    """The written credential file is chmod 0600: readable or writable
    only by the owner."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    volume_root = tmp_path / "volumes"
    _install_fake_msb(monkeypatch, tmp_path, volume_root)

    helper.write_credential(
        volume, helper.credential_json("access", "refresh", 1_758_908_400_123, "acct_1")
    )

    credential_file = _volume_credential_file(volume_root, volume)
    assert credential_file.stat().st_mode & 0o777 == 0o600


def test_write_credential_without_msb_raises_clear_error(helper, monkeypatch, tmp_path):
    """A missing msb binary raises an error that names the fix; nothing
    is written."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))

    with pytest.raises(helper.OAuthError, match="install the microsandbox CLI"):
        helper.write_credential(volume, "{}\n")


def test_write_credential_inspect_without_path_line_raises(helper, monkeypatch, tmp_path):
    """Inspect output without a parseable Path: line raises a clear error
    instead of guessing a default host path."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    _install_fake_msb(monkeypatch, tmp_path, tmp_path / "volumes")
    monkeypatch.setenv("MSB_VOLUME_INSPECT_NOPATH", "1")

    with pytest.raises(helper.OAuthError, match="no host path"):
        helper.write_credential(volume, "{}\n")


def test_write_credential_create_failure_raises(helper, monkeypatch, tmp_path):
    """A nonzero msb volume create exit raises a clear error."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    _install_fake_msb(monkeypatch, tmp_path, tmp_path / "volumes")
    monkeypatch.setenv("MSB_VOLUME_CREATE_STATUS", "1")

    with pytest.raises(helper.OAuthError, match="volume create"):
        helper.write_credential(volume, "{}\n")


def test_volume_name_regex_requires_true_end_of_name(helper):
    """The legality regex anchors at the true end of the name: Python's
    $ matches before a trailing newline, bash's =~ does not, so the
    helper uses \\Z to stay in agreement with run.sh."""
    assert helper._VOLUME_NAME_RE.match("plain-name") is not None
    assert helper._VOLUME_NAME_RE.match("plain-name\n") is None


def test_write_merges_into_existing_credentials(helper, monkeypatch, tmp_path):
    """A stored document keeps every non-openai-codex entry; the
    openai-codex entry is replaced and the merged document is serialized
    byte-identically to the fork's FileCredentialStore._save."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    volume_root = tmp_path / "volumes"
    _install_fake_msb(monkeypatch, tmp_path, volume_root)
    credential_file = _volume_credential_file(volume_root, volume)
    credential_file.parent.mkdir(parents=True)
    stored = {
        "anthropic-key": "sk-ant-42",
        "openai-codex": {
            "type": "oauth",
            "access": "old-access",
            "refresh": "old-refresh",
            "expires": 1,
            "account_id": "old-acct",
        },
    }
    credential_file.write_text(json.dumps(stored), encoding="utf-8")

    helper.write_credential(
        volume, helper.credential_json("new-access", "new-refresh", 2, "new-acct")
    )

    expected = (
        "{\n"
        '  "anthropic-key": "sk-ant-42",\n'
        '  "openai-codex": {\n'
        '    "access": "new-access",\n'
        '    "account_id": "new-acct",\n'
        '    "expires": 2,\n'
        '    "refresh": "new-refresh",\n'
        '    "type": "oauth"\n'
        "  }\n"
        "}\n"
    )
    assert credential_file.read_text(encoding="utf-8") == expected


def test_write_without_existing_file_produces_single_entry_document(
    helper, monkeypatch, tmp_path
):
    """A missing credentials.json yields exactly the new single-entry
    document; no other entry appears."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    volume_root = tmp_path / "volumes"
    _install_fake_msb(monkeypatch, tmp_path, volume_root)
    (volume_root / volume).mkdir(parents=True)
    new_doc = helper.credential_json(
        "new-access", "new-refresh", 1_758_908_400_123, "new-acct"
    )

    helper.write_credential(volume, new_doc)

    credential_file = _volume_credential_file(volume_root, volume)
    assert credential_file.read_text(encoding="utf-8") == new_doc
    assert set(json.loads(credential_file.read_text(encoding="utf-8"))) == {"openai-codex"}


def test_write_corrupt_credentials_file_fails_without_writing(
    helper, monkeypatch, tmp_path
):
    """A stored document that is not a JSON object raises a clear error
    and leaves the stored file untouched, so a corrupt file is never
    destroyed."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    volume_root = tmp_path / "volumes"
    _install_fake_msb(monkeypatch, tmp_path, volume_root)
    new_doc = helper.credential_json("new-access", "new-refresh", 2, "new-acct")

    for corrupt in (b"{not json", b"[1, 2, 3]", b"\xff\xfe{not json"):
        credential_file = _volume_credential_file(volume_root, volume)
        credential_file.parent.mkdir(parents=True, exist_ok=True)
        credential_file.write_bytes(corrupt)

        with pytest.raises(helper.OAuthError, match="refusing to overwrite"):
            helper.write_credential(volume, new_doc)

        assert credential_file.read_bytes() == corrupt


# --- End-to-end output never contains tokens ---


def _stub_login(helper, monkeypatch, tmp_path, written, server):
    """Stub every external step of run_login; return the flow in use."""
    project = tmp_path / "proj"
    project.mkdir()
    flow = helper.create_openai_codex_authorization_flow()
    monkeypatch.setattr(helper, "create_openai_codex_authorization_flow", lambda: flow)
    monkeypatch.setattr(helper, "start_callback_server", lambda state: server)
    monkeypatch.setattr(
        helper,
        "exchange_openai_codex_authorization_code",
        lambda code, verifier: (_jwt(), "refresh-token-xyz", 1_758_908_400_123),
    )

    def fake_write_credential(_volume_name, content):
        written.append(("/home/tau/.tau/credentials.json", content.encode("utf-8")))

    monkeypatch.setattr(helper, "write_credential", fake_write_credential)
    return project, flow


def test_browser_login_output_contains_no_tokens(helper, monkeypatch, tmp_path, capsys):
    """Browser path: script output carries no code or token values, while
    the written document holds the real tokens."""
    written = []

    class FakeServer:
        def __init__(self):
            self.closed = False

        def wait_for_code(self):
            return "auth-code-from-callback"

        def close(self):
            self.closed = True

    server = FakeServer()
    project, _flow = _stub_login(helper, monkeypatch, tmp_path, written, server)
    monkeypatch.setattr(helper, "open_browser", lambda url: True)

    assert helper.run_login(str(project)) is None
    out = capsys.readouterr()
    for secret in ("auth-code-from-callback", "refresh-token-xyz", "acct_42"):
        assert secret not in out.out
        assert secret not in out.err

    assert server.closed
    assert written[-1][0] == "/home/tau/.tau/credentials.json"
    credential = json.loads(written[-1][1])["openai-codex"]
    assert credential["refresh"] == "refresh-token-xyz"
    assert credential["account_id"] == "acct_42"


def test_paste_login_output_contains_no_tokens(helper, monkeypatch, tmp_path, capsys):
    """Headless path: the URL is printed, the pasted redirect is consumed
    from stdin, and neither the code nor the tokens reach stdout/stderr."""
    written = []
    project, flow = _stub_login(helper, monkeypatch, tmp_path, written, None)
    redirect = f"http://localhost:1455/auth/callback?code=pasted-code&state={flow.state}"
    monkeypatch.setattr(sys, "stdin", io.StringIO(redirect + "\n"))

    assert helper.run_login(str(project)) is None
    out = capsys.readouterr()
    assert flow.url in out.out
    assert "Paste the redirect URL" in out.out
    for secret in ("pasted-code", "refresh-token-xyz", "acct_42"):
        assert secret not in out.out
        assert secret not in out.err

    assert written[-1][0] == "/home/tau/.tau/credentials.json"
    credential = json.loads(written[-1][1])["openai-codex"]
    assert credential["refresh"] == "refresh-token-xyz"


def test_paste_login_rejects_mismatched_state(helper, monkeypatch, tmp_path, capsys):
    """Headless path with a tampered redirect: the run fails without
    exchanging or writing anything."""
    written = []
    project, flow = _stub_login(helper, monkeypatch, tmp_path, written, None)
    tampered = f"http://localhost:1455/auth/callback?code=pasted-code&state=wrong"
    monkeypatch.setattr(sys, "stdin", io.StringIO(tampered + "\n"))

    with pytest.raises(helper.OAuthError, match="state mismatch"):
        helper.run_login(str(project))
    assert written == []


# --- Exchange and account extraction ---


def test_exchange_and_account_returns_tuple_with_account_id(helper, monkeypatch):
    """The full exchange path yields (access, refresh, expires_ms,
    account_id) with the account id read from the access JWT."""
    jwt = _jwt({"exp": 1893456000, "https://api.openai.com/auth": {"chatgpt_account_id": "acct_789"}})
    monkeypatch.setattr(
        helper,
        "exchange_openai_codex_authorization_code",
        lambda code, verifier: (jwt, "refresh-token", 1_758_908_400_123),
    )
    assert helper.exchange_and_account("code", "verifier") == (
        jwt,
        "refresh-token",
        1_758_908_400_123,
        "acct_789",
    )
    assert helper.account_id_from_access_token("not-a-jwt") is None


# --- CLI failure contract ---


def test_failure_exits_nonzero_with_stderr_message(helper, monkeypatch, capsys):
    """A failed login exits non-zero with the error on stderr."""
    def boom(path):
        raise helper.OAuthError("authorization rejected")

    monkeypatch.setattr(helper, "run_login", boom)
    assert helper.main(["tau-login-openai", "/some/project"]) == 1
    assert "authorization rejected" in capsys.readouterr().err


def test_usage_error_exits_nonzero_with_stderr_message(helper, capsys):
    """Missing the project argument exits non-zero and explains usage."""
    assert helper.main(["tau-login-openai"]) == 2
    assert "Usage" in capsys.readouterr().err
