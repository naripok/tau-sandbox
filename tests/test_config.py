"""Unit tests for config/ files: .bashrc, APPEND_SYSTEM.md, entrypoint.sh.

These tests prove the sandbox configuration files exist and describe or
implement persistent install paths, isolated state, host-config
bootstrapping, and invariant environment-reference injection.

The entrypoint namespace guard parses config/entrypoint.sh into a real
Bash AST (tree-sitter) and enforces a bounded dialect rather than
chasing every Bash writer form: every variable the entrypoint creates,
declares, loops over, or unsets must use the reserved TAU_ENTRYPOINT_
prefix (enumerated guest names are allowed only as `export NAME=...`
assignments), the only permitted `read` is the exact manifest streaming
loop, `{NAME}` descriptor allocations must use the prefix too, and
everything else that writes — writer builtins, Bash reserved words in
command position, dynamic command words, arithmetic, namerefs, unprefixed
or dynamic brace words abutting a redirect operator — fails the whole file
closed. That the allowed dialect actually boots
the sandbox is verified end-to-end by the integration suite; this guard
only proves the entrypoint stays inside the dialect.
"""
import os
import pathlib
import re
import shlex
import subprocess

import pytest
import tree_sitter_bash
from tree_sitter import Language, Parser

REPO_ROOT = pathlib.Path(__file__).parent.parent
CONFIG_DIR = REPO_ROOT / "config"

GUEST_EXPORT_VARIABLES = frozenset(
    """HOME SHELL TERM COLORTERM USER LOGNAME PATH PYTHONUSERBASE
    NPM_CONFIG_PREFIX PIP_USER TAU_NO_UPDATE_CHECK""".split()
)

ENTRYPOINT_PREFIX = "TAU_ENTRYPOINT_"


def _read(name: str) -> str:
    return (CONFIG_DIR / name).read_text()


# --- entrypoint bounded-dialect guard (tree-sitter) ---
#
# One small walk over the grammar-resolved tree; anything the walk cannot
# statically name fails the whole file closed instead of passing silently.

_SYNTAX_ERROR = "<syntax error>"
_RESERVED_WORD = "<reserved word>"
_BLOCKED_WRITER = "<blocked writer>"
_DYNAMIC_COMMAND = "<dynamic command>"

# A command name that is a Bash reserved word only arises from constructs
# outside the dialect — a `time`/`coproc` command word, or a compound
# operand of `!` whose `{`/`}` surface as command names — so it fails
# closed; a negated command with a simple-command operand stays clean.
_RESERVED_WORDS = frozenset(
    """! { } if then elif else fi for while until do done case esac in
    function select time coproc [[ ]]""".split()
)

# Writer builtins outside the allowed dialect. `read` is permitted only in
# the exact manifest loop; declarations, `unset`, `getopts`, `shopt`, and
# writer-free `printf` have their own dispatch below. Documented
# allowances outside this set: `exec` is no shell-variable writer —
# `exec env NAME=v cmd` sets a child-process environment, outside the
# boundary — and `getopts`' implicit OPTIND/OPTARG and `dirs -c`'s DIRSTACK
# writes go to spec-reserved Bash state names that no guest secret can
# collide with.
_BLOCKED_WRITERS = frozenset(
    """read mapfile readarray let eval source . trap cd pushd popd alias
    enable bind fc wait time coproc select""".split()
)

_DECLARATION_KEYWORDS = frozenset({"export", "local", "declare", "readonly", "typeset"})
_REDIRECT_TYPES = frozenset({"file_redirect", "heredoc_redirect", "herestring_redirect"})
_MANIFEST_TARGET_RE = re.compile(r"^TAU_ENTRYPOINT_[A-Za-z0-9_]+$")
# One statically readable target: NAME, NAME=value, or subscripted NAME.
_TARGET_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])*(?:=.*)?$")
_UNSET_OPTION_RE = re.compile(r"-[fnv]+")
_OPTION_WORD_RE = re.compile(r"-[A-Za-z]+")


def _node_text(node) -> str:
    return node.text.decode()


def _static_word_text(node) -> str | None:
    """Static text of a word-shaped node after quote removal, or None when
    the spelling resolves at runtime ($ expansion, command substitution, or
    an escaped word) so no builtin or target name can be proven."""
    if node is None:
        return None
    text = _node_text(node)
    ntype = node.type
    if ntype == "word":  # unquoted backslash/backtick: resolved at runtime
        return None if any(ch in text for ch in "$\\`") else text
    if ntype in ("variable_name", "number"):
        return text
    if ntype == "raw_string":
        return text[1:-1] if len(text) >= 2 else None
    if ntype == "string":
        inner = text[1:-1] if len(text) >= 2 else None
        return None if (inner is None or "$" in inner or "`" in inner) else inner
    if ntype == "concatenation":
        parts = [_static_word_text(c) for c in node.named_children]
        return None if None in parts else "".join(parts)
    if ntype == "command_name":
        kids = node.named_children
        return _static_word_text(kids[0]) if len(kids) == 1 else None
    return None


