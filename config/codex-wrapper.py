#!/usr/bin/python
"""Launch the image-pinned Codex package without changing caller arguments.

Execution defaults live in the lowest-precedence system config; additive
sandbox instructions live separately in managed requirements.
"""

import os
import sys


def main(args: list[str]) -> None:
    os.execv("/usr/bin/node", [
        "node", "/opt/codex/node_modules/@openai/codex/bin/codex.js", *args,
    ])


if __name__ == "__main__":
    main(sys.argv[1:])
