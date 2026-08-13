# Project Conventions

## Commit Messages

- **Subject**: `<type>: brief description` (e.g. `spec: per-project agent isolation with microsandbox microVMs`)
- **Body**: 2-4 lines. What the commit is and what it covers. No reasoning, no details that live in the changed files.
- The message is an index for history navigation, not a summary of the diff. If someone needs context, they read the file.

## Shell Scripts

- `run.sh` and `install.sh` must be `set -euo pipefail` and produce no stdout noise unless building or reporting.
- All user-facing streams (prompts, warnings, errors) go to stdout except errors, which go to stderr.
- Scripts must be testable with a fake `msb` on `PATH` (see `tests/test_run.py`).

## Documentation Scope

- `README.md` and `config/APPEND_SYSTEM.md` describe only the current implementation. Do not document removed or legacy behavior.
- `config/APPEND_SYSTEM.md` must stay in sync with `run.sh` flags, the `Containerfile` tool list, and resource limits.
