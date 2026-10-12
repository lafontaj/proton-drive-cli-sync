#!/usr/bin/env python3
"""proton-drive-sync-doctor: read-only report for bug reports.

Prints the install, CLI, systemd, settings, mapping and session facts needed to
diagnose an unattended run. It never writes, never uploads and never starts a
unit. Every external command has a timeout. A failure becomes a line in the
report, and the exit code is always 0.

Use --redact before pasting the report anywhere public: home paths become `~`,
other absolute paths become <path#n>, and e-mail addresses become <account>.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

import paths
import unitexec

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CMD_TIMEOUT = 10
# Same location as config.DATA_DIR. Not imported from config: that import
# renames ~/.proton_sync and copies settings, and this tool must not write.
DATA_DIR = os.path.expanduser("~/.proton-drive-sync")
_DEFAULTS = {
    "cli_stall_minutes": 5,
    "max_delete_min": 20,
    "max_delete_ratio": 0.5,
}

SYNC_SERVICE = "proton-sync.service"
SYNC_TIMER = "proton-sync.timer"
REALTIME_UNITS = ("proton-watch.service", "proton-consume.service")
NA = "not available"


# ---------- helpers ----------

def run_cmd(args, timeout=CMD_TIMEOUT):
    """(ok, text). Never raises."""
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        return False, "%s: %s (command not found)" % (args[0], NA)
    except subprocess.TimeoutExpired:
        return False, "%s: timed out after %ss" % (args[0], timeout)
    except Exception as exc:  # report, never fatal
        return False, "%s: %s: %s" % (args[0], type(exc).__name__, exc)
    text = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
    return r.returncode == 0, text.strip()


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as exc:
        return None, "unreadable (%s)" % type(exc).__name__


def compact(data, limit=1500):
    try:
        text = json.dumps(data, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(data)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def version_tuple(text):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


# ---------- redaction ----------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ABS_PATH = re.compile(r"(?<![\w./~<>#:=@-])/(?:[^\s\"'<>|;,()\[\]{}]+)")
_HOME_PATH = re.compile(r"(?<![\w.<>#-])~(/[^\s\"'<>|;,()\[\]{}]+)")
# Fixed system and application locations: not personal, useful in a report.
_KEEP_ABS = ("/usr/", "/proc/", "/sys/", "/bin/", "/etc/systemd/",
             "/run/user/", "/lib/")
_KEEP_HOME = ("/.proton-drive-sync", "/.config/proton-drive-sync",
              "/.config/systemd/user")


class Redactor:
    """Replaces personal strings. Known values (seen in settings and mappings)
    are replaced literally first, so a path with spaces cannot leak a tail."""

    def __init__(self):
        self.known = []
        self.numbers = {}

    def number(self, path):
        if path not in self.numbers:
            self.numbers[path] = len(self.numbers) + 1
        return "<path#%d>" % self.numbers[path]

    def learn(self, value):
        if isinstance(value, str) and len(value) > 1 and "/" in value:
            self.known.append(value.rstrip("/"))

    def learn_tree(self, data):
        if isinstance(data, dict):
            for v in data.values():
                self.learn_tree(v)
        elif isinstance(data, list):
            for v in data:
                self.learn_tree(v)
        elif isinstance(data, str) and data.startswith("/"):
            self.learn(data)

    def apply(self, text):
        home = os.path.expanduser("~").rstrip("/")
        for value in sorted(set(self.known), key=len, reverse=True):
            if value and value != home:
                text = text.replace(value, self.number(value))
        text = _EMAIL.sub("<account>", text)
        if home and home != "/":
            text = _replace_home_boundary(text, home)
        text = _HOME_PATH.sub(self._home_sub, text)
        return _ABS_PATH.sub(self._abs_sub, text)

    def _home_sub(self, m):
        rest = m.group(1)
        if rest.startswith(_KEEP_HOME):
            return m.group(0)
        return "~/" + self.number(rest.lstrip("/"))

    def _abs_sub(self, m):
        path = m.group(0)
        if path.startswith(_KEEP_ABS):
            return path
        return self.number(path)


# ---------- report sections ----------

class Report:
    def __init__(self):
        self.lines = []

    def section(self, title):
        self.lines.append("")
        self.lines.append("== %s ==" % title)

    def add(self, text=""):
        for line in str(text).splitlines() or [""]:
            self.lines.append(line)

    def kv(self, key, value):
        self.add("%s: %s" % (key, value))

    def text(self):
        return "\n".join(self.lines) + "\n"


def _replace_home_boundary(text, home):
    """Replace HOME only when it is a path prefix, not a username prefix.
    `/home/meg` must not turn `/home/megan/secret` into `~an/secret`."""
    out = []
    i = 0
    n = len(home)
    while True:
        j = text.find(home, i)
        if j < 0:
            out.append(text[i:])
            break
        nxt = text[j + n] if j + n < len(text) else ""
        if nxt in ("", "/", '"', "'", " ", "\t", "\n", "\r"):
            out.append(text[i:j])
            out.append("~")
            i = j + n
        else:
            out.append(text[i:j + n])
            i = j + n
    return "".join(out)


def _learn_paths(redactor):
    """Paths that can contain spaces, learned before the regex pass."""
    redactor.learn(APP_DIR)
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        redactor.learn(xdg)
    for value in os.environ.values():
        if isinstance(value, str) and value.startswith("/") and len(value) > 1:
            redactor.learn(value)


def _learn_unit_text(redactor, text):
    for line in (text or "").splitlines():
        body = None
        if line.startswith("ExecStart="):
            body = line[len("ExecStart="):]
        elif line.startswith("Environment="):
            body = line[len("Environment="):]
        if body is None:
            continue
        for arg in unitexec.split_exec(body):
            redactor.learn(arg)
            if "=" in arg:
                redactor.learn(arg.split("=", 1)[1])


def settings_file():
    """Settings path, read only. Does not copy the legacy file."""
    override = os.environ.get("PROTON_SYNC_SETTINGS", "").strip()
    if override:
        return override
    dest = paths.xdg_settings_path()
    try:
        if os.path.isfile(dest):
            return dest
    except OSError:
        pass
    legacy = paths.legacy_settings_path()
    try:
        if os.path.isfile(legacy):
            return legacy
    except OSError:
        pass
    return dest


def _load_settings():
    path = settings_file()
    data, err = read_json(path)
    if not isinstance(data, dict):
        data = {}
    return path, data, err


def _effective_rename(data, modern):
    if "rename_ext_enabled" in data and not data.get("rename_ext_enabled"):
        return False
    if (data.get("rename_ext_enabled") is True
            and data.get("rename_ext_auto_disabled") is True):
        return True
    return not bool(modern)


def _int_setting(data, key):
    try:
        n = int(data.get(key))
    except (TypeError, ValueError):
        return _DEFAULTS[key]
    return n if n >= 0 else _DEFAULTS[key]


def _ratio_setting(data):
    try:
        r = float(data.get("max_delete_ratio"))
    except (TypeError, ValueError):
        return _DEFAULTS["max_delete_ratio"]
    return r if 0.0 <= r <= 1.0 else _DEFAULTS["max_delete_ratio"]


def _resolve_cli(settings):
    # Même repli que paths.resolve_cli. config n'est pas importé ici :
    # son import réécrit des dossiers sous $HOME, et ce rapport ne doit rien écrire.
    env = os.environ.get("PROTON_DRIVE_CLI")
    configured = settings.get("proton_cli_path")
    if not isinstance(configured, str) or not configured.strip():
        configured = None
    else:
        configured = configured.strip()
    bundled = os.path.join(APP_DIR, "proton-drive")
    for path in (env, configured):
        if path and os.path.exists(path):
            return path
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    found = shutil.which("proton-drive")
    if found:
        return found
    return env or configured or bundled


def engine_version():
    """(__version__ de proton_sync.py, `git describe` ou None). Lu sans importer
    le moteur : importer config créerait des dossiers pendant le diagnostic."""
    version = None
    try:
        with open(os.path.join(APP_DIR, "proton_sync.py"), "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("__version__"):
                    version = line.split("=", 1)[1].split("#", 1)[0].strip().strip("\"'")
                    break
    except OSError:
        pass
    commit = None
    if os.path.isdir(os.path.join(APP_DIR, ".git")):
        ok, out = run_cmd(["git", "-C", APP_DIR, "describe", "--tags", "--always"])
        if ok:
            commit = out
    return version, commit


def section_install(rep):
    rep.section("Install")
    version, commit = engine_version()
    rep.kv("engine version", version or "unknown")
    rep.kv("git version", commit or "not a git checkout")
    rep.kv("program directory", APP_DIR)
    rep.kv("python", sys.version.split()[0])
    rep.kv("HOME", os.path.expanduser("~"))
    rep.kv("XDG_CONFIG_HOME", os.environ.get("XDG_CONFIG_HOME") or "(unset)")


def section_cli(rep, settings):
    rep.section("Proton CLI")
    configured = settings.get("proton_cli_path")
    if not (isinstance(configured, str) and configured.strip()):
        configured = None
    chain = [
        ("PROTON_DRIVE_CLI", os.environ.get("PROTON_DRIVE_CLI") or "(unset)"),
        ("settings proton_cli_path", configured or "(unset)"),
        ("next to the scripts",
         os.path.join(APP_DIR, "proton-drive")
         if os.access(os.path.join(APP_DIR, "proton-drive"), os.X_OK)
         else "(not found)"),
        ("on PATH", shutil.which("proton-drive") or "(not found)"),
    ]
    for label, value in chain:
        rep.kv("resolve: " + label, value)
    try:
        cli = _resolve_cli(settings)
    except Exception as exc:
        rep.kv("resolved", "error (%s)" % type(exc).__name__)
        cli = None
    rep.kv("resolved", cli or NA)
    version = None
    if cli and os.path.exists(cli):
        ok, out = run_cmd([cli, "--version"])
        rep.kv("proton-drive --version", out.splitlines()[0] if out else NA)
        version = version_tuple(out) if ok else None
    else:
        rep.kv("proton-drive --version", NA + " (CLI not found)")
    return version


def section_systemd(rep, redactor):
    rep.section("systemd (user)")
    if shutil.which("systemctl") is None:
        rep.add("systemctl: " + NA)
    else:
        for unit in (SYNC_SERVICE, SYNC_TIMER) + REALTIME_UNITS:
            rep.add("-- %s" % unit)
            ok, out = run_cmd(["systemctl", "--user", "cat", unit])
            _learn_unit_text(redactor, out)
            rep.add(out if out else NA)
            ok, out = run_cmd(["systemctl", "--user", "show", unit, "-p",
                               "Result", "-p", "ExecMainStatus",
                               "-p", "ActiveState"])
            rep.add(out if out else NA)
        ok, out = run_cmd(["systemctl", "--user", "list-timers", "--all",
                           SYNC_TIMER, "--no-pager"])
        rep.add("-- list-timers")
        rep.add(out if out else NA)
    rep.add("-- linger")
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if shutil.which("loginctl") is None or not user:
        rep.add("linger: " + NA)
    else:
        ok, out = run_cmd(["loginctl", "show-user", user, "-p", "Linger"])
        rep.add(out if out else NA)
    rep.add("-- journal (last 50 lines of %s)" % SYNC_SERVICE)
    if shutil.which("journalctl") is None:
        rep.add("journalctl: " + NA)
    else:
        ok, out = run_cmd(["journalctl", "--user", "-u", SYNC_SERVICE,
                           "-n", "50", "--no-pager"])
        rep.add(out if out else NA)


def section_state(rep, redactor):
    rep.section("Last run and health")
    for label, name in (("last-run.json", "last-run.json"),
                        ("health.json", "health.json")):
        path = os.path.join(DATA_DIR, name)
        data, err = read_json(path)
        redactor.learn_tree(data)
        redactor.learn(path)
        rep.kv(label, compact(data) if err is None else err)


def mappings_file(explicit):
    if explicit:
        return explicit
    path = os.path.expanduser("~/.config/systemd/user/proton-sync.service")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r"^ExecStart=(.*)$", content, re.MULTILINE)
    if not m:
        return None
    return unitexec.mappings_arg_from_exec(
        m.group(1), "proton_sync.py", "proton-drive-sync")


def section_settings(rep, redactor, cli_version, mappings_arg, settings, settings_path, settings_err):
    rep.section("Settings")
    rep.kv("settings file", settings_path)
    redactor.learn(settings_path)
    if settings_err == "missing":
        rep.kv("settings", "missing")
    elif settings_err is not None:
        rep.kv("settings", settings_err)
    else:
        shown = dict(settings)
        redactor.learn_tree(shown)
        for key in sorted(shown):
            if "account" in key or "email" in key:
                shown[key] = "<hidden>"
        rep.kv("settings (as stored)", compact(shown))
    modern = bool(cli_version and cli_version >= (0, 5, 0))
    effective = _effective_rename(settings, modern)
    rep.kv("rename-ext effective", "%s (CLI >= 0.5.0: %s)"
           % ("on" if effective else "off",
              "yes" if modern else "no or unknown"))
    rep.kv("max_delete_min", _int_setting(settings, "max_delete_min"))
    rep.kv("max_delete_ratio", _ratio_setting(settings))
    rep.kv("cli_stall_minutes", _int_setting(settings, "cli_stall_minutes"))

    rep.section("Mappings")
    path = mappings_file(mappings_arg)
    if path:
        redactor.learn(path)
        _learn_unit_text(redactor, "ExecStart=" + path)
    rep.kv("mappings file", path or "unknown (no unit and no --mappings)")
    if not path:
        return
    data, err = read_json(path)
    if err is not None:
        rep.kv("mappings", err)
        return
    redactor.learn_tree(data)
    if isinstance(data, dict):
        mappings = data.get("mappings") or []
        global_ex = data.get("exclusions") or {}
    else:
        mappings, global_ex = data, {}
    rep.kv("global exclusions", _ex_counts(global_ex))
    if not isinstance(mappings, list):
        rep.kv("mappings", "unexpected shape")
        return
    for i, m in enumerate(mappings, 1):
        if not isinstance(m, dict):
            rep.add("mapping %d: unexpected shape" % i)
            continue
        rep.add("mapping %d: type=%s source=%s dest_parent=%s" % (
            i, m.get("type"), m.get("source"), m.get("dest_parent")))
        rep.add("  allow_delete=%s delete_mode=%s excluded_remote=%s "
                "source_kind=%s conflict_mode=%s exclusions=%s" % (
                    m.get("allow_delete"), m.get("delete_mode"),
                    m.get("excluded_remote"), m.get("source_kind"),
                    m.get("conflict_mode"), _ex_counts(m.get("exclusions"))))


def _ex_counts(ex):
    if not isinstance(ex, dict):
        return "names=0 patterns=0"
    names = ex.get("names") or []
    patterns = ex.get("patterns") or []
    return "names=%d patterns=%d" % (len(names), len(patterns))


def section_session(rep):
    rep.section("Session (unattended auth)")
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    rep.kv("DBUS_SESSION_BUS_ADDRESS", "set" if bus else "not set")
    rep.kv("PROTON_DRIVE_CREDENTIALS_STORE",
           os.environ.get("PROTON_DRIVE_CREDENTIALS_STORE") or "(unset)")
    if shutil.which("busctl") is None:
        rep.kv("Secret Service (org.freedesktop.secrets)", NA + " (busctl missing)")
    else:
        ok, out = run_cmd(["busctl", "--user", "status",
                           "org.freedesktop.secrets"])
        rep.kv("Secret Service (org.freedesktop.secrets)",
               "reachable" if ok else "not reachable")


def build_report(mappings_arg=None, redact=False):
    rep = Report()
    redactor = Redactor()
    _learn_paths(redactor)
    settings_path, settings, settings_err = _load_settings()
    rep.add("proton-drive-sync doctor (read-only)")
    rep.add("Redaction: %s" % ("on" if redact else
                               "OFF - run with --redact before pasting this publicly"))
    _guard(rep, section_install, rep)
    cli_version = _guard(rep, section_cli, rep, settings)
    _guard(rep, section_systemd, rep, redactor)
    _guard(rep, section_state, rep, redactor)
    _guard(rep, section_settings, rep, redactor, cli_version, mappings_arg,
           settings, settings_path, settings_err)
    _guard(rep, section_session, rep)
    text = rep.text()
    return redactor.apply(text) if redact else text


def _guard(rep, fn, *args):
    """A broken section is a line in the report, not a crash."""
    try:
        return fn(*args)
    except Exception as exc:
        rep.add("%s failed: %s: %s" % (fn.__name__, type(exc).__name__, exc))
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="proton-drive-sync-doctor",
        description="Read-only diagnostic report. Exit code is always 0.")
    parser.add_argument("--redact", action="store_true",
                        help="hide e-mails and personal paths (use before pasting)")
    parser.add_argument("--mappings", help="mappings file (default: the one in the unit)")
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(build_report(args.mappings, args.redact))
    except Exception as exc:  # last resort: still a report, still exit 0
        sys.stdout.write("doctor failed: %s: %s\n" % (type(exc).__name__, exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
