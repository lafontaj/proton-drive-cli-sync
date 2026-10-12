"""Helpers for the systemd unit text written by schedule_manager and realtime_manager.

Pure functions, no side effects at import. Only the standard library.

- systemd_quote: one argument as systemd reads it in ExecStart= / Environment=.
- split_exec: the reverse, tolerant of units written by older versions
  (unquoted paths).
- exec_command: the command prefix for a script (packaged launcher or python3).
"""

import os

# Where the Arch package installs the program files (packaging/arch/PKGBUILD).
PACKAGED_DIR = "/usr/lib/proton-drive-cli-sync"

# Stable launchers (packaging/arch/launchers), by script name.
PACKAGED_LAUNCHERS = {
    "proton_sync.py": "/usr/bin/proton-drive-sync",
    "cloudlaunch.py": "/usr/bin/proton-drive-sync-cloud",
}

_NEEDS_QUOTES = set(" \t\r\n\"'\\;")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "s": " ",
            "\\": "\\", '"': '"', "'": "'"}


def systemd_quote(arg):
    """Quote one argument for ExecStart=. `%` -> `%%` (specifier), `$` -> `$$`
    (environment substitution), `\\` and `"` are escaped, and the argument is
    wrapped in double quotes when it has whitespace, quotes or a backslash."""
    text = str(arg)
    text = text.replace("%", "%%").replace("$", "$$")
    if text and not any(c in _NEEDS_QUOTES for c in text):
        return text
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + text + '"'


def quote_environment(name, value):
    """One `Environment=` assignment. systemd does not expand `$` in
    Environment= (only `%` specifiers), so `$` is left as-is. `%` is doubled,
    and `\\` and `"` are escaped when the assignment is quoted."""
    text = "%s=%s" % (name, value)
    text = text.replace("%", "%%")
    if text and not any(c in _NEEDS_QUOTES for c in text):
        return text
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + text + '"'


def split_exec(line):
    """Split an ExecStart= value (without the `ExecStart=` prefix) into
    arguments. Reads quoted arguments, backslash escapes, `%%` and `$$`.
    An unquoted legacy line splits on whitespace. A lone `%` or `$` is kept."""
    args = []
    cur = []
    have = False
    quote = None
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "\\" and i + 1 < n:
            nxt = line[i + 1]
            cur.append(_ESCAPES.get(nxt, "\\" + nxt))
            have = True
            i += 2
            continue
        if c in "%$" and i + 1 < n and line[i + 1] == c:
            cur.append(c)
            have = True
            i += 2
            continue
        if quote:
            if c == quote:
                quote = None
            else:
                cur.append(c)
            i += 1
            continue
        if c in "\"'":
            quote = c
            have = True
            i += 1
            continue
        if c in " \t":
            if have:
                args.append("".join(cur))
                cur, have = [], False
            i += 1
            continue
        cur.append(c)
        have = True
        i += 1
    if have:
        args.append("".join(cur))
    return args


def is_packaged(app_dir):
    try:
        return os.path.realpath(app_dir) == os.path.realpath(PACKAGED_DIR) \
            or os.path.normpath(app_dir) == PACKAGED_DIR
    except (OSError, ValueError):
        return False


def exec_command(script_path, app_dir=None):
    """Argument list that starts `script_path`. A packaged install uses the
    stable /usr/bin launcher when one exists (stable across upgrades);
    otherwise `/usr/bin/python3 <script>`."""
    base = os.path.basename(script_path)
    directory = app_dir if app_dir is not None else os.path.dirname(script_path)
    if is_packaged(directory) and base in PACKAGED_LAUNCHERS:
        return [PACKAGED_LAUNCHERS[base]]
    return ["/usr/bin/python3", script_path]


def exec_line(script_path, args=(), app_dir=None):
    """Full `ExecStart=` line with every argument quoted."""
    parts = exec_command(script_path, app_dir) + [str(a) for a in args]
    return "ExecStart=" + " ".join(systemd_quote(p) for p in parts)


def mappings_arg_from_exec(value, script_name, launcher_name=None):
    """First argument after the script (or its launcher) in an ExecStart value,
    or None. Works on quoted and legacy unquoted lines."""
    args = split_exec(value)
    for idx, tok in enumerate(args):
        base = os.path.basename(tok)
        if base == script_name or (launcher_name and base == launcher_name):
            if idx + 1 < len(args):
                return args[idx + 1]
            return None
    return None