def _assignment_name(node) -> str:
    """Created name of a variable_assignment, unwrapping array subscripts."""
    name = node.child_by_field_name("name")
    if name is not None and name.type == "subscript":
        name = name.child_by_field_name("name")
    return _node_text(name)


def _benign_missing(node) -> bool:
    """Whether a missing node is the absent command name of an
    assignment-only command (`A=1 >file B=2`), which bash accepts; every
    other missing node fails closed as unparseable."""
    parent = node.parent
    grand = parent.parent if parent is not None else None
    return (
        node.type == "word"
        and parent is not None
        and parent.type == "command_name"
        and grand is not None
        and grand.type == "command"
        and any(c.type in _REDIRECT_TYPES or c.type == "variable_assignment" for c in grand.named_children)
    )


def _manifest_while(while_node) -> bool:
    """True only for the exact manifest loop the entrypoint is allowed to
    use: `while IFS= read -r TAU_ENTRYPOINT_*; do ...; done <
    "$TAU_ENTRYPOINT_SYNC_MANIFEST"` — one empty `IFS=`, the unquoted
    `read`, the single option `-r`, one prefixed target, and exactly one
    redirect, the input redirect from the manifest, and the `while` keyword
    itself (tree-sitter parses `until` as a while_statement, but an until
    loop runs until the read fails — a different contract). This exact
    shape is the only place a command-local IFS= is not a guest-namespace
    violation."""
    # tree-sitter parses `until` as a while_statement too, and an until loop
    # runs until the read *fails* — a different contract — so the keyword
    # itself must be `while`.
    if while_node.type != "while_statement" or not _node_text(while_node).startswith("while"):
        return False
    cond = while_node.child_by_field_name("condition")
    name = cond.child_by_field_name("name") if cond is not None else None
    kids = cond.named_children if cond is not None else []
    if not (
        cond is not None
        and cond.type == "command"
        and name is not None
        and name.type == "command_name"
        and len(name.named_children) == 1
        and name.named_children[0].type == "word"
        and _node_text(name.named_children[0]) == "read"
        and len(kids) == 4
        and kids[0].type == "variable_assignment"
        and _assignment_name(kids[0]) == "IFS"
        and kids[0].child_by_field_name("value") is None
        and kids[1].type == "command_name"
        and kids[2].type == "word"
        and _node_text(kids[2]) == "-r"
        and kids[3].type == "word"
        and _MANIFEST_TARGET_RE.match(_node_text(kids[3]))
    ):
        return False
    parent = while_node.parent
    if parent is None or parent.type != "redirected_statement":
        return False
    redirects = [c for c in parent.named_children if c.type in _REDIRECT_TYPES]
    if len(redirects) != 1 or redirects[0].type != "file_redirect":
        return False
    rkids = redirects[0].children
    dest = redirects[0].named_children[0] if redirects[0].named_children else None
    return (
        len(rkids) == 2
        and _node_text(rkids[0]) == "<"
        and dest is not None
        and _node_text(dest) == '"$TAU_ENTRYPOINT_SYNC_MANIFEST"'
    )


_FD_NAME_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _fd_allocation_offenders(command) -> set[str]:
    """Offenders of `{NAME}` descriptor allocations: a word-shaped argument
    ending exactly at a redirect operator of the same command (redirects sit
    either among the command's own children or beside it under a
    redirected_statement) makes Bash allocate an fd and assign its number to
    NAME, so `{NAME}` must carry the reserved prefix; any other brace fragment
    or dynamic word abutting a redirect is not statically nameable and fails
    the whole file closed."""
    scopes = [command]
    if command.parent is not None and command.parent.type == "redirected_statement":
        scopes.append(command.parent)
    operators = [
        (r.children[1] if r.children[0].type == "file_descriptor" else r.children[0])
        for s in scopes
        for r in s.named_children
        if r.type in _REDIRECT_TYPES and r.children
    ]
    starts = {op.start_byte for op in operators}
    bad = set()
    for c in command.named_children:
        if c.type in ("command_name", "variable_assignment") or c.type in _REDIRECT_TYPES:
            continue
        if c.end_byte not in starts:
            continue
        text = _node_text(c)
        m = _FD_NAME_RE.fullmatch(text)
        if m is not None:
            if not m.group(1).startswith(ENTRYPOINT_PREFIX):
                bad.add(m.group(1))
        elif any(ch in text for ch in "{}$`"):
            bad.add(_DYNAMIC_COMMAND)
    return bad


def _is_manifest_read(command) -> bool:
    """Whether a command is the `read` condition of the exact manifest
    while loop — the one place `read` is allowed at all."""
    parent = command.parent if command is not None else None
    return parent is not None and parent.type == "while_statement" and _manifest_while(parent)


