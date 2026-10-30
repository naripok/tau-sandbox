"""Account-free pinned CLI RPC probe shared by local opt-in and VM tests.

No thread, model request, or login is created. Uses config/read,
configRequirements/read and skills/list from rust-v0.166.0.
"""
import json
import os
from pathlib import Path
import select
import subprocess
import sys


def read_config(cli, home, overrides, cwd=None):
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home)}
    process = subprocess.Popen(
        [cli, *overrides, "app-server", "--stdio"], env=env, cwd=cwd,
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
        config = request(2, "config/read", {"includeLayers": True, "cwd": str(cwd or home)})
        requirements = request(3, "configRequirements/read", {})
        skills = request(4, "skills/list", {"cwds": [str(cwd or home)], "forceReload": True})
        return config, requirements, skills
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def catalog(skills):
    return [skill for entry in skills["data"] for skill in entry["skills"]]


def main(cli, home, context_file):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    context = Path(context_file).read_text()
    assert subprocess.check_output([cli, "--version"], text=True).strip() == "codex-cli 0.166.0"
    # This is the discovery path mounted read-only by run.sh in the VM.
    own_skill = home / ".agents/skills/sandbox-own-skill/SKILL.md"
    own_skill.parent.mkdir(parents=True, exist_ok=True)
    own_skill.write_text("---\nname: sandbox-own-skill\ndescription: Local discovery test\n---\nLocal test skill.\n")
    config, requirements, skills = read_config(cli, home, [])
    expected = {
        "sandbox_mode": "danger-full-access", "approval_policy": "never",
        "cli_auth_credentials_store": "file", "check_for_update_on_startup": False,
        "web_search": "disabled",
    }
    for key, value in expected.items():
        assert config["config"][key] == value, (key, config)
        assert config["origins"][key]["name"]["type"] == "system", config["origins"]
    assert requirements["requirements"]["additionalDeveloperInstructions"] == context, requirements
    effective = config["config"]
    assert effective["skills"]["bundled"]["enabled"] is False, effective
    assert effective["analytics"]["enabled"] is False
    assert effective["feedback"]["enabled"] is False
    assert effective["otel"]["metrics_exporter"] == "none"
    # Evaluate known flags, rather than just testing a TOML echo.
    output = subprocess.check_output(
        [cli, "features", "list"], text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home)},
    )
    flags = {line.split()[0]: line.split()[-1] for line in output.splitlines()}
    system_layer = next(layer for layer in config["layers"] if layer["name"]["type"] == "system")
    # ToolsV2 only projects web_search; inspect raw layers plus effective origins
    # for this supported core setting, not the lossy config.tools RPC projection.
    assert system_layer["config"]["tools"]["experimental_request_user_input"]["enabled"] is False
    assert config["origins"]["tools.experimental_request_user_input.enabled"]["name"]["type"] == "system"
    for key, value in system_layer["config"]["features"].items():
        assert effective["features"][key] == value, (key, effective)
        assert flags[key] == str(value).lower(), (key, output)
    assert any(skill["name"] == "sandbox-own-skill" and skill["enabled"] for skill in catalog(skills)), skills
    assert not any(skill["scope"] == "system" for skill in catalog(skills)), skills

    # User configuration outranks system defaults; explicit CLI outranks user.
    (home / "config.toml").write_text('sandbox_mode = "workspace-write"\napproval_policy = "on-request"\n')
    config, _, _ = read_config(cli, home, [])
    assert config["config"]["sandbox_mode"] == "workspace-write", config
    assert config["config"]["approval_policy"] == "on-request", config
    config, requirements, _ = read_config(cli, home, [
        "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
        "-c", 'developer_instructions="Caller instructions remain separate"',
    ])
    assert config["config"]["sandbox_mode"] == "read-only", config
    assert config["config"]["approval_policy"] == "never", config
    assert config["config"]["developer_instructions"] == "Caller instructions remain separate"
    assert requirements["requirements"]["additionalDeveloperInstructions"] == context

    # A trusted project can re-enable optional features and bundled skills.
    project = home / "project"
    (project / ".codex").mkdir(parents=True, exist_ok=True)
    (project / ".git").mkdir(exist_ok=True)
    (project / ".codex/config.toml").write_text(
        'web_search = "live"\n[features]\napps = true\n[skills.bundled]\nenabled = true\n'
        '[tools.experimental_request_user_input]\nenabled = true\n'
    )
    with (home / "config.toml").open("a") as user_config:
        user_config.write(f'\n[projects.{json.dumps(str(project))}]\ntrust_level = "trusted"\n')
    config, _, skills = read_config(cli, home, [], cwd=project)
    assert config["config"]["web_search"] == "live", config
    assert config["config"]["features"]["apps"] is True, config
    project_layer = next(layer for layer in config["layers"] if layer["name"]["type"] == "project")
    assert project_layer["config"]["tools"]["experimental_request_user_input"]["enabled"] is True
    assert config["origins"]["tools.experimental_request_user_input.enabled"]["name"]["type"] == "project"
    assert config["config"]["skills"]["bundled"]["enabled"] is True, config
    assert any(skill["scope"] == "system" for skill in catalog(skills)), skills
    assert any(skill["name"] == "sandbox-own-skill" for skill in catalog(skills)), skills
    assert not (home / "auth.json").exists()
    print("Codex defaults, evaluated flags, own/bundled skills, project opt-in, and managed context verified without login")


if __name__ == "__main__":
    main(*sys.argv[1:])
