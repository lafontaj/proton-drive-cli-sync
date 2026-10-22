"""The fake CLI must accept exactly what the installed Proton CLI accepts.

Runs `proton-drive filesystem <verb> --help` (read-only, no account needed)
for every subcommand the code calls, then checks that:
  * every option and strategy value the engine passes to the fake CLI is one
    the real CLI lists;
  * the fake CLI rejects every strategy value the real one does not list.
Skipped when the real CLI is not installed.
"""
import os
import re
import shutil
import subprocess

import pytest

VERBS = ("upload", "download", "list", "info", "create-folder", "trash")
_STRATEGY_OPTIONS = ("-f", "-d")
# Every strategy word known to any Proton CLI subcommand, to probe the fake.
_PROBES = ("create-new-revision", "rename", "replace", "skip", "merge",
           "remove", "keep-both", "overwrite", "bogus")


def _real_cli():
    explicit = os.environ.get("PROTON_DRIVE_CLI_REAL")
    path = explicit or shutil.which("proton-drive")
    return path if path and os.access(path, os.X_OK) else None


@pytest.fixture(scope="module")
def real_help():
    cli = _real_cli()
    if not cli:
        pytest.skip("the real proton-drive CLI is not installed")
    out = {}
    for verb in VERBS:
        result = subprocess.run(
            [cli, "filesystem", verb, "--help"], capture_output=True,
            text=True, timeout=30, stdin=subprocess.DEVNULL)
        text = result.stdout + result.stderr
        if "Usage:" not in text:
            pytest.skip("cannot read the help of the real CLI: " + text[:200])
        out[verb] = _parse_help(text)
    return out


def _parse_help(text):
    """Return {"options": {flag: [aliases]}, "values": {short flag: set}}."""
    options, values = set(), {}
    current = None
    for line in text.splitlines():
        match = re.match(r"\s{2,}(-\w)(?:, (--[\w-]+))?", line)
        if match:
            current = match.group(1)
            options.add(match.group(1))
            if match.group(2):
                options.add(match.group(2))
            values[current] = set()
            continue
        bullet = re.match(r"\s+\* ([\w-]+):", line)
        if bullet and current:
            values[current].add(bullet.group(1))
        elif line and not line.startswith(" "):
            current = None
    return {"options": options, "values": values}


def _engine_calls(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Two/a.txt": (b"AAAA", 1_000_000_000)})
    mapping = {"type": "folder", "source": str(src / "Two"),
               "dest_parent": "/my-files/TW", "direction": "twoway",
               "allow_delete": True, "source_kind": "local"}
    cfg = write_mappings([], sync_mappings=[mapping])
    assert engine(cfg).returncode == 0
    fake_drive.seed_file("/my-files/TW/Two/b.txt", b"B", mtime=1_000_000_100)
    assert engine(cfg).returncode == 0           # a download
    os.remove(src / "Two" / "a.txt")
    assert engine(cfg, "--delete").returncode == 0   # a trash
    return fake_drive.calls()


def test_the_engine_only_passes_what_the_real_cli_accepts(
        real_help, fake_drive, local_tree, write_mappings, engine):
    calls = _engine_calls(fake_drive, local_tree, write_mappings, engine)
    seen = set()
    for argv in calls:
        if len(argv) < 2 or argv[0] != "filesystem" or argv[1] not in real_help:
            continue
        verb, help_ = argv[1], real_help[argv[1]]
        seen.add(verb)
        args = argv[2:]
        if "--" in args:
            args = args[:args.index("--")]
        i = 0
        while i < len(args):
            token = args[i]
            if token.startswith("-") and token != "-j":
                assert token in help_["options"], (
                    "`filesystem %s` does not list %s: %r" % (verb, token, argv))
                short = token if len(token) == 2 else {
                    "--file-conflict-strategy": "-f",
                    "--folder-conflict-strategy": "-d"}.get(token)
                if short in _STRATEGY_OPTIONS and i + 1 < len(args):
                    assert args[i + 1] in help_["values"].get(short, ()), (
                        "`filesystem %s %s` does not accept %r (real CLI: %s)"
                        % (verb, token, args[i + 1],
                           sorted(help_["values"].get(short, ()))))
                    i += 1
            i += 1
    assert {"upload", "download", "trash"} <= seen, (seen, calls)


@pytest.mark.parametrize("verb", ("upload", "download"))
@pytest.mark.parametrize("flag", _STRATEGY_OPTIONS)
def test_the_fake_cli_rejects_what_the_real_cli_rejects(
        real_help, fake_drive, tmp_path, verb, flag):
    real_values = real_help[verb]["values"][flag]
    assert real_values, (verb, flag)
    (tmp_path / "a.txt").write_bytes(b"A")
    fake_drive.seed_file("/my-files/a.txt", b"R", mtime=1_000_000_000)
    dest = tmp_path / "down"
    dest.mkdir()
    for probe in _PROBES:
        if verb == "upload":
            args = ["filesystem", "upload", flag, probe,
                    str(tmp_path / "a.txt"), "/my-files"]
        else:
            args = ["filesystem", "download", flag, probe,
                    "/my-files/a.txt", str(dest)]
        result = fake_drive.run(*args)
        rejected = "Invalid conflict strategy" in result.stderr
        assert rejected == (probe not in real_values), (
            verb, flag, probe, sorted(real_values), result.stderr)
