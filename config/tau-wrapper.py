#!/opt/tau/bin/python
"""Sandbox launcher that adds invariant context and supports credential file mounts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from tau_coding import credentials


_ORIGINAL_SAVE = credentials.FileCredentialStore._save


def _save_credentials(self: credentials.FileCredentialStore, data: dict[str, Any]) -> None:
    """Write shared credentials in place because a mounted file cannot be replaced."""
    shared_path = credentials.credentials_path()
    if os.environ.get("TAU_SANDBOX_SHARED_CREDENTIALS") != "1" or self.path != shared_path:
        _ORIGINAL_SAVE(self, data)
        return

    raw = {key: credentials._credential_to_json(value) for key, value in data.items()}
    content = json.dumps(raw, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(self.path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


credentials.FileCredentialStore._save = _save_credentials

from tau_coding.cli import app  # noqa: E402


if __name__ == "__main__":
    sandbox_prompt = Path("/etc/tau-sandbox/APPEND_SYSTEM.md")
    sys.argv[0] = sys.argv[0].removesuffix(".exe")
    sys.argv[1:1] = ["--append-system-prompt", str(sandbox_prompt)]
    sys.exit(app())
