"""Escaped systemd units, doctor, launchers and the Arch PKGBUILD."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import realtime_manager
import schedule_manager
import unitexec

REPO = Path(__file__).resolve().parents[1]
ARCH = REPO / "packaging" / "arch"
PACKAGED = "/usr/lib/proton-drive-cli-sync"


def _exec_line(text):
    return next(l for l in text.splitlines() if l.startswith("ExecStart="))


def test_environment_quote_keeps_dollar():
    quoted = unitexec.quote_environment("PROTON_DRIVE_CLI", "/opt/a$b/%p")
    assert "a$b" in quoted
    assert "a$$b" not in quoted
    assert "%%p" in quoted
    assert unitexec.split_exec(quoted) == ["PROTON_DRIVE_CLI=/opt/a$b/%p"]


@pytest.mark.parametrize("arg,expected", [
    ("/plain/path.json", "/plain/path.json"),
    ("/with space/m.json", '"/with space/m.json"'),
    ('/with"quote/m.json', '"/with\\"quote/m.json"'),
    ("/100%/m.json", "/100%%/m.json"),
    ("/back\\slash/m.json", '"/back\\\\slash/m.json"'),
    ("/cost$/m.json", "/cost$$/m.json"),
    ("", '""'),
    ('a b"c%d', '"a b\\"c%%d"'),
])
def test_systemd_quote_spaces_quotes_percent_backslash(arg, expected):
    assert unitexec.systemd_quote(arg) == expected
    # What systemd reads back is the original text (specifiers unescaped).
    assert unitexec.split_exec(expected) == [arg]


def _use_unit_dir(monkeypatch, tmp_path):
    unit_dir = tmp_path / "user"
    unit_dir.mkdir()
    monkeypatch.setattr(schedule_manager, "SYSTEMD_USER_DIR", str(unit_dir))
    monkeypatch.setattr(schedule_manager, "SERVICE_PATH",
                        str(unit_dir / schedule_manager.SERVICE_NAME))
    return unit_dir


@pytest.mark.parametrize("name", [
    "my maps/mappings.json",
    'quo"te/mappings.json',
    "100%/mappings.json",
    "back\\slash/mappings.json",
])
@pytest.mark.parametrize("delete", [False, True])
def test_service_unit_with_space_in_mappings_path_roundtrips(
        tmp_path, monkeypatch, name, delete):
    _use_unit_dir(monkeypatch, tmp_path)
    path = str(tmp_path) + "/" + name
    text = schedule_manager.build_service_text(path, delete=delete)
    Path(schedule_manager.SERVICE_PATH).write_text(text, encoding="utf-8")
    assert schedule_manager.read_service_mappings_path() == path
    assert schedule_manager.read_service_delete() is delete


def test_legacy_unquoted_unit_still_parsed(tmp_path, monkeypatch):
    _use_unit_dir(monkeypatch, tmp_path)
    Path(schedule_manager.SERVICE_PATH).write_text(
        "[Service]\n"
        "Environment=PROTON_DRIVE_CLI=/opt/app/proton-drive\n"
        "ExecStart=/usr/bin/python3 /opt/app/proton_sync.py /data/mappings.json --delete\n",
        encoding="utf-8")
    assert schedule_manager.read_service_mappings_path() == "/data/mappings.json"
    assert schedule_manager.read_service_delete() is True
    Path(schedule_manager.SERVICE_PATH).write_text(
        "[Service]\nExecStart=/usr/bin/python3 /opt/app/proton_sync.py /data/m.json\n",
        encoding="utf-8")
    assert schedule_manager.read_service_mappings_path() == "/data/m.json"
    assert schedule_manager.read_service_delete() is False


def test_legacy_consumer_unit_still_parsed(tmp_path, monkeypatch):
    consume = tmp_path / "proton-consume.service"
    consume.write_text(
        "[Service]\nExecStart=/usr/bin/python3 /opt/app/realtime_consumer.py /data/m.json\n",
        encoding="utf-8")
    monkeypatch.setattr(realtime_manager, "CONSUME_PATH", str(consume))
    assert realtime_manager.read_units_mappings_path() == "/data/m.json"


def test_packaged_install_uses_usr_bin_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule_manager, "APP_DIR", PACKAGED)
    monkeypatch.setattr(schedule_manager, "DEFAULT_ENGINE", PACKAGED + "/proton_sync.py")
    monkeypatch.setattr(schedule_manager, "DEFAULT_CLI", PACKAGED + "/proton-drive")
    path = str(tmp_path / "my maps" / "mappings.json")
    text = schedule_manager.build_service_text(path, delete=True)
    line = _exec_line(text)
    assert line.startswith("ExecStart=/usr/bin/proton-drive-sync ")
    assert "python3" not in line and PACKAGED not in line
    assert "PROTON_DRIVE_CLI=" not in text  # nothing to point at under /usr/lib
    assert "RestartPreventExitStatus=5" in text
    monkeypatch.setattr(schedule_manager, "SERVICE_PATH",
                        str(tmp_path / "proton-sync.service"))
    Path(schedule_manager.SERVICE_PATH).write_text(text, encoding="utf-8")
    assert schedule_manager.read_service_mappings_path() == path
    assert schedule_manager.read_service_delete() is True


def test_unpackaged_install_keeps_python_path(monkeypatch, tmp_path):
    text = schedule_manager.build_service_text(str(tmp_path / "m.json"))
    line = _exec_line(text)
    assert line.startswith("ExecStart=/usr/bin/python3 ")
    default = schedule_manager.DEFAULT_CLI
    if os.path.isfile(default) and os.access(default, os.X_OK):
        assert "Environment=PROTON_DRIVE_CLI=" in text
    else:
        assert "PROTON_DRIVE_CLI=" not in text


def test_consumer_unit_generation_escaped(tmp_path, monkeypatch):
    app = str(tmp_path / "my app")
    monkeypatch.setattr(realtime_manager, "H_ENGINE_DIR", app)
    path = str(tmp_path / "my maps" / "100%.json")
    for build, script in ((realtime_manager.build_consume_service_text,
                           "realtime_consumer.py"),
                          (realtime_manager.build_watch_service_text,
                           "local_watcher.py")):
        text = build(path)
        args = unitexec.split_exec(_exec_line(text)[len("ExecStart="):])
        assert args == ["/usr/bin/python3", app + "/" + script, path]
        assert '"%s"' % (app + "/" + script) in text
        assert "100%%.json" in text
        default = realtime_manager.DEFAULT_CLI
        if os.path.isfile(default) and os.access(default, os.X_OK):
            env = next(l for l in text.splitlines() if l.startswith("Environment="))
            assert env.startswith('Environment="PROTON_DRIVE_CLI=') or \
                env.startswith("Environment=PROTON_DRIVE_CLI=")
        else:
            assert "PROTON_DRIVE_CLI=" not in text
    consume = tmp_path / "proton-consume.service"
    consume.write_text(realtime_manager.build_consume_service_text(path),
                       encoding="utf-8")
    monkeypatch.setattr(realtime_manager, "CONSUME_PATH", str(consume))
    assert realtime_manager.read_units_mappings_path() == path


# ---------- doctor ----------

def _doctor(args, home, env_extra=None, path_env=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("PROTON_DRIVE_CLI", "DBUS_SESSION_BUS_ADDRESS")}
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(Path(home) / ".config")
    if path_env is not None:
        env["PATH"] = path_env
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(REPO / "doctor.py")] + args,
                          cwd=str(REPO), env=env, capture_output=True,
                          text=True, timeout=120)


def _doctor_stubs(tmp_path, unit_text, journal_text):
    stub = tmp_path / "stubs"
    stub.mkdir()
    (stub / "unit.txt").write_text(unit_text, encoding="utf-8")
    (stub / "journal.txt").write_text(journal_text, encoding="utf-8")
    body = """#!/bin/sh
