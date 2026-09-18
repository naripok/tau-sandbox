#!/bin/bash
set -euo pipefail

# Chromium on Linux uses the user's NSS database rather than the system CA
# bundle. Keep microsandbox's runtime TLS CA trusted there without disabling
# certificate verification. Optional arguments make the helper testable; the
# entrypoint always supplies the fixed production paths.
PATH=/usr/local/bin:/usr/bin:/bin
MSB_CA="${1:-/usr/local/share/ca-certificates/microsandbox-ca.crt}"
NSS_DB="${2:-/home/opencode/.pki/nssdb}"
NICKNAME="opencode-sandbox microsandbox CA"

# Do not create an NSS database unless there is a CA to add. If a database
# already exists, continue so a previously managed CA can be removed.
if [ ! -f "$MSB_CA" ] && [ ! -f "$NSS_DB/cert9.db" ]; then
    exit 0
fi

mkdir -p "$NSS_DB"

# Sandboxes for one project share a persistent home and may start together.
# Descriptor 9 exists only in this subshell and closes automatically on exit.
(
    flock -x 9

    if [ ! -f "$NSS_DB/cert9.db" ]; then
        certutil -N --empty-password -d "sql:$NSS_DB"
    fi
    certutil -L -d "sql:$NSS_DB" >/dev/null # validate the database

    if [ ! -f "$MSB_CA" ]; then
        if certutil -L -d "sql:$NSS_DB" -n "$NICKNAME" >/dev/null 2>&1; then
            certutil -D -d "sql:$NSS_DB" -n "$NICKNAME"
        fi
        exit 0
    fi

    source_sha="$(openssl x509 -in "$MSB_CA" -outform DER | sha256sum)"
    installed_sha=""
    installed_trust=""
    if certutil -L -d "sql:$NSS_DB" -n "$NICKNAME" >/dev/null 2>&1; then
        installed_sha="$(certutil -L -d "sql:$NSS_DB" -n "$NICKNAME" -r | sha256sum)"
        installed_trust="$(certutil -L -d "sql:$NSS_DB" | sed -n 's/^opencode-sandbox microsandbox CA  *\([^ ]*\)  *$/\1/p')"
    fi

    if [ "$installed_sha" != "$source_sha" ]; then
        if [ -n "$installed_sha" ]; then
            certutil -D -d "sql:$NSS_DB" -n "$NICKNAME"
        fi
        certutil -A -d "sql:$NSS_DB" -n "$NICKNAME" -t "C,," -i "$MSB_CA"
    elif [ "$installed_trust" != "C,," ]; then
        certutil -M -d "sql:$NSS_DB" -n "$NICKNAME" -t "C,,"
    fi
) 9>>"$NSS_DB/.opencode-sandbox-ca.lock"
