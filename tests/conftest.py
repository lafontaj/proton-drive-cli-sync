"""Isolation and the fake proton-drive harness. See docs/dev/TESTING.md."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FAKES = Path(__file__).resolve().parent / "fakes"
_REAL_HOME = os.path.expanduser("~")
_REAL_DATA = os.path.join(_REAL_HOME, ".proton-drive-sync")
_REAL_DATA_BEFORE = os.path.exists(_REAL_DATA)
_REPO_SETTINGS = REPO / "settings.json"
_REPO_SETTINGS_BEFORE = _REPO_SETTINGS.exists()

# Any import of config.py creates ~/.proton-drive-sync. Point the pytest
# process at a throwaway home before test modules are imported.
_PYTEST_HOME = tempfile.mkdtemp(prefix="proton-sync-pytest-")
os.environ["HOME"] = _PYTEST_HOME
os.environ["XDG_CONFIG_HOME"] = os.path.join(_PYTEST_HOME, ".config")
os.environ["XDG_STATE_HOME"] = os.path.join(_PYTEST_HOME, ".local", "state")
os.environ["XDG_CACHE_HOME"] = os.path.join(_PYTEST_HOME, ".cache")
_PYTEST_SETTINGS = os.path.join(_PYTEST_HOME, "settings.json")
with open(_PYTEST_SETTINGS, "w", encoding="utf-8") as _settings_handle:
    _settings_handle.write('{"language": "en"}\n')
os.environ["PROTON_SYNC_SETTINGS"] = _PYTEST_SETTINGS

sys.path.insert(0, str(FAKES))
import remote_state  # noqa: E402


class LocalTree:
    """Build a source tree and rewrite files with an explicit mtime."""

    def __init__(self, root):
        self.root = root

    def write(self, rel, data, mtime):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        stamp = _mtime_ns(mtime)
        os.utime(path, ns=(stamp, stamp))
        return path

    def __call__(self, spec):
        for rel, value in (spec or {}).items():
            if isinstance(value, tuple):
                data, mtime = value
            else:
                data, mtime = value, 1_000_000_000
            self.write(rel, data, mtime)
        return self.root


def _mtime_ns(mtime):
    """Seconds (the Appendix A style) or an already-absolute nanosecond stamp."""
    number = int(mtime)
    if number < 10**15:
        return number * 1_000_000_000
    return number


class FakeDrive:
    def __init__(self, state_path, cli):
        self.state_path = state_path
        self.cli = cli

    def _load(self):
        return remote_state.load(str(self.state_path))

    def seed_file(self, remote_path, data, mtime=1_000_000_000):
        def mutate(state):
            remote_state.store_file(state, remote_path, data, int(mtime))
            # seed is not an upload the engine performed
            if state["uploads"] and state["uploads"][-1] == remote_state.normalize(remote_path):
                state["uploads"].pop()
            node = state["nodes"][remote_state.normalize(remote_path)]
            node["revisions"] = 1
        self._update(mutate)

    def seed_folder(self, remote_path):
        def mutate(state):
            path = remote_state.normalize(remote_path)
            remote_state.ensure_folders(state, path + "/placeholder")
            state["nodes"].setdefault(path, {"type": "folder"})
        self._update(mutate)

    def content(self, remote_path):
        return remote_state.file_bytes(self._load(), remote_path)

    def listing(self, remote_path):
        return [name for name, _node in remote_state.direct_children(self._load(), remote_path)]

    def calls(self):
        return list(self._load().get("calls") or [])

    def upload_cwds(self):
        return list(self._load().get("upload_cwds") or [])

    def uploads(self):
        return list(self._load().get("uploads") or [])

    def add_fault(self, **fault):
        def mutate(state):
            state.setdefault("faults", []).append(dict(fault))
        self._update(mutate)

    def trashed(self, remote_path):
        node = self._load()["nodes"].get(remote_state.normalize(remote_path))
        return bool(isinstance(node, dict) and node.get("trashed"))

    def set_version(self, version):
        text = "Proton Drive CLI cli-drive@{v}+fake\n".format(v=version)

        def mutate(state):
            state["version_text"] = text
        self._update(mutate)

    def revisions(self, remote_path):
        node = self._load()["nodes"].get(remote_state.normalize(remote_path))
        if not isinstance(node, dict):
            return None
        return node.get("revisions")

    def run(self, *args, cwd=None):
        env = os.environ.copy()
        env["FAKE_PROTON_STATE"] = str(self.state_path)
        return subprocess.run(
            [str(self.cli), *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )

    def _update(self, mutate):
        import fcntl
        lock_path = str(self.state_path) + ".lock"
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = remote_state.load(str(self.state_path))
                mutate(state)
                remote_state.save(str(self.state_path), state)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _install_fake(dest_dir):
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in ("fake_proton_drive.py", "remote_state.py"):
        target = dest_dir / name
        shutil.copy(FAKES / name, target)
    cli = dest_dir / "fake_proton_drive.py"
    cli.chmod(0o755)
    return cli


@pytest.fixture(scope="session", autouse=True)
def _real_home_untouched():
    """The suite must not create the real data dir or a repo-root settings file."""
    yield
    if not _REAL_DATA_BEFORE and os.path.exists(_REAL_DATA):
        raise AssertionError("test session created " + _REAL_DATA)
    if not _REPO_SETTINGS_BEFORE and _REPO_SETTINGS.exists():
        raise AssertionError("test session created settings.json in the repo root")
    shutil.rmtree(_PYTEST_HOME, ignore_errors=True)


@pytest.fixture
def isolated_home(tmp_path):
    home = tmp_path / "home"
    for relative in ("", ".config", ".local/state", ".cache"):
        (home / relative).mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, isolated_home, tmp_path):
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_home / ".local" / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_home / ".cache"))
    monkeypatch.delenv("PROTON_SYNC_DEBUG", raising=False)
    settings = tmp_path / "isolate-settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("PROTON_SYNC_SETTINGS", str(settings))
    for module_name, attr in (("config", "_SETTINGS_PATH"), ("i18n", "SETTINGS_PATH")):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, attr):
            monkeypatch.setattr(module, attr, str(settings))
    for module_name, names in (
        ("config", ("LOCK_FILE", "CACHE_DIR", "FAILURES_LOG", "RENAMED_LOG",
                    "HEALTH_FILE", "LAST_RUN_FILE")),
        ("proton_sync", ("LOCK_FILE", "CACHE_DIR", "FAILURES_LOG", "RENAMED_LOG",
                         "HEALTH_FILE", "LAST_RUN_FILE", "CLI_VERSION_CACHE")),
    ):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        base = isolated_home / ".proton-drive-sync"
        mapping = {
            "LOCK_FILE": base / "proton_sync.lock",
            "CACHE_DIR": base / "cache",
            "FAILURES_LOG": base / "failures.log",
            "RENAMED_LOG": base / "renamed-extensions.log",
            "HEALTH_FILE": base / "health.json",
            "LAST_RUN_FILE": base / "last-run.json",
            "CLI_VERSION_CACHE": base / "cli-version.json",
        }
        for name in names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, str(mapping[name]))
    yield


@pytest.fixture
def fake_drive(tmp_path, monkeypatch):
    state_path = tmp_path / "remote.json"
    remote_state.save(str(state_path), remote_state.default_state())
    cli = _install_fake(tmp_path / "bin")
    monkeypatch.setenv("FAKE_PROTON_STATE", str(state_path))
    monkeypatch.setenv("PROTON_DRIVE_CLI", str(cli))
    return FakeDrive(state_path, cli)


@pytest.fixture
def local_tree(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    return LocalTree(root)


@pytest.fixture
def write_mappings(tmp_path):
    def _write(mappings, exclusions=None):
        document = {"mappings": mappings}
        if exclusions is not None:
            document["exclusions"] = exclusions
        path = tmp_path / "mappings.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path
    return _write


class EngineRunner:
    """Callable engine launch, plus a way to write this test's settings.json."""

    def __init__(self, settings, isolated_home, fake_drive):
        self.settings = settings
        self.isolated_home = isolated_home
        self.fake_drive = fake_drive

    def update_settings(self, **values):
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        data.update(values)
        self.settings.write_text(json.dumps(data), encoding="utf-8")

    def __call__(self, mappings, *args, settings_doc=None):
        if settings_doc is not None:
            self.settings.write_text(json.dumps(settings_doc) + "\n", encoding="utf-8")
        cli = os.environ.get("PROTON_DRIVE_CLI")
        if not cli:
            raise RuntimeError(
                "PROTON_DRIVE_CLI is unset; refusing to launch the engine "
                "(it would look for a real proton-drive binary)"
            )
        env = os.environ.copy()
        env["HOME"] = str(self.isolated_home)
        env["XDG_CONFIG_HOME"] = str(self.isolated_home / ".config")
        env["XDG_STATE_HOME"] = str(self.isolated_home / ".local" / "state")
        env["XDG_CACHE_HOME"] = str(self.isolated_home / ".cache")
        env["PROTON_SYNC_SETTINGS"] = str(self.settings)
        env["PROTON_DRIVE_CLI"] = cli
        env["FAKE_PROTON_STATE"] = str(self.fake_drive.state_path)
        env["LANG"] = "C.UTF-8"
        env["LC_ALL"] = "C.UTF-8"
        env.pop("PROTON_SYNC_DEBUG", None)
        return subprocess.run(
            [sys.executable, str(REPO / "proton_sync.py"), str(mappings), *map(str, args)],
            cwd=str(REPO),
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )


@pytest.fixture
def engine(fake_drive, isolated_home, tmp_path):
    """Run proton_sync.py in a subprocess. Refuses to start without the fake CLI."""
    settings = tmp_path / "engine-settings.json"
    settings.write_text('{"language": "en"}\n', encoding="utf-8")
    return EngineRunner(settings, isolated_home, fake_drive)