cmd=$(basename "$0")
d=$(dirname "$0")
case "$cmd" in
  systemctl)
    case "$2" in
      cat) cat "$d/unit.txt" ;;
      list-timers)
        printf '%s\\n' "$*" | grep -q -- '--all' \\
          && echo 'timers listed with --all' \\
          || echo 'timers missing --all'
        ;;
      *) echo 'ActiveState=inactive' ;;
    esac
    ;;
  journalctl) cat "$d/journal.txt" ;;
  loginctl) echo 'Linger=no' ;;
  busctl) echo 'Name=org.freedesktop.secrets' ;;
esac
exit 0
"""
    for name in ("systemctl", "journalctl", "loginctl", "busctl"):
        path = stub / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    # Stubs win over the real systemctl/journalctl. /bin stays so the
    # stubs can use cat and grep.
    return str(stub) + ":/usr/bin:/bin"


def test_doctor_redact_removes_emails_and_paths(tmp_path):
    home = tmp_path / "home" / "alice"
    (home / ".proton-drive-sync").mkdir(parents=True)
    docs = home / "My Docs"
    docs.mkdir(parents=True)
    mappings = docs / "mappings.json"
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "language": "en",
        "account_name": "alice@example.org",
        "proton_cli_path": "/opt/secret-tools/proton-drive",
    }), encoding="utf-8")
    mappings.write_text(json.dumps({"mappings": [{
        "type": "folder",
        "source": "/mnt/secret-nas/private photos",
        "dest_parent": "/my-files/Backups of bob@example.net",
        "allow_delete": False,
        "delete_mode": "trash",
    }]}), encoding="utf-8")
    (home / ".proton-drive-sync" / "last-run.json").write_text(json.dumps({
        "last_full": {"exit": 0, "source": "/mnt/secret-nas/private photos",
                      "user": "carol@example.com"}}), encoding="utf-8")
    sibling = str(home) + "x/secret"
    unit = (
        "[Service]\n"
        'ExecStart=/usr/bin/proton-drive-sync "%s"\n'
        'Environment="PROTON_DRIVE_CLI=/opt/secret-tools/proton-drive"\n'
    ) % mappings
    journal = (
        "alice@example.org uploaded /mnt/secret-nas/private photos\n"
        "also %s\n" % sibling
    )
    stubs = _doctor_stubs(tmp_path, unit, journal)
    extra = {"PROTON_SYNC_SETTINGS": str(settings)}
    plain = _doctor(["--mappings", str(mappings)], home, extra, path_env=stubs)
    assert plain.returncode == 0, plain.stderr
    assert "secret-nas" in plain.stdout
    assert "timers listed with --all" in plain.stdout
    redacted = _doctor(["--redact", "--mappings", str(mappings)], home, extra,
                       path_env=stubs)
    assert redacted.returncode == 0, redacted.stderr
    out = redacted.stdout + redacted.stderr
    for secret in ("alice@example.org", "bob@example.net", "carol@example.com",
                   "secret-nas", "private photos", "secret-tools",
                   "Docs", "x/secret", str(home), str(tmp_path), sibling):
        assert secret not in out, secret
    assert "mapping 1: type=folder" in redacted.stdout
    assert "allow_delete=False" in redacted.stdout
    assert "timers listed with --all" in redacted.stdout


def test_doctor_redact_does_not_migrate_legacy_home(tmp_path):
    home = tmp_path / "home" / "nizar"
    legacy = home / ".proton_sync"
    (legacy / "cache").mkdir(parents=True)
    marker = legacy / "cache" / "keep"
    marker.write_text("x", encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text('{"language": "en"}', encoding="utf-8")
    stubs = _doctor_stubs(tmp_path, "[Service]\n", "ok\n")
    result = _doctor(["--redact"], home,
                     {"PROTON_SYNC_SETTINGS": str(settings)}, path_env=stubs)
    assert result.returncode == 0, result.stderr
    assert str(home) not in result.stdout
    assert str(home) not in result.stderr
    assert legacy.is_dir()
    assert marker.is_file()
    assert not (home / ".proton-drive-sync").exists()


def test_gui_run_logs_not_under_app_dir():
    editor = (REPO / "proton_mapping_editor.py").read_text(encoding="utf-8")
    ui_run = (REPO / "ui" / "run.py").read_text(encoding="utf-8")
    config = (REPO / "config.py").read_text(encoding="utf-8")
    assert 'os.path.join(APP_DIR, "logs")' not in editor
    assert 'os.path.join(APP_DIR, "logs")' not in ui_run
    assert "RUN_LOG_DIR" in ui_run
    assert 'RUN_LOG_DIR = os.path.join(DATA_DIR, "logs")' in config
    assert 'DATA_DIR = os.path.expanduser("~/.proton-drive-sync")' in config
    assert "def _dialog_dir(" in editor
    assert "os.access(APP_DIR, os.W_OK)" in editor
    assert editor.count("_dialog_dir(") >= 4


def test_doctor_survives_missing_systemctl_and_cli(tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    settings = tmp_path / "settings.json"
    settings.write_text('{"language": "en"}', encoding="utf-8")
    result = _doctor([], home, {"PROTON_SYNC_SETTINGS": str(settings)},
                     path_env=str(empty))
    assert result.returncode == 0, result.stderr
    assert "systemctl: not available" in result.stdout
    assert "proton-drive --version: not available" in result.stdout
    assert "Secret Service" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


# ---------- packaging ----------

def test_pkgbuild_syntax():
    pkgbuild = ARCH / "PKGBUILD"
    assert pkgbuild.is_file()
    result = subprocess.run(["bash", "-n", str(pkgbuild)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    text = pkgbuild.read_text(encoding="utf-8")
    start = text.index("package_proton-drive-cli-sync-git() {")
    end = text.index("package_proton-drive-cli-sync-dolphin() {")
    body = text[start:end]
    # Destinations (${pkgdir}/usr/...) are not repository files.
    body = re.sub(r"\$\{pkgdir\}\S*", "", body)
    body = re.sub(r"\$\{_lib\}\S*", "", body)
    names = set(re.findall(
        r"[A-Za-z0-9_./-]+\.(?:py|png|md|json|desktop|install)\b", body))
    names |= set(re.findall(r"packaging/arch/launchers/[\w-]+", body))
    names |= {"LICENSE"} & set(re.findall(r"\bLICENSE\b", body))
    assert names, "package() references no files?"
    missing = [n for n in sorted(names)
               if "${" not in n and not (REPO / n).exists()]
    assert not missing, missing
    install = re.search(r"^install=(\S+)", text, re.MULTILINE).group(1)
    assert (ARCH / install).is_file()
    for glob_pattern in re.findall(r"in (locale/[^;\s]+)", body):
        assert list(REPO.glob(glob_pattern)), glob_pattern
    for glob_pattern in re.findall(r"\b(ui/[\w/]*\*\.py)\b", body):
        assert list(REPO.glob(glob_pattern)), glob_pattern
    assert "python-dbus" in text and "python-gobject" in text
    assert "arch=('x86_64')" in text
    assert "packaging/dolphin" in text
    assert (REPO / "packaging" / "dolphin" / "CMakeLists.txt").is_file()
    assert "proton-drive-cli-sync-dolphin" in text
    assert "check()" in text and "pytest" in text


def _pkgbuild_python_files():
    """Repository .py paths the git package installs, globs expanded."""
    text = (ARCH / "PKGBUILD").read_text(encoding="utf-8")
    start = text.index("package_proton-drive-cli-sync-git() {")
    end = text.index("package_proton-drive-cli-sync-dolphin() {")
    body = re.sub(r"\$\{(?:pkgdir|_lib)\}\S*", "", text[start:end])
    files = set()
    for word in re.findall(r"[A-Za-z0-9_./*-]+\.py\b", body):
        if word.startswith("packaging/"):
            continue
        if "*" in word:
            files |= {p.relative_to(REPO).as_posix() for p in REPO.glob(word)}
        else:
            files.add(word)
    return files


def _local_imports(path):
    """Repository modules (``a.py`` or ``a/b.py``) a file imports, at any depth."""
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.append(node.module)
            names += ["%s.%s" % (node.module, a.name) for a in node.names]
    found = set()
    for name in names:
        rel = name.replace(".", "/")
        for candidate in (rel + ".py", rel + "/__init__.py"):
            if (REPO / candidate).is_file():
                found.add(candidate)
        parts = rel.split("/")
        for i in range(1, len(parts)):
            init = "/".join(parts[:i]) + "/__init__.py"
            if (REPO / init).is_file():
                found.add(init)
    return found


def test_pkgbuild_installs_every_module_the_program_imports():
    installed = _pkgbuild_python_files()
    entry = {"ui/__main__.py", "proton_sync.py", "doctor.py", "cloudlaunch.py"}
    todo, seen = list(entry | installed), set()
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        todo += sorted(_local_imports(REPO / rel) - seen)
    missing = sorted(seen - installed)
    assert not missing, "imported but not installed by the PKGBUILD: %s" % missing


def test_pkgbuild_builds_main_of_the_project():
    text = (ARCH / "PKGBUILD").read_text(encoding="utf-8")
    branch = re.search(r"^_branch=(\S+)", text, re.MULTILINE).group(1)
    assert branch == "main"
    assert "#branch=${_branch}" in text
    assert "github.com/lafontaj/${_reponame}.git" in text


def test_gui_launcher_starts_qt_ui():
    text = (ARCH / "launchers" / "proton-drive-sync-gui").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0] == "#!/bin/sh"
    assert lines[1] == "cd %s || exit 1" % PACKAGED
    assert lines[2] == 'exec python3 -m ui "$@"'
    assert (REPO / "ui" / "__main__.py").is_file()


@pytest.mark.parametrize("launcher,script", [
    ("proton-drive-sync", "proton_sync.py"),
    ("proton-drive-sync-doctor", "doctor.py"),
    ("proton-drive-sync-cloud", "cloudlaunch.py"),
])
def test_launchers_exec_correct_scripts(launcher, script):
    text = (ARCH / "launchers" / launcher).read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    assert lines[0] == "#!/bin/sh"
    assert lines[1] == 'exec python3 %s/%s "$@"' % (PACKAGED, script)
    assert (REPO / script).is_file()
    assert unitexec.PACKAGED_DIR == PACKAGED
    assert unitexec.PACKAGED_LAUNCHERS.get("proton_sync.py") == "/usr/bin/proton-drive-sync"
    assert unitexec.PACKAGED_LAUNCHERS.get("cloudlaunch.py") == "/usr/bin/proton-drive-sync-cloud"


def test_cloud_unit_uses_the_engine_mappings_path(tmp_path):
    path = str(tmp_path / "my maps" / "mappings.json")
    engine = schedule_manager.build_service_text(path)
    cloud = schedule_manager.build_cloud_service_text(path)
    assert path in engine
    assert path in cloud
    assert "WantedBy=graphical-session.target" in cloud
    assert "cloudlaunch.py" in cloud or "proton-drive-sync-cloud" in cloud
    assert "proton_sync.py" not in cloud
    assert "proton-drive-sync " not in cloud


def test_cloudlaunch_exits_without_owning_the_bus(tmp_path, monkeypatch):
    import cloudlaunch
    import cloudproviders

    monkeypatch.setattr(schedule_manager, "read_service_mappings_path", lambda: None)
    called = []
    monkeypatch.setattr(cloudproviders, "main", lambda argv: called.append(argv) or 0)
    assert cloudlaunch.main([]) == 0
    assert called == []
    missing = str(tmp_path / "missing.json")
    assert cloudlaunch.main([missing]) == 0
    assert called == []


def test_cloudlaunch_forwards_an_existing_mappings_file(tmp_path, monkeypatch):
    import cloudlaunch
    import cloudproviders

    mappings = tmp_path / "mappings.json"
    mappings.write_text("{}", encoding="utf-8")
    called = []
    monkeypatch.setattr(
        cloudproviders, "main", lambda argv: called.append(list(argv)) or 0)
    assert cloudlaunch.main([str(mappings), "--data-dir", str(tmp_path)]) == 0
    assert called == [["--mappings", str(mappings), "--data-dir", str(tmp_path)]]


def test_nautilus_extension_reads_directory_status():
    text = (REPO / "packaging" / "nautilus" / "proton_drive_sync.py").read_text(
        encoding="utf-8")
    assert "GetDirectoryStatus" in text
    assert "add_emblem" in text
    assert "run_cli" not in text
    assert "proton_sync" not in text
    assert "emblem-default" not in text
