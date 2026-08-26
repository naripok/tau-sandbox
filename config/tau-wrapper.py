#!/opt/tau/bin/python
"""Sandbox launcher that injects the invariant sandbox-context prompt."""

from __future__ import annotations

import sys
from pathlib import Path

from tau_coding.cli import app


if __name__ == "__main__":
    sandbox_prompt = Path("/etc/tau-sandbox/APPEND_SYSTEM.md")
    sys.argv[0] = sys.argv[0].removesuffix(".exe")
    sys.argv[1:1] = ["--append-system-prompt", str(sandbox_prompt)]
    sys.exit(app())
