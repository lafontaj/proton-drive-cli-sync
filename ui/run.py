"""Lancement du moteur et filtre d'affichage, sans widget.

Les drapeaux, les balises stables (@@PROGRESS, [auth-failed], code 5) et
l'orchestration amorçage/réinitialisation suivent l'éditeur Tk. Un code 5
n'est pas présenté comme une réussite.
"""

import json
import os
import shlex
import subprocess
import sys
import time

try:
    from i18n import _
except ImportError:
    def _(s):
        return s

try:
    import config as appconfig
except ImportError:
    appconfig = None

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(APP_DIR, "proton_sync.py")

_STATUS_PREFIXES = (
    "===", "▶", "⏸", "✓", "✗", "❌", "⚠", "🔑", "🌱", "⟳",
    "♻", "🗑", "⛔", "📂", "==", "Terminé", "Done", "Erreur",
    "Error", "Cache", "Global", "▶ Mapping", "  ↪", "✅", "⏭",
    # Two-way lines: what came down, what failed, what was kept or held back.
    "[download", "[upload-failed]", "[list-skipped]", "[delete-guard]",
    "[held]", "[kept]", "[restore]", "[conflict",
)

# Tags that mean something was skipped or refused, without the word "failed".
_ERROR_TAGS = ("[list-skipped]", "[delete-guard]")

# Compteurs d'échec du JSON [run-result] : la liste de RunStats._FAILURES.
_FAILURE_COUNTERS = (
    "files_failed",
    "folders_listing_failed",
    "folders_unreadable",
    "folders_permission_denied",
    "folders_stall_skipped",
    "trash_failed",
    "deletions_refused",
    "sources_missing",
)


def cli_path():
    """Binaire Proton, relu à chaque appel (le réglage peut changer fenêtre ouverte)."""
    if appconfig is not None:
        return appconfig.resolve_proton_cli()
    return os.path.join(APP_DIR, "proton-drive")


def log_dir():
    if appconfig is not None:
        return appconfig.RUN_LOG_DIR
    return os.path.expanduser("~/.proton-drive-sync/logs")


def engine_env():
    env = dict(os.environ)
    env["PROTON_DRIVE_CLI"] = cli_path()
    return env


def live_pass_args(config_path, source):
    """Passe automatique du dossier Proton Drive.

    ``--delete`` est posé. Le mapping décide encore : ``allow_delete``,
    la corbeille, et la confirmation d'un dossier partagé.
    """
    sources = [source] if source else None
    return sync_args(
        config_path, dry_run=False, verify_hash=False, verbose=False,
        delete=True, only_sources=sources)


def sync_args(config_path, dry_run=False, verify_hash=False, verbose=False,
              delete=False, only_sources=None, allow_mass_delete=False):
    """Arguments du moteur pour une synchro manuelle. Sans sélection : tout le fichier."""
    args = [config_path]
    if dry_run:
        args.append("--dry-run")
    if verify_hash:
        args.append("--verify-hash")
    if verbose:
        args.append("-v")
    if delete:
        args.append("--delete")
    if allow_mass_delete:
        args.append("--allow-mass-delete")
    for source in only_sources or []:
        args += ["--only-source", source]
    return args


def prime_args(config_path, sources):
    """--delete est toujours passé : le moteur l'applique mapping par mapping."""
    args = [config_path, "--delete", "-v", "--accept-account-change"]
    for source in sources:
        args += ["--only-source", source]
    return args


def reset_args(config_path, sources, wipe=False):
    args = [config_path, "-v", "--accept-account-change"]
    for source in sources:
        args += ["--reset-source", source]
    if wipe:
        args.append("--wipe-remote")
    return args


def engine_cmd(args):
    return [sys.executable, ENGINE] + list(args)


def shell_command(args, log_path):
    """Commande copiable. Reflète ce que le bouton lancerait."""
    return (
        f"PROTON_DRIVE_CLI={shlex.quote(cli_path())} "
        f"{shlex.quote(sys.executable)} {shlex.quote(ENGINE)} "
        + " ".join(shlex.quote(a) for a in args)
        + f" 2>&1 | tee {shlex.quote(log_path)}"
    )


