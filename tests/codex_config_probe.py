"""Account-free pinned CLI probe, shared by local opt-in and VM tests.

Protocol verified against rust-v0.166.0 app-server-protocol/schema/json:
InitializeParams, v2/ConfigReadParams, ConfigReadResponse and
ConfigRequirementsReadResponse. config/read returns effective merged TOML;
configRequirements/read exposes independently loaded managed requirements.
No thread, model request, or login is created.
"""
import json
import os
from pathlib import Path
import select
import subprocess
import sys


def read_config(cli, home, overrides):
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home)}
    process = subprocess.Popen(
        [cli, *overrides, "app-server", "--stdio"], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    def request(identifier, method, params):
        process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}).encode() + b"\n")
        process.stdin.flush()
        while True:
            if not select.select([process.stdout], [], [], 20)[0]:
                raise AssertionError(f"RPC timed out: {method}")
            line = process.stdout.readline()
            assert line, f"app-server exited during {method}"
            response = json.loads(line)
            if response.get("id") == identifier:
                assert "error" not in response, response
                return response["result"]

    try:
        request(1, "initialize", {
            "clientInfo": {"name": "sandbox_config_test", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        config = request(2, "config/read", {"includeLayers": True})
        requirements = request(3, "configRequirements/read", {})
        return config, requirements
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main(cli, home, context_file):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    context = Path(context_file).read_text()
    assert subprocess.check_output([cli, "--version"], text=True).strip() == "codex-cli 0.166.0"
    config, requirements = read_config(cli, home, [])
    expected = {
        "sandbox_mode": "danger-full-access", "approval_policy": "never",
        "cli_auth_credentials_store": "file", "check_for_update_on_startup": False,
    }
    for key, value in expected.items():
        assert config["config"][key] == value, (key, config)
        assert config["origins"][key]["name"]["type"] == "system", config["origins"]
    assert requirements["requirements"]["additionalDeveloperInstructions"] == context, requirements

    # User configuration outranks system defaults; explicit CLI outranks user.
    (home / "config.toml").write_text('sandbox_mode = "workspace-write"\napproval_policy = "on-request"\n')
    config, _ = read_config(cli, home, [])
    assert config["config"]["sandbox_mode"] == "workspace-write", config
    assert config["config"]["approval_policy"] == "on-request", config
    config, requirements = read_config(cli, home, [
        "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
        "-c", 'developer_instructions="Caller instructions remain separate"',
    ])
    assert config["config"]["sandbox_mode"] == "read-only", config
    assert config["config"]["approval_policy"] == "never", config
    assert config["config"]["developer_instructions"] == "Caller instructions remain separate"
    assert requirements["requirements"]["additionalDeveloperInstructions"] == context
    assert not (home / "auth.json").exists()
    print("Codex effective config, precedence, and managed context verified without login")


if __name__ == "__main__":
    main(*sys.argv[1:])