def _allowed_manifest_ifs(var_node) -> bool:
    """Whether an `IFS=` assignment is the manifest loop's control
    assignment: the exact manifest-while shape already guarantees the
    empty value and the `read` command around it."""
    return _assignment_name(var_node) == "IFS" and _is_manifest_read(var_node.parent)


def _arg_groups(node):
    """Group a node's children into source-contiguous argument units, so a
    subscripted name split across nodes (`unset SNEAKY[0]`) is one argument."""
    groups, current, prev_end = [], [], None
    for c in node.named_children:
        if prev_end is not None and c.start_byte > prev_end:
            groups.append(current)
            current = []
        current.append(c)
        prev_end = c.end_byte
    if current:
        groups.append(current)
    return groups


def _declaration_offenders(kw, children) -> set[str]:
    """Offending targets of one declaration builtin: a variable_assignment
    under `export` may use an enumerated guest name, every other target
    must use the reserved prefix, and a `-n` nameref — whose writes escape
    the reserved namespace — is rejected wholesale."""
    bad = set()
    for c in children:
        if c.type == "variable_assignment":
            name = _assignment_name(c)
            if (kw != "export" or name not in GUEST_EXPORT_VARIABLES) and not name.startswith(ENTRYPOINT_PREFIX):
                bad.add(name)
            continue
        text = _static_word_text(c)
        if text is None:
            bad.add(_node_text(c))
        elif _OPTION_WORD_RE.fullmatch(text):
            if "n" in text[1:]:
                bad.add(_DYNAMIC_COMMAND)
        else:
            m = _TARGET_RE.match(text)
            if m is None or not m.group(1).startswith(ENTRYPOINT_PREFIX):
                bad.add(m.group(1) if m else text)
    return bad


def _unset_offenders(groups) -> set[str]:
    """Names an `unset` argument list removes: every target (option bundles
    skipped) must use the reserved prefix, and a runtime-resolved target
    fails the whole call closed."""
    bad = set()
    for nodes in groups:
        parts = [_static_word_text(n) for n in nodes]
        if None in parts:
            bad.add("".join(_node_text(n) for n in nodes))
            continue
        text = "".join(parts)
        if text == "--" or _UNSET_OPTION_RE.fullmatch(text):
            continue
        m = _TARGET_RE.fullmatch(text)
        if m is None or not m.group(1).startswith(ENTRYPOINT_PREFIX):
            bad.add(m.group(1) if m else text)
    return bad