def scope_line(only_sources, mapping_count):
    if only_sources:
        text = _("{n} selected mapping(s)").format(n=len(only_sources))
        line = _("Scope: {s}").format(s=text)
        line += "\n  • " + "\n  • ".join(os.path.basename(s) for s in only_sources)
        return line
    text = _("all {n} mapping(s)").format(n=mapping_count)
    return _("Scope: {s}").format(s=text)


def deletion_confirm_text(eff_mappings, scope):
    """Texte de confirmation si --delete est réel. None si rien à confirmer n'est calculé ici :
    l'appelant n'appelle cette fonction que lorsque la case est cochée hors dry-run.
    """
    permanent = [m for m in eff_mappings
                 if m.get("allow_delete") and m.get("delete_mode") == "permanent"]
    any_delete = any(m.get("allow_delete") for m in eff_mappings)
    if not any_delete:
        return "info", scope + "\n\n" + _(
            "“Propagate deletions” is checked, but no mapping in scope "
            "allows deletion (set it via Edit). Nothing will be "
            "deleted.\n\nRun anyway?")
    if permanent:
        names = "\n  • ".join(os.path.basename(m["source"]) for m in permanent)
        return "warning", scope + "\n\n" + _(
            "You are launching a sync with “Propagate deletions”.\n\n"
            "⚠  {n} mapping(s) are in PERMANENT mode "
            "(deletion without trash, IRREVERSIBLE):\n  • {names}"
            "\n\nThe other mappings allowing deletion will go to the "
            "trash, where they stay recoverable until you empty it.\n\n"
            "Tip: a “Test (dry-run)” first shows what would be "
            "deleted.\n\nRun anyway?").format(n=len(permanent), names=names)
    return "question", scope + "\n\n" + _(
        "You are launching a sync with “Propagate deletions”.\n\n"
        "For the mappings that allow deletion, what was deleted "
        "locally will be sent to the Proton trash "
        "(recoverable until you empty the trash, which never "
        "empties itself).\n\n"
        "Tip: a “Test (dry-run)” first shows what would be "
        "deleted.\n\nRun?")


def is_error_line(stripped):
    """Erreur ou avertissement. Les mots-clés ne sont cherchés que avant le premier /."""
    if not stripped:
        return False
    if any(glyph in stripped for glyph in ("❌", "⛔", "⚠")):
        return True
    # mount_check also reports a healthy verdict under [delete-guard] (verbose).
    if stripped.startswith(_ERROR_TAGS) and not stripped.startswith("[delete-guard] OK"):
        return True
    head = stripped.split("/", 1)[0].lower()
    return ("erreur" in head or "error" in head
            or "échec" in head or "echec" in head or "failed" in head)


def is_status_line(stripped):
    if stripped.startswith(_STATUS_PREFIXES):
        return True
    low = stripped.lower()
    return ("mapping " in low and "/" in stripped and "=>" in stripped) \
        or "error" in low or "erreur" in low or "❌" in stripped


def visible_text(line, verbose, errors_only):
    """Texte à afficher, ou None si le filtre le masque. Compte les dossiers à part.

    Filtre ligne à ligne, sans le cas du résumé (voir output_step).
    [run-result] n'est jamais affiché. « Erreurs seules » prime sur « Détaillé »."""
    if line.strip().startswith("[run-result]"):
        return None
    if errors_only:
        stripped = line.strip()
        if is_error_line(stripped):
            return line if line.endswith("\n") else line + "\n"
        return None
    if verbose:
        return line if line.endswith("\n") else line + "\n"
    stripped = line.strip()
    if not stripped:
        return None
    if is_status_line(stripped):
        return line if line.endswith("\n") else line + "\n"
    return None


