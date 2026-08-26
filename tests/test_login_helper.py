"""Unit tests for lib/tau-login-openai.

These tests prove the host helper derives the same per-project volume as
run.sh, isolates projects from each other, writes the exact credential
document guest Tau reads, validates pasted redirect state, targets the
SDK write at the project volume and the credential path, and never puts
token values on stdout or stderr. No browser, network, or microsandbox
runtime is required: the SDK call and the token exchange are stubbed.
"""
import base64
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from types import SimpleNamespace

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


class RecordingFs:
    """A VolumeFs stand-in recording writes and reporting existing paths."""

    def __init__(self, written, volume, seed=None):
        self.written = written
        self.volume = volume
        self._store = dict(seed or {})

    def exists(self, path):
        return True

    def read(self, path):
        return self._store.get(path, b"{}")

    def mkdir(self, path):
        raise AssertionError(f"mkdir on existing dirs-only fake: {path}")

    def write(self, path, data):
        self.written.append((path, data))
        self._store[path] = data


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


# --- Credential write targets the project volume ---


def test_write_step_creates_missing_volume_then_writes(helper, monkeypatch, tmp_path):
    """A missing volume is created through the SDK first; the document is
    written to /home/tau/.tau/credentials.json with parent dirs ensured."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    created = []
    written = []
    made_dirs = []

    class VolumeNotFoundError(Exception):
        pass

    class FakeFs:
        def exists(self, path):
            return False

        def mkdir(self, path):
            made_dirs.append(path)

        def write(self, path, data):
            written.append((path, data))

    class FakeVolume:
        @staticmethod
        async def get(name):
            raise VolumeNotFoundError(name)

        @staticmethod
        async def create(name, **kwargs):
            created.append(name)
            return SimpleNamespace(fs=FakeFs())

    monkeypatch.setitem(
        sys.modules,
        "microsandbox",
        SimpleNamespace(Volume=FakeVolume, VolumeNotFoundError=VolumeNotFoundError),
    )
    helper.write_credential(volume, "{}\n")

    assert created == [volume]
    assert made_dirs == ["/home", "/home/tau", "/home/tau/.tau"]
    assert written == [("/home/tau/.tau/credentials.json", b"{}\n")]


def test_write_step_reuses_existing_volume(helper, monkeypatch, tmp_path):
    """An existing project volume is used as-is: no create call."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    written = []

    class FakeVolume:
        @staticmethod
        async def get(name):
            if name != volume:
                raise AssertionError(f"expected {volume}, got {name}")
            return SimpleNamespace(fs=RecordingFs(written, volume))

        @staticmethod
        async def create(name, **kwargs):
            raise AssertionError("existing volume must not be created")

    monkeypatch.setitem(
        sys.modules,
        "microsandbox",
        SimpleNamespace(Volume=FakeVolume, VolumeNotFoundError=RuntimeError),
    )
    content = helper.credential_json("access-token", "refresh-token", 1_758_908_400_123, "acct_1")
    helper.write_credential(volume, content)

    assert written == [("/home/tau/.tau/credentials.json", content.encode("utf-8"))]


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
    written = []
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

    class FakeVolume:
        @staticmethod
        async def get(name):
            seed = {"/home/tau/.tau/credentials.json": json.dumps(stored).encode("utf-8")}
            fs = RecordingFs(written, volume, seed=seed)
            return SimpleNamespace(fs=fs)

        @staticmethod
        async def create(name, **kwargs):
            raise AssertionError("existing volume must not be created")

    monkeypatch.setitem(
        sys.modules,
        "microsandbox",
        SimpleNamespace(Volume=FakeVolume, VolumeNotFoundError=RuntimeError),
    )
    helper.write_credential(
        volume, helper.credential_json("new-access", "new-refresh", 2, "new-acct")
    )

    assert written == [
        (
            "/home/tau/.tau/credentials.json",
            (
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
            ).encode("utf-8"),
        )
    ]


def test_write_without_existing_file_produces_single_entry_document(helper, monkeypatch, tmp_path):
    """A missing credentials.json yields exactly the new single-entry
    document; no read happens and no other entry appears."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    written = []
    reads = []

    class FakeFs:
        def exists(self, path):
            return False

        def mkdir(self, path):
            pass

        def read(self, path):
            reads.append(path)
            raise AssertionError("read must not be called for a missing file")

        def write(self, path, data):
            written.append((path, data))

    class FakeVolume:
        @staticmethod
        async def get(name):
            return SimpleNamespace(fs=FakeFs())

        @staticmethod
        async def create(name, **kwargs):
            raise AssertionError("existing volume must not be created")

    monkeypatch.setitem(
        sys.modules,
        "microsandbox",
        SimpleNamespace(Volume=FakeVolume, VolumeNotFoundError=RuntimeError),
    )
    new_doc = helper.credential_json("new-access", "new-refresh", 1_758_908_400_123, "new-acct")
    helper.write_credential(volume, new_doc)

    assert reads == []
    assert written == [("/home/tau/.tau/credentials.json", new_doc.encode("utf-8"))]
    assert set(json.loads(written[0][1])) == {"openai-codex"}


def test_write_corrupt_credentials_file_fails_without_writing(helper, monkeypatch, tmp_path):
    """A stored document that is not a JSON object raises a clear error
    and writes nothing, so a corrupt file is never destroyed."""
    project = tmp_path / "proj"
    project.mkdir()
    volume = helper.volume_name_for(str(project))
    new_doc = helper.credential_json("new-access", "new-refresh", 2, "new-acct")

    for corrupt in (b"{not json", b"[1, 2, 3]"):
        written = []

        class FakeFs:
            def exists(self, path):
                return True

            def read(self, path):
                return corrupt

            def write(self, path, data):
                written.append((path, data))

        class FakeVolume:
            @staticmethod
            async def get(name):
                return SimpleNamespace(fs=FakeFs())

            @staticmethod
            async def create(name, **kwargs):
                raise AssertionError("existing volume must not be created")

        monkeypatch.setitem(
            sys.modules,
            "microsandbox",
            SimpleNamespace(Volume=FakeVolume, VolumeNotFoundError=RuntimeError),
        )
        with pytest.raises(helper.OAuthError, match="refusing to overwrite"):
            helper.write_credential(volume, new_doc)
        assert written == []


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
    monkeypatch.setattr(helper, "_project_volume_fs", lambda name: RecordingFs(written, name))
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