def _command_offenders(node) -> set[str]:
    """Offenders of one simple command: the first word — unquoted, after a
    single `builtin`/`command` unwrap — is dispatched against the dialect.
    A wrapper whose inner word is missing, an option, another wrapper
    (deeper than one level), or dynamically resolved fails closed."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return set()  # assignment-only command; assignments are walked above
    word = _static_word_text(name_node)
    if word is None:
        return {_DYNAMIC_COMMAND}
    if word in _RESERVED_WORDS:
        return {_RESERVED_WORD}
    args = [
        c
        for c in node.named_children
        if c.type not in ("command_name", "variable_assignment") and c.type not in _REDIRECT_TYPES
    ]
    if word in ("builtin", "command"):
        inner = _static_word_text(args[0]) if args else None
        if inner is None or inner.startswith("-") or inner in ("builtin", "command"):
            return {_DYNAMIC_COMMAND}
        word, args = inner, args[1:]
    if word in _DECLARATION_KEYWORDS:
        return _declaration_offenders(word, args)
    if word == "read":
        return set() if _is_manifest_read(node) else {_BLOCKED_WRITER}
    if word == "unset":
        return _unset_offenders([[a] for a in args])
    if word == "getopts":
        # getopts OPTSTRING NAME: the option variable must be prefixed;
        # OPTIND/OPTARG are known-reserved Bash state, part of the dialect.
        name = _static_word_text(args[1]) if len(args) >= 2 else None
        if name is None:
            return {_node_text(args[1]) if len(args) >= 2 else _DYNAMIC_COMMAND}
        return set() if name.startswith(ENTRYPOINT_PREFIX) else {name}
    if word == "printf":
        # Writer-free only: a static format word and no option words, so
        # no `-v` nameref target can be present.
        fmt = _static_word_text(args[0]) if args else "-"
        if fmt is None:
            return {_DYNAMIC_COMMAND}
        return set() if not fmt.startswith("-") else {_BLOCKED_WRITER}
    if word == "shopt":
        # The alias machinery is the one shopt use that hides writers.
        if "expand_aliases" in {_static_word_text(a) for a in args}:
            return {_BLOCKED_WRITER}
        return set()
    if word in _BLOCKED_WRITERS:
        return {_BLOCKED_WRITER}
    return set()


def _entrypoint_offenders(text: str) -> set[str]:
    """Offender set of a Bash text under the bounded entrypoint dialect:
    unprefixed variable names plus the fail-closed classes."""
    parser = Parser(Language(tree_sitter_bash.language()))
    root = parser.parse(text.encode()).root_node
    offenders = set()
    stack = [root]
    while stack:
        node = stack.pop()
        ntype = node.type
        if ntype == "ERROR" or (node.is_missing and not _benign_missing(node)):
            offenders.add(_SYNTAX_ERROR)
        elif ntype in ("string", "raw_string") and not _node_text(node).endswith(_node_text(node)[0]):
            offenders.add(_SYNTAX_ERROR)  # unterminated quote
        elif ntype == "variable_assignment":
            parent = node.parent
            if (parent is None or parent.type != "declaration_command") and not _allowed_manifest_ifs(node):
                name = _assignment_name(node)
                if not name.startswith(ENTRYPOINT_PREFIX):
                    offenders.add(name)
        elif ntype == "declaration_command":
            kw = _node_text(node).split(maxsplit=1)[0]
            offenders |= _declaration_offenders(kw, node.named_children)
        elif ntype == "command":
            offenders |= _command_offenders(node) | _fd_allocation_offenders(node)
        elif ntype == "unset_command":
            offenders |= _unset_offenders(_arg_groups(node))
        elif ntype == "for_statement":
            var = node.child_by_field_name("variable")
            name = _static_word_text(var)
            if name is None or not name.startswith(ENTRYPOINT_PREFIX):
                offenders.add(name or (_node_text(var) if var is not None else _DYNAMIC_COMMAND))
        elif ntype in ("c_style_for_statement", "arithmetic_expansion"):
            offenders.add(_BLOCKED_WRITER)
        elif ntype == "compound_statement" and node.text.startswith(b"(("):
            offenders.add(_BLOCKED_WRITER)  # `((...))` arithmetic command
        elif ntype == "expansion":
            # Only the `=`/`:=` (mutating) and `!` (indirection) operators
            # can assign or redirect; the dialect has none, so any of them
            # fails closed with its source text.
            op = node.child_by_field_name("operator")
            if op is not None and _node_text(op) in ("=", ":=", "!"):
                offenders.add(_node_text(node))
        stack.extend(node.named_children)
    return offenders


def _manifest_prune_block(text: str) -> str:
    """Extract the sync-manifest pruning if-block from the entrypoint."""
    lines = text.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if 'if [ -f "$TAU_ENTRYPOINT_SYNC_MANIFEST" ]; then' in line
    )
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[start : end + 1]) + "\n"


def _run_manifest_prune(
    tmp_path: pathlib.Path, manifest: bytes, bootstrap_names: tuple[str, ...] = ()
) -> pathlib.Path:
    """Run the entrypoint manifest-pruning block against a temp environment."""
    taudir = tmp_path / "taudir"
    bootstrap = tmp_path / "bootstrap"
    taudir.mkdir()
    bootstrap.mkdir()
    for name in ("kept.txt", "stale.txt", "stray.txt"):
        (taudir / name).write_text("x")
    for name in bootstrap_names:
        (bootstrap / name).write_text("x")
    (taudir / ".host-config-synced").write_bytes(manifest)
    script = "\n".join(
        [
            "set -euo pipefail",
            f"TAU_ENTRYPOINT_DIR={shlex.quote(str(taudir))}",
            f"TAU_ENTRYPOINT_BOOTSTRAP_DIR={shlex.quote(str(bootstrap))}",
            'TAU_ENTRYPOINT_SYNC_MANIFEST="$TAU_ENTRYPOINT_DIR/.host-config-synced"',
            _manifest_prune_block(_read("entrypoint.sh")),
        ]
    )
    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True)
    return taudir


# --- .bashrc ---


def test_bashrc_exists():
    assert (CONFIG_DIR / ".bashrc").exists()


def test_bashrc_sets_prompt():
    assert "PS1=" in _read(".bashrc")


def test_bashrc_sets_local_bin_in_path():
    assert '$HOME/.local/bin' in _read(".bashrc")


def test_bashrc_sets_pythonuserbase():
    assert "PYTHONUSERBASE" in _read(".bashrc")


def test_bashrc_sets_npm_config_prefix():
    assert "NPM_CONFIG_PREFIX" in _read(".bashrc")


def test_bashrc_sets_pip_user():
    # pip --user installs land in ~/.local, which persists in the volume.
    assert "PIP_USER=1" in _read(".bashrc")


# --- APPEND_SYSTEM.md ---


def test_append_system_doc_exists():
    assert (CONFIG_DIR / "APPEND_SYSTEM.md").exists()


def test_append_system_doc_describes_filesystem():
    text = _read("APPEND_SYSTEM.md")
    for path in (
        "/workspace",
        "/home/tau",
        "/home/tau/.tau",
        "/home/tau/.tau/sessions",
        "/home/tau/.tau/logs",
        "/home/tau/.agents",
        "/tmp",
    ):
        assert path in text


def test_append_system_doc_describes_ephemeral_rootfs():
    # The microVM rootfs is discarded after every run; the doc must say so.
    assert "ephemeral" in _read("APPEND_SYSTEM.md").lower()


def test_append_system_doc_lists_installed_tools():
    text = _read("APPEND_SYSTEM.md")
    for tool in ("Python", "uv", "Node.js", "tau", "git", "ast-grep", "ripgrep"):
        assert tool in text


def test_append_system_doc_describes_security():
    text = _read("APPEND_SYSTEM.md")
    assert "UID 1000" in text
    assert "hardware-isolated" in text


def test_append_system_doc_describes_packages_file():
    assert ".tau-packages" in _read("APPEND_SYSTEM.md")


def test_append_system_doc_describes_lan_host_egress_rule():
    text = _read("APPEND_SYSTEM.md")
    assert "192.168.15.9" not in text
    assert "TAU_LAN_HOSTS" in text
    assert "other private-network addresses are blocked" in text


# --- entrypoint.sh ---

# The exact manifest streaming loop, and its four single deviations proven
# to revoke the exemption.
_MANIFEST_LOOP = (
    "while IFS= read -r TAU_ENTRYPOINT_X; do :\n"
    'done < "$TAU_ENTRYPOINT_SYNC_MANIFEST"\n'
)


def test_entrypoint_exists_and_executable():
    path = CONFIG_DIR / "entrypoint.sh"
    assert path.exists()
    assert os.access(path, os.X_OK)


def test_entrypoint_internal_variables_use_reserved_prefix():
    """Prove the entrypoint stays inside the bounded dialect: every scratch
    assignment, declaration target, loop variable, and unset target uses the
    reserved TAU_ENTRYPOINT_ prefix (enumerated guest names appear only as
    `export NAME=...`), the only `read` is the manifest streaming loop, and
    no blocked writer, reserved-word command, or dynamic command word
    appears anywhere, so entrypoint scratch state and the guest secret
    namespace can never collide."""
    assert _entrypoint_offenders(_read("entrypoint.sh")) == set()


@pytest.mark.parametrize(
    ("snippet", "offenders"),
    [
        # The allowed dialect: prefixed scratch names, enumerated guest
        # exports, reader-only commands, and the manifest streaming loop.
        ("export HOME=/tmp/root\n", set()),
        ('export TERM="${TERM:-xterm-256color}"\n', set()),
        ("TAU_ENTRYPOINT_X=1\n", set()),
        ("local TAU_ENTRYPOINT_X=1\n", set()),
        ("for TAU_ENTRYPOINT_X in *; do :\ndone\n", set()),
        ("unset TAU_ENTRYPOINT_X\n", set()),
        ('getopts "ab" TAU_ENTRYPOINT_X\n', set()),
        ("printf '%s\\n' \"$TAU_ENTRYPOINT_X\"\n", set()),
        ("shopt -s dotglob nullglob\n", set()),
        ("grep -q x\n", set()),
        (_MANIFEST_LOOP, set()),
        # Unprefixed names in the allowed writer forms are reported by name.
        ("SNEAKY=1\n", {"SNEAKY"}),
        ("export SNEAKY=1\n", {"SNEAKY"}),
        ("local SNEAKY\n", {"SNEAKY"}),
        ("unset SNEAKY\n", {"SNEAKY"}),
        ('getopts "ab" SNEAKY\n', {"SNEAKY"}),
        ("echo \"${SNEAKY:=1}\"\n", {"${SNEAKY:=1}"}),
        ('export "$name"\n', {'"$name"'}),
        ("builtin export SNEAKY=1\n", {"SNEAKY"}),
        # Writer builtins outside the dialect fail the whole file closed.
        ("read SNEAKY\n", {"<blocked writer>"}),
        ("builtin read SNEAKY\n", {"<blocked writer>"}),
        ("mapfile -t SNEAKY < /tmp/f\n", {"<blocked writer>"}),
        ("printf -v SNEAKY '%s' x\n", {"<blocked writer>"}),
        ("let SNEAKY=1\n", {"<blocked writer>"}),
        ("eval 'SNEAKY=1'\n", {"<blocked writer>"}),
        ("trap 'SNEAKY=1' EXIT\n", {"<blocked writer>"}),
        ("wait -p SNEAKY\n", {"<blocked writer>"}),
        ("cd /tmp\n", {"<blocked writer>"}),
        ("source ./guest.sh\n", {"<blocked writer>"}),
        (". ./guest.sh\n", {"<blocked writer>"}),
        ("shopt -s expand_aliases\n", {"<blocked writer>"}),
        # `{NAME}` fd allocation: a brace word ending exactly at a redirect
        # operator makes Bash assign the allocated descriptor number to NAME,
        # in every spelling (file, herestring, heredoc, plain command).
        ("exec {SNEAKY}>/dev/null\n", {"SNEAKY"}),
        ('exec {SNEAKY}<<<"hi"\n', {"SNEAKY"}),
        ("exec {SNEAKY}<<SNEAKY_DOC\nx\nSNEAKY_DOC\n", {"SNEAKY"}),
        ("echo {SNEAKY}>/dev/null\n", {"SNEAKY"}),
        ("read {SNEAKY}< /dev/null\n", {"SNEAKY", "<blocked writer>"}),
        ("exec {TAU_ENTRYPOINT_FD}>/dev/null\n", set()),
        ("echo {SNEAKY} >out\n", set()),
        ("echo {SNEAKY}\n", set()),
        ("exec {SNEAKY x}>/dev/null\n", {"<dynamic command>"}),
        ("for ((SNEAKY=0; SNEAKY<3; SNEAKY++)); do :\ndone\n", {"SNEAKY", "<blocked writer>"}),
        ("((SNEAKY=1))\n", {"<blocked writer>"}),
        ("echo \"$((SNEAKY=1))\"\n", {"<blocked writer>"}),
        ("local -n TAU_ENTRYPOINT_REF=TERM\n", {"<dynamic command>"}),
        # Reserved words surface as command names only in constructs the
        # dialect excludes, so they fail closed.
        ("time read SNEAKY\n", {"<reserved word>"}),
        ("coproc SNEAKY { :; }\n", {"<reserved word>"}),
        ("! { SNEAKY=1; }\n", {"<reserved word>"}),
        # Dynamic command words and wrapper nesting fail closed.
        ('command "$cmd" SNEAKY\n', {"<dynamic command>"}),
        ("builtin builtin export SNEAKY=1\n", {"<dynamic command>"}),
        # Indented because tree-sitter-bash mis-lexes a column-0 escaped
        # word as an argument of the previous command.
        ("    \\read SNEAKY\n", {"<dynamic command>"}),
        # The manifest-loop exemption is exact: every single deviation below
        # revokes it, so both the command-local IFS and the read fail closed.
        (_MANIFEST_LOOP.replace("-r ", "-ra "), {"IFS", "<blocked writer>"}),
        (_MANIFEST_LOOP[:-1] + " > /tmp/out\n", {"IFS", "<blocked writer>"}),
        (_MANIFEST_LOOP.replace("IFS= ", "IFS= IFS= "), {"IFS", "<blocked writer>"}),
        (_MANIFEST_LOOP.replace(" TAU_ENTRYPOINT_X", " TAU_ENTRYPOINT_X OTHER"), {"IFS", "<blocked writer>"}),
        # tree-sitter parses `until` as a while_statement, so the exemption
        # must match the keyword itself, not just the loop shape.
        (_MANIFEST_LOOP.replace("while ", "until "), {"IFS", "<blocked writer>"}),
    ],
)
def test_entrypoint_guard_enforces_bounded_dialect(snippet, offenders):
    """Prove the bounded-dialect guard classifies every dialect boundary
    correctly: allowed forms stay clean, unprefixed writer targets are
    reported by name, and writer builtins, reserved-word command names, and
    dynamic command words fail the whole file closed. Each snippet is
    spliced onto the real entrypoint so the guard runs against the exact
    text it protects; the manifest-loop deviation cases prove the IFS/read
    exemption matches the exact loop shape rather than any `read`
    spelling."""
    text = _read("entrypoint.sh") + "\n" + snippet
    assert _entrypoint_offenders(text) == offenders


def test_entrypoint_manifest_prune_uses_streaming_read_loop():
    """Prove the sync manifest is consumed by the streaming while-read loop,
    not a whole-file mapfile buffer. A mapfile rewrite would keep the entire
    manifest in memory and, under set -u on Bash 4.0-4.3, crash on an empty
    array expansion; the while-read loop streams one record at a time and is
    safe with empty manifests on every supported Bash."""
    text = _read("entrypoint.sh")
    assert "while IFS= read -r TAU_ENTRYPOINT_SYNCED_NAME; do" in text
    assert 'done < "$TAU_ENTRYPOINT_SYNC_MANIFEST"' in text
    assert "mapfile" not in text
    assert "readarray" not in text


@pytest.mark.parametrize(
    ("manifest", "bootstrap_names", "surviving"),
    [
        (b"", (), {"kept.txt", "stale.txt", "stray.txt"}),
        (b"stale.txt\n", (), {"kept.txt", "stray.txt"}),
        (b"stale.txt\nkept.txt\n", ("kept.txt",), {"kept.txt", "stray.txt"}),
    ],
)
def test_entrypoint_manifest_prune_removes_stale_sync_entries(
    tmp_path, manifest, bootstrap_names, surviving
):
    """Prove the manifest pruning keeps its well-formed-record behavior: an
    empty manifest prunes nothing, and a record whose bootstrap source no
    longer exists removes exactly that resource while names still present on
    the host survive. This pins the entrypoint bootstrap synchronization
    contract while the read loop is refactored."""
    taudir = _run_manifest_prune(tmp_path, manifest, bootstrap_names)
    for name in ("kept.txt", "stale.txt", "stray.txt"):
        assert (taudir / name).exists() == (name in surviving)


def test_entrypoint_manifest_prune_ignores_unterminated_final_record(tmp_path):
    """Prove orphan cleanup keeps the exact read semantics of the original
    loop: a final manifest record without a trailing newline is ignored. The
    sandbox can write the manifest between starts, and a mapfile rewrite would
    treat that partial record as a sync entry and delete the corresponding
    resource; this test is red against such rewrites."""
    taudir = _run_manifest_prune(tmp_path, b"stale.txt\nstray.txt")
    assert not (taudir / "stale.txt").exists()
    assert (taudir / "stray.txt").exists()


def test_entrypoint_has_required_directives():
    text = _read("entrypoint.sh")
    assert "set -euo pipefail" in text
    assert 'TAU_ENTRYPOINT_DIR="$TAU_ENTRYPOINT_HOME/.tau"' in text
    assert "rsync" not in text
    assert ".host-config-synced" in text
    assert "TAU_ENTRYPOINT_LEGACY_BOOTSTRAP_MARKER" in text
    assert "cp -a" in text
    assert "chmod -R u+w" in text
    assert 'rm -rf -- "$TAU_ENTRYPOINT_DESTINATION"' in text
    assert ".tau.msb-root-owned" in text
    assert "link_volume_dir /var/lib/tau-sandbox/sessions" in text
    assert "link_volume_dir /var/lib/tau-sandbox/logs" in text
    assert 'cp -Rn "$TAU_ENTRYPOINT_LEGACY/." "$TAU_ENTRYPOINT_BACKING/"' in text
    assert "TAU_NO_UPDATE_CHECK" in text
    assert 'exec "$@"' in text


def test_entrypoint_describes_isolated_config_layout():
    text = _read("entrypoint.sh")
    assert "/etc/tau-sandbox/bootstrap/tau" in text
    assert "/home/tau/.tau/credentials.json" in text
    assert "/home/tau/.tau/sessions" in text
    assert "/home/tau/.tau/logs" in text
    assert "/var/lib/tau-sandbox/sessions" in text
    assert "/var/lib/tau-sandbox/logs" in text
    assert "/etc/tau-sandbox/shared/credentials.json" in text
    assert "/home/tau/.agents" in text
    assert "/tau-source" not in text
    assert "APPEND_SYSTEM.md" not in text


def test_entrypoint_sets_persistent_env():
    text = _read("entrypoint.sh")
    assert "PYTHONUSERBASE" in text
    assert "NPM_CONFIG_PREFIX" in text
    assert "HOME=" in text


# --- tau-wrapper.py ---


def test_tau_wrapper_injects_immutable_prompt():
    text = _read("tau-wrapper.py")
    assert "--append-system-prompt" in text
    assert "/etc/tau-sandbox/APPEND_SYSTEM.md" in text
    assert "TAU_SANDBOX_SHARED_CREDENTIALS" in text
    assert "os.fsync" in text


# --- documentation contracts: project secrets ---


def _readme() -> str:
    return (REPO_ROOT / "README.md").read_text()


def _fenced_blocks(text: str) -> list[str]:
    return re.findall(r"```[a-zA-Z]*\n(.*?)```", text, re.S)


def _example_blocks(readme: str) -> tuple[list[set[str]], list[set[str]]]:
    """Name sets of every documentation block shaped like a valid
    secrets.env example (NAME=value lines) or a valid runtime-native
    secrets.yaml example (NAME: headers with value:/allow: fields)."""
    env_names: list[set[str]] = []
    policy_names: list[set[str]] = []
    value_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.+)$")
    name_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):$")
    for block in _fenced_blocks(readme):
        env: set[str] = set()
        policy: set[str] = set()
        is_env = bool(block.strip())
        is_policy = bool(block.strip())
        for line in block.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = value_re.match(line)
            if m is not None and line.isprintable():
                env.add(m.group(1))
                continue
            if not line.startswith(" "):
                m = name_re.match(line)
                if m is not None:
                    policy.add(m.group(1))
                    continue
            if line.startswith(("  value:", "  allow:", "  inject:", "    - ")):
                continue
            is_env = is_policy = False
            break
        if is_env and env:
            env_names.append(env)
        if is_policy and policy:
            policy_names.append(policy)
    return env_names, policy_names


def test_append_system_doc_distinguishes_placeholders_and_ordinary_values():
    """Prove the sandbox environment reference tells a guest agent that
    ordinary forwarded variables carry real values while protected
    project-secret variables are runtime placeholders, and never claims
    `env` reveals a protected real value. A guest that believed real values
    were inspectable would print them into transcripts and logs; the doc
    must make the placeholder the only guest-visible form."""
    text = _read("APPEND_SYSTEM.md")
    lower = text.lower()
    assert "$msb_" in lower
    assert "placeholder" in lower
    assert "ordinary" in lower
    # Ordinary forwarding is documented as carrying real values.
    assert "real value" in lower
    # Every sentence pairing `env` with real protected values must deny
    # visibility, never claim it.
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        s = sentence.lower()
        if re.search(r"\benv\b", s) and "real" in s and "protected" in s:
            assert "never" in s or "not" in s or "placeholder" in s, sentence


def test_readme_has_valid_paired_examples_and_exact_mapping():
    """Prove the README documents `TAU_PROJECTS_DIR` and the exact
    physical launch-directory mapping, and that its secrets.env/secrets.yaml
    examples are themselves grammatically valid paired sources with
    matching name sets. Invalid examples would teach users a configuration
    the launcher rejects."""
    readme = _readme()
    assert "TAU_PROJECTS_DIR" in readme
    assert "${HOME}/Projects" in readme
    assert "empty" in readme.lower()
    assert "relative" in readme.lower()
    assert "$HOME/Projects/megali" in readme
    assert "$HOME/.megali" in readme
    assert "$HOME/Projects/megali/main" in readme
    assert "$HOME/.megali/main" in readme
    assert "never inherit" in readme.lower()
    env_blocks, policy_blocks = _example_blocks(readme)
    assert env_blocks and policy_blocks
    # A paired example: one value block and one policy block with exactly
    # matching name sets, as the launcher requires.
    assert any(env in policy_blocks for env in env_blocks)


def test_readme_covers_pair_contract_and_reserved_names():
    """Prove the README documents the sourced-env and pass-through policy
    contract (no launcher grammar, runtime validation), the runtime
    requirement, and the exact reserved-name set. Stale claims about
    launcher-side grammars or version gates would teach users checks the
    launcher no longer performs."""
    readme = _readme()
    lower = readme.lower()
    assert "sourced as shell" in lower
    assert "native `--secret-conf` format" in lower
    assert "passed to the runtime unmodified" in lower
    assert "value: \"${OPENAI_API_KEY}\"" in readme
    assert "run --secret-conf" in readme
    assert "no version is checked" in lower
    # No stale grammar/gate claims.
    assert "printable ascii" not in lower
    assert ">=0.6.12" not in readme
    assert "plaintext fallback" not in lower
    # Reserved names: the exact list and prefixes, in the reserved section.
    reserved_section = readme.split("#### Reserved names", 1)[1]
    for token in ("PATH", "HOME", "BASH_ENV", "LD_PRELOAD", "BASH", "TAU_"):
        assert token in reserved_section


def test_readme_covers_boundary_network_reset_and_forwarding_precedence():
    """Prove the README documents the placeholder boundary delegated to
    the runtime contract, network independence of secret destinations,
    reset bypass, and the suppression of same-name raw env-file
    forwarding."""
    readme = _readme()
    lower = readme.lower()
    assert "present pair" in lower
    assert "tls" in lower
    assert "allowlist" in lower
    assert "request location" in lower
    assert "documented contract" in lower
    assert "never grants network access" in lower
    assert "bypasses secret discovery" in lower
    assert "suppressed from raw forwarding" in lower
    assert "trusted" in lower


def test_living_spec_contains_current_project_secret_requirements():
    """Prove the living specification carries the simplified project-secret
    requirements (paired sources, mapping, boundary, forwarding, reset,
    documentation) and none of the removed defensive machinery."""
    spec = (REPO_ROOT / "docs/SPEC.md").read_text()
    for name in (
        "Paired secret sources",
        "Exact secret location mapping",
        "Protected secret boundary",
        "Environment forwarding",
        "Reset bypasses secret discovery",
        "Project secret documentation",
    ):
        assert f"### Requirement: {name}" in spec, name
    for removed in (
        "Early source preflight",
        "Host-only source isolation",
        "Literal secret value grammar",
        "Restricted secret policy grammar",
        "Compatible secret runtime",
        "Collision-free source references",
        "Environment source isolation",
    ):
        assert f"### Requirement: {removed}" not in spec, removed
    # Sentinel pre-existing requirements are retained intact.
    for sentinel in (
        "Per-project persistent state",
        "Workspace bind mount",
        "Image build and load",
    ):
        assert f"### Requirement: {sentinel}" in spec


def test_scripts_pass_syntax_checks():
    """Prove shell and Python launcher scripts parse."""
    scripts = [REPO_ROOT / "run.sh", REPO_ROOT / "install.sh", CONFIG_DIR / "entrypoint.sh"]
    for script in scripts:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script.name} failed bash -n:\n{result.stderr}"

    result = subprocess.run(
        ["python", "-m", "py_compile", str(CONFIG_DIR / "tau-wrapper.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