def run_result_has_failures(line):
    """True si le JSON [run-result] a au moins un compteur d'échec non nul."""
    raw = line.strip()
    prefix = "[run-result] "
    if not raw.startswith(prefix):
        return False
    try:
        payload = json.loads(raw[len(prefix):])
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    for name in _FAILURE_COUNTERS:
        try:
            if int(payload.get(name) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _hold_for_summary(line, verbose, errors_only):
    """True si la ligne attend la suivante avant d'être affichée.

    Le résumé est la ligne juste avant [run-result]. Une ligne de statut ou une
    erreur à glyphe n'est pas retenue : le balayage reste immédiat."""
    if verbose and not errors_only:
        return False
    stripped = line.strip()
    if not stripped or stripped.startswith("[run-result]"):
        return False
    if any(glyph in stripped for glyph in ("❌", "⛔", "⚠")):
        return False
    if not errors_only and is_status_line(stripped):
        return False
    return True


def output_step(pending, line, verbose, errors_only):
    """Une ligne du moteur. Retourne (ligne retenue, textes à afficher).

    La vue par défaut et « erreurs seules » ne montrent le résumé que si un
    compteur d'échec de [run-result] est non nul. « Détaillé » seul le montre."""
    if line.strip().startswith("[run-result]"):
        shown = []
        if pending is not None and (run_result_has_failures(line)
                                    or (verbose and not errors_only)):
            shown.append(pending if pending.endswith("\n") else pending + "\n")
        return None, shown
    shown = []
    if pending is not None:
        text = visible_text(pending, verbose, errors_only)
        if text:
            shown.append(text)
    if _hold_for_summary(line, verbose, errors_only):
        return line, shown
    text = visible_text(line, verbose, errors_only)
    if text:
        shown.append(text)
    return None, shown


def visible_lines(lines, verbose, errors_only):
    """Textes affichés pour ce flux (rejeu du journal). [run-result] en est absent."""
    pending = None
    shown = []
    for line in lines:
        pending, step = output_step(pending, line, verbose, errors_only)
        shown.extend(step)
    if pending is not None:
        text = visible_text(pending, verbose, errors_only)
        if text:
            shown.append(text)
    return shown


def parse_progress(line):
    """Texte d'envoi, chaîne vide pour masquer, None si ce n'est pas @@PROGRESS."""
    if not line.lstrip().startswith("@@PROGRESS"):
        return None
    fields = {}
    try:
        for tok in line.split()[1:]:
            if "=" in tok:
                key, value = tok.split("=", 1)
                fields[key] = value
    except Exception:
        return None
    state = fields.get("state")
    if state == "done":
        return ""
    if state != "start":
        return None
    files = fields.get("files", "?")
    try:
        gb = int(fields.get("bytes", "0")) / (1024 ** 3)
        size = _("{gb:.2f} GB").format(gb=gb) if gb >= 0.01 else _("< 0.01 GB")
    except (TypeError, ValueError):
        size = "?"
    return _("Uploading — {n} file(s), {size}").format(n=files, size=size)


def first_reason(counters):
    """Première raison d'un code 5, en clair, ou "" si on ne sait pas."""
    if not counters:
        return ""
    try:
        import proton_sync
        reasons = proton_sync.failure_reasons(counters)
    except Exception:
        return ""
    return reasons[0] if reasons else ""


def sync_status(code, counters=None):
    """Libellé de fin. Le code 5 n'est pas une réussite et dit ce qui a manqué."""
    if code == 5:
        reason = first_reason(counters)
        if reason:
            return _("Sync finished with problems: {r}").format(r=reason)
        return _("Sync finished with failures (code 5).")
    return _("Sync finished (code {c}).").format(c=code)


def parse_run_result(line):
    """Compteurs de la ligne [run-result], ou None."""
    marker = "[run-result] "
    if not line.startswith(marker):
        return None
    try:
        data = json.loads(line[len(marker):])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def finished_banner(kind, code, failures, log_path):
    if kind == "sync":
        if code == 5:
            return _("=== Finished with failures (code 5) — log: {p} ===").format(p=log_path)
        return _("=== Finished (code {c}) — log: {p} ===").format(c=code, p=log_path)
    if code == 5:
        if kind == "reset":
            return _("Reset finished with failures (code 5)")
        return _("Priming finished with failures (code 5)")
    if kind == "reset":
        if failures:
            return _("Reset finished (code {c}) — but some files failed; those "
                     "folders will be retried").format(c=code)
        return _("Reset finished (code {c})").format(c=code)
    if failures:
        return _("Priming finished (code {c}) — but some files failed; those "
                 "folders will be retried").format(c=code)
    return _("Priming finished (code {c})").format(c=code)


def lock_is_busy(config_path, env):
    """True si une autre passe tient le verrou. Une sonde ratée ne bloque pas."""
    try:
        result = subprocess.run(
            [sys.executable, ENGINE, config_path, "--check-lock"],
            env=env, capture_output=True, text=True, timeout=15)
        return result.returncode != 0
    except (OSError, subprocess.SubprocessError):
        return False


def _emit_line(control, line, log_handle):
    """Affiche selon le filtre et écrit le journal, sauf @@PROGRESS."""
    progress = parse_progress(line)
    if progress is not None:
        control.on_progress(progress)
        return
    if line.lstrip().startswith("📂"):
        control.folders_shown += 1
    if "[auth-failed]" in line:
        control.auth_failed = True
    result = parse_run_result(line)
    if result is not None:
        control.result = result
    if "[upload-failed]" in line:
        control.upload_failed = True
    if "[delete-guard]" in line and "refusing to trash" in line:
        control.mass_refused = True
    marker = "[unreadable] "
    index = line.find(marker)
    if index >= 0:
        path = line[index + len(marker):].strip()
        if path and path not in control.unreadable:
            control.unreadable.append(path)
    if "[account-changed]" in line and "→" not in line:
        control.on_status(_(
            "Proton account changed — prime the cache (or "
            "reset the mappings) to rebuild on the new "
            "account."))
    control.pending, shown = output_step(
        control.pending, line, control.verbose, control.errors_only)
    for text in shown:
        control.on_text(text)
    if log_handle is not None and not line.startswith("@@PROGRESS"):
        log_handle.write(line if line.endswith("\n") else line + "\n")
        log_handle.flush()


def _flush_pending(control):
    """Fin du flux : la ligne retenue passe par le filtre ordinaire."""
    pending, control.pending = control.pending, None
    if pending is not None:
        text = visible_text(pending, control.verbose, control.errors_only)
        if text:
            control.on_text(text)


def _pump(proc, control, log_handle):
    control.proc = proc
    try:
        for line in proc.stdout:
            if control.stop:
                break
            _emit_line(control, line, log_handle)
        _flush_pending(control)
        proc.wait()
    finally:
        control.proc = None
    return proc.returncode


def run_sync(cmd, log_path, env, control):
    """Passe manuelle. ``control`` porte verbose, errors_only, stop, et les callbacks."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    control.folders_shown = 0
    control.pending = None
    control.auth_failed = False
    control.upload_failed = False
    control.unreadable = []
    control.result = None
    with open(log_path, "w", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        code = _pump(proc, control, log_handle)
    if (not control.verbose and control.folders_shown == 0 and code == 0):
        control.on_text(_("  ✓ Nothing to update — everything is already in sync.") + "\n")
    control.on_text("\n" + finished_banner("sync", code, code == 5, log_path) + "\n\n")
    control.on_progress("")
    control.on_status(sync_status(code, control.result))
    control.on_auth(not control.auth_failed)
    return code


def _restart_background(control):
    try:
        import realtime_manager
    except ImportError:
        return
    control.on_text(_("▶ Restarting the consumer…") + "\n")
    try:
        realtime_manager.start_consumer()
    except Exception:
        pass
    try:
        import schedule_manager
        schedule_manager.resume_timer()
        control.on_text(_("▶ Scheduled timer restored.") + "\n")
    except Exception:
        pass


def run_orchestrated(cmd, config_path, log_path, env, control, kind):
    """Amorçage ou réinitialisation : session, pause du consommateur, moteur, reprise.

    Le filet du ``finally`` relance le consommateur si l'arrêt a eu lieu
    mais pas la reprise normale (annulation pendant l'attente du verrou).
    """
    is_reset = kind == "reset"
    if is_reset:
        header = _("Resetting the mapping(s)")
        abort_status = _("Reset aborted: sign in to Proton first.")
    else:
        header = _("Priming the cache")
        abort_status = _("Priming aborted: sign in to Proton first.")
    daemons_stopped = False
    code = None
    try:
        control.on_text("\n=== " + header + " ===\n")
        try:
            import realtime_manager
        except ImportError:
            realtime_manager = None
            control.on_text(_("[realtime_manager.py missing — cannot orchestrate daemons]") + "\n")
        if realtime_manager is not None:
            control.on_text(_("🔑 Checking Proton session…") + "\n")
            ok = False
            try:
                ok = bool(realtime_manager.check_auth())
                if not ok:
                    time.sleep(2.5)
                    ok = bool(realtime_manager.check_auth())
            except Exception:
                ok = False
            if not ok:
                control.on_text(_(
                    "❌ Proton session unavailable — sign in "
                    "first (button “Sign in to Proton”), then prime again.") + "\n")
                control.on_status(abort_status)
                return None
            control.on_text(_("   ✓ session OK") + "\n")
            control.on_text(_("⏸ Stopping the consumer (watcher kept running)…") + "\n")
            realtime_manager.stop_consumer()
            daemons_stopped = True
            control.on_text(_("⏸ Pausing the scheduled timer…") + "\n")
            try:
                import schedule_manager
                schedule_manager.pause_timer()
            except Exception:
                pass

        control.stop = False
        waited = False
        while lock_is_busy(config_path, env):
            if control.stop:
                control.on_text(_("⏹ Cancelled while waiting for the lock.") + "\n")
                control.on_status(_("Reset cancelled.") if is_reset else _("Priming cancelled."))
                return None
            if not waited:
                control.on_text(_(
                    "⏳ Waiting for the lock to be released "
                    "(another pass is running)… (Stop to cancel)") + "\n")
                waited = True
            time.sleep(3)
        if waited:
            control.on_text(_("🔓 Lock released — starting.") + "\n")

        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        control.folders_shown = 0
        control.pending = None
        control.auth_failed = False
        control.upload_failed = False
        control.unreadable = []
        control.result = None
        with open(log_path, "w", encoding="utf-8") as log_handle:
            proc = subprocess.Popen(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            code = _pump(proc, control, log_handle)
        failures = control.upload_failed or code == 5
        control.on_text("\n=== " + finished_banner(kind, code, failures, log_path) + " ===\n")
        control.on_progress("")
        control.on_auth(not control.auth_failed)
        if realtime_manager is not None:
            _restart_background(control)
            daemons_stopped = False
            try:
                ready, total = realtime_manager.mappings_ready_count(config_path)
                control.on_text(
                    _("✓ {r}/{t} mapping(s) now ready for real-time.").format(
                        r=ready, t=total) + "\n\n")
                if code == 5:
                    control.on_status(sync_status(code, control.result))
                elif is_reset:
                    control.on_status(_(
                        "Reset done — {r}/{t} mapping(s) ready for real-time.").format(
                            r=ready, t=total))
                else:
                    control.on_status(_(
                        "Priming done — {r}/{t} mapping(s) ready for real-time.").format(
                            r=ready, t=total))
            except Exception:
                control.on_status(
                    sync_status(code, control.result) if code is not None else "")
        else:
            control.on_status(
                sync_status(code, control.result) if code is not None else "")
        return code
    except Exception as exc:
        if is_reset:
            control.on_text("\n=== " + _("Reset error: {e}").format(e=exc) + " ===\n")
            control.on_status(_("Reset error: {e}").format(e=exc))
        else:
            control.on_text("\n=== " + _("Priming error: {e}").format(e=exc) + " ===\n")
            control.on_status(_("Priming error: {e}").format(e=exc))
        return code
    finally:
        if daemons_stopped:
            _restart_background(control)


class PassControl:
    """État d'une passe, lu par le fil du moteur et écrit par les boutons."""

    def __init__(self):
        self.stop = False
        self.proc = None
        self.verbose = False
        self.errors_only = False
        self.folders_shown = 0
        self.pending = None
        self.unreadable = []
        self.auth_failed = False
        self.upload_failed = False
        self.mass_refused = False
        self.result = None
        self.on_text = lambda _s: None
        self.on_status = lambda _s: None
        self.on_progress = lambda _s: None
        self.on_auth = lambda _ok: None


def request_stop(control):
    """Demande l'arrêt. Le verrou d'attente et le processus en cours s'arrêtent."""
    control.stop = True
    proc = getattr(control, "proc", None)
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        control.on_text("\n" + _("=== Interruption requested (terminate) ===") + "\n")
        control.on_status(_("Sync interrupted."))
