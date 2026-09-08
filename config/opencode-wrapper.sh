#!/bin/bash
set -euo pipefail

# opencode sandbox wrapper: merges the immutable sandbox config on every
# launch. /etc/opencode-sandbox/opencode.json carries the sandbox context
# document as an instructions entry, and opencode concatenates instructions
# arrays, so global and project config can add instructions but can never
# shadow the sandbox reference. run.sh mounts both files read-only from the
# repository; the image also contains a fallback copy at the same paths.
export OPENCODE_CONFIG=/etc/opencode-sandbox/opencode.json
export OPENCODE_DISABLE_AUTOUPDATE=true
exec /opt/opencode/bin/opencode "$@"
