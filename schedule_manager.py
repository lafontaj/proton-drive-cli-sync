#!/usr/bin/env python3
"""
Gestion de la planification systemd (timer --user) pour la synchro Proton Drive.

Encapsule toute la logique de lecture/écriture des fichiers service+timer et les
appels `systemctl --user`, pour que le GUI n'ait qu'à appeler des méthodes
claires. Conçu pour tourner SANS privilèges (session utilisateur courante).

Limite connue : `loginctl enable-linger` exige sudo et N'est PAS gérée ici. On se
contente de LIRE l'état du linger et de rappeler la commande à l'utilisateur.

Tout est centré sur l'utilisateur courant : chaque GUI gère la planification
de son propre utilisateur (sessions et homes séparés).
"""
__version__ = "1.3.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import os
import re
import sys
import json
import datetime
import argparse
import subprocess

# i18n (import guardé : l'absence de i18n.py n'empêche rien — les
# messages restent alors en anglais, la langue source).
try:
    from i18n import _
except ImportError:
    def _(s):
        return s

import unitexec  # guillemets/échappement systemd

# Réglages d'installation (chemin CLI...) : une SEULE source de vérité
# partagée par le moteur, le GUI et les démons. Import tolérant.
try:
    import config as appconfig
    _HAS_CONFIG = True
except ImportError:
    _HAS_CONFIG = False

SYSTEMD_USER_DIR = os.path.expanduser("~/.config/systemd/user")
SERVICE_NAME = "proton-sync.service"
TIMER_NAME = "proton-sync.timer"
CLOUD_SERVICE_NAME = "proton-drive-sync-cloud.service"
SERVICE_PATH = os.path.join(SYSTEMD_USER_DIR, SERVICE_NAME)
TIMER_PATH = os.path.join(SYSTEMD_USER_DIR, TIMER_NAME)

# Chemins écrits dans l'unité systemd générée. Ils valaient auparavant
# « %h/Logiciels/Proton-drive/… » : `%h` est bien résolu par systemd, mais le
# reste était l'arborescence d'UN poste, transposée chez tout le monde. Une
# installation ailleurs produisait une unité pointant vers un dossier
# inexistant. APP_DIR est l'emplacement RÉEL de ce module, donc du projet.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CLI = os.path.join(APP_DIR, "proton-drive")
DEFAULT_ENGINE = os.path.join(APP_DIR, "proton_sync.py")


def _run(args):
    """Lance une commande et retourne (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)


# ---------- Génération des fichiers ----------

def build_service_text(mappings_path, delete=False, engine_command=None,
                       cli_line=None, omit_cli=False):
    """Génère le contenu du fichier .service pointant vers le fichier de mappings
    donné. Si delete=True, ajoute --delete à l'ExecStart (Option B).

    Le même ExecStart est le passage nocturne des mappings twoway : le moteur
    lit ``direction`` dans le JSON. Le texte d'unité n'est généré qu'ici.

    engine_command / cli_line / omit_cli ne servent qu'à la réécriture
    automatique : le bouton Installer / Mettre à jour les laisse vides, et les
    chemins viennent alors du dossier qui tourne. Une réécriture qui les
    fournit recopie l'interpréteur (ou le lanceur), proton_sync.py et
    PROTON_DRIVE_CLI déjà installés.
    """
    args = [mappings_path] + (["--delete"] if delete else [])
    if engine_command:
        exec_line = "ExecStart=" + " ".join(
            unitexec.systemd_quote(a) for a in list(engine_command) + args)
    else:
        # unitexec : chemins entre guillemets si besoin, lanceur stable si paquet.
        exec_line = unitexec.exec_line(DEFAULT_ENGINE, args, app_dir=APP_DIR)
    if omit_cli:
        env_line = ""
    elif cli_line:
        env_line = cli_line + "\n"
    else:
        cli_value = appconfig.cli_unit_value(DEFAULT_CLI) if _HAS_CONFIG else (
            DEFAULT_CLI if os.path.isfile(DEFAULT_CLI) and os.access(DEFAULT_CLI, os.X_OK)
            else None)
        if not cli_value or (unitexec.is_packaged(APP_DIR) and cli_value == DEFAULT_CLI):
            env_line = ""  # chemin absent ou paquet : proton-drive via PATH
        else:
            env_line = "Environment=" + unitexec.quote_environment(
                "PROTON_DRIVE_CLI", cli_value) + "\n"
    desc_service = _("Proton Drive sync")
    return f"""[Unit]
Description={desc_service}
After=network-online.target

# Borne les relances : au plus 6 démarrages par heure. Si le passage échoue
# encore après ces tentatives (vrai problème, pas une simple collision), on
# abandonne jusqu'au prochain déclenchement du timer -> pas de boucle infinie.
StartLimitIntervalSec=1h
StartLimitBurst=6

[Service]
# Type=exec (et non oneshot) : nécessaire pour que Restart= fonctionne.
Type=exec
{env_line}{exec_line}

# Deux codes de sortie du moteur sont des NON-échecs du point de vue systemd,
# déclarés ici pour éviter à la fois le marquage "failed" ET une relance inutile :
#   - code 2 : auth échouée (trousseau verrouillé, session non ouverte) — relancer
#     ne sert à rien ; le temps réel prend le relais dès la session ouverte ;
#   - code 4 : compte Proton changé (le cache appartient à l'ancien compte, rien
#     touché). Le moteur REFUSERA à chaque tentative tant que l'utilisateur n'a pas
#     ré-amorcé depuis le GUI : relancer toutes les 2 min, 6× par heure, chaque
#     nuit, ne ferait que polluer le journal et refaire la sonde d'auth + 2 appels
#     CLI pour rien. L'état est déjà signalé par le consommateur et le GUI.
SuccessExitStatus=0 2 4

# Code 5 : le passage est allé au bout, mais des fichiers ou des dossiers ont
# échoué. L'unité doit apparaître en échec (donc PAS dans SuccessExitStatus),
# sans pour autant relancer 6 fois par heure : le prochain timer, ou le cycle
# temps réel, reprendra. Restart=on-failure ignorerait ce code s'il était un
# succès ; RestartPreventExitStatus le marque failed et ne le redémarre pas.
RestartPreventExitStatus=5

# Collision de verrou : si le consommateur temps réel tient le flock au moment du
# déclenchement, le moteur sort en échec (code 1). On relance alors le passage
# 2 min plus tard — le temps que le consommateur ait fini et libéré le verrou.
# (C'était le trou : avant, le passage planifié échouait et n'était pas relancé,
# donc une seule collision sautait tout le passage nocturne.)
Restart=on-failure
RestartSec=120

# Garde-fou : tue le passage s'il dépasse 6h (large pour un 1er passage).
# RuntimeMaxSec (et non TimeoutStartSec) est le bon réglage pour Type=exec.
RuntimeMaxSec=6h

[Install]
WantedBy=default.target
"""


def build_cloud_service_text(mappings_path):
    """Unité utilisateur du bus de statut (Dolphin et Nautilus).

    Le chemin de mappings est celui du service moteur. Cette unité ne lance
    pas le moteur et n'appelle pas le CLI Proton : elle publie la base de
    synchro sur le bus de session, pour la session graphique seulement.
    """
    script = os.path.join(APP_DIR, "cloudlaunch.py")
    exec_line = unitexec.exec_line(script, [mappings_path], app_dir=APP_DIR)
    desc = _("Proton Drive Sync file-manager status")
    return f"""[Unit]
Description={desc}
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
{exec_line}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical-session.target
"""


def _cloud_service_path():
    return os.path.join(SYSTEMD_USER_DIR, CLOUD_SERVICE_NAME)


def build_timer_text(on_calendar="*-*-* 03:00:00"):
    """Génère le contenu du fichier .timer avec l'heure donnée."""
    desc_timer = _("Triggers the Proton Drive sync once a day")
    return f"""[Unit]
Description={desc_timer}

[Timer]
OnCalendar={on_calendar}
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
"""


# ---------- Lecture de l'état ----------

def service_exists():
    return os.path.exists(SERVICE_PATH)


def service_missing_restart_prevent_5():
    """True seulement si le fichier service installé existe et ne contient pas
    encore RestartPreventExitStatus=5. Absent ou illisible : rien à réécrire."""
    try:
        with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return False
    return "RestartPreventExitStatus=5" not in content


def timer_exists():
    return os.path.exists(TIMER_PATH)


def _read_service_content():
    """Texte du service installé, ou None s'il est absent ou illisible."""
    if not service_exists():
        return None
    try:
        with open(SERVICE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def read_service_engine_command():
    """Arguments d'ExecStart jusqu'au moteur inclus : `python3 proton_sync.py`
    ou le lanceur `proton-drive-sync`.

    None si la ligne n'a ni l'un ni l'autre. La réécriture automatique recopie
    ces arguments tels quels : elle ne les remplace pas par le dossier d'où
    l'éditeur a été lancé."""
    content = _read_service_content()
    if not content:
        return None
    m = re.search(r"^ExecStart=(.*)$", content, re.MULTILINE)
    if not m:
        return None
    args = unitexec.split_exec(m.group(1))
    for idx, tok in enumerate(args):
        if os.path.basename(tok) in ("proton_sync.py", "proton-drive-sync"):
            return args[:idx + 1]
    return None


def read_service_cli_line():
    """Ligne Environment= qui fixe PROTON_DRIVE_CLI, ou None si elle est absente.

    La ligne est rendue telle qu'écrite, guillemets compris. None veut dire
    « ne pas en ajouter une » lors d'une réécriture automatique."""
    content = _read_service_content()
    if not content:
        return None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("Environment=") and "PROTON_DRIVE_CLI=" in stripped:
            return stripped
    return None


def read_service_mappings_path():
    """Extrait le chemin du fichier de mappings de l'ExecStart, ou None."""
    content = _read_service_content()
    if not content:
        return None
    m = re.search(r"^ExecStart=(.*)$", content, re.MULTILINE)
    if m:
        return unitexec.mappings_arg_from_exec(
            m.group(1), "proton_sync.py", "proton-drive-sync")
    return None


def read_service_delete():
    """Retourne True si l'ExecStart contient --delete (Option B)."""
    content = _read_service_content()
    if not content:
        return False
    m = re.search(r"^ExecStart=(.*)$", content, re.MULTILINE)
    return bool(m and "--delete" in unitexec.split_exec(m.group(1)))


def read_timer_calendar():
    """Extrait la valeur OnCalendar du timer, ou None."""
    if not timer_exists():
        return None
    try:
        with open(TIMER_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r"^OnCalendar=(.+)$", content, re.MULTILINE)
    return m.group(1).strip() if m else None


def timer_is_active():
    """True si le timer est chargé et actif (via systemctl)."""
    rc, out, _err = _run(["systemctl", "--user", "is-active", TIMER_NAME])
    return out.strip() == "active"


def timer_next_run():
    """Retourne la prochaine échéance du timer (texte) ou None."""
    rc, out, _err = _run(["systemctl", "--user", "list-timers", TIMER_NAME,
                       "--no-pager", "--all"])
    if rc != 0:
        return None
    for line in out.splitlines():
        if TIMER_NAME in line:
            # La 1re colonne est la date NEXT ; on renvoie la ligne brute, le GUI
            # l'affiche telle quelle (formats systemd variables selon la locale).
            return line.strip()
    return None


def linger_enabled():
    """True si le linger est actif pour l'utilisateur courant."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    rc, out, _err = _run(["loginctl", "show-user", user, "--property=Linger"])
    return "Linger=yes" in out


def linger_command():
    """Retourne la commande sudo à lancer pour activer le linger."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "<utilisateur>"
    return f"sudo loginctl enable-linger {user}"


def status():
    """Retourne un dict résumant l'état complet de la planification."""
    return {
        "service_exists": service_exists(),
        "timer_exists": timer_exists(),
        "timer_active": timer_is_active() if timer_exists() else False,
        "mappings_path": read_service_mappings_path(),
        "delete": read_service_delete(),
        "calendar": read_timer_calendar(),
        "next_run": timer_next_run() if timer_exists() else None,
        "linger": linger_enabled(),
    }


# ---------- Journal de la planification ----------
#
# Source de vérité = le JOURNAL systemd (sur disque, persistant, survit au
# reboot), et NON l'état runtime du service (`systemctl show`), qui est vidé à
# chaque redémarrage du manager. On isole la DERNIÈRE exécution en repérant, dans
# le journal du service, la dernière frontière de démarrage (« Starting/Started
# proton-sync.service ») et en prenant tout ce qui suit — sans dépendre de
# l'InvocationID, que les lignes du manager systemd à propos de l'unité ne
# portent pas de façon fiable (notamment après un reboot).

# Plafond de lecture du journal du service : borne mémoire/temps. Un passage
# nocturne dépasse rarement quelques centaines de lignes, donc la frontière de
# démarrage du dernier passage est toujours dans cette fenêtre.
_MAX_JOURNAL_LINES = 5000


def _entry_message(e):
    """Texte MESSAGE d'une entrée JSON (journald encode parfois un message
    non-UTF8 comme une liste d'octets)."""
    msg = e.get("MESSAGE", "")
    if isinstance(msg, list):
        try:
            msg = bytes(msg).decode("utf-8", "replace")
        except Exception:
            msg = str(msg)
    return str(msg)


def _is_run_start(msg):
    """True si le message est la frontière de DÉMARRAGE d'un passage du service
    (ligne du manager systemd « Starting/Started proton-sync.service … »)."""
    return (msg.startswith("Starting ") or msg.startswith("Started ")) \
        and SERVICE_NAME in msg


def _service_entries(limit=_MAX_JOURNAL_LINES):
    """Dernières entrées JSON du journal du service (ordre chronologique)."""
    rc, out, _err = _run(["journalctl", "--user", "-u", SERVICE_NAME,
                       "-o", "json", "--no-pager", "-n", str(limit)])
    if rc != 0 or not out.strip():
        return []
    entries = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    return entries


def _last_run_entries():
    """Entrées de la DERNIÈRE exécution, isolées par la dernière frontière de
    démarrage dans le journal du service. Ne dépend PAS de l'InvocationID (peu
    fiable : les lignes du manager systemd à propos de l'unité ne le portent pas
    toujours, surtout après un reboot). [] si le journal est vide."""
    entries = _service_entries()
    if not entries:
        return []
    start_idx = 0
    found = False
    for i, e in enumerate(entries):
        if _is_run_start(_entry_message(e)):
            start_idx = i
            found = True
    # Si aucune frontière trouvée (lignes de démarrage hors fenêtre), on renvoie
    # tout ce qu'on a — dégradé, mais préférable à un panneau vide.
    return entries[start_idx:] if found else entries


def _entry_local_time(entry):
    """Horodatage local lisible d'une entrée de journal, ou ''."""
    ts = entry.get("__REALTIME_TIMESTAMP")
    if not ts:
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(int(ts) / 1_000_000)
        return dt.strftime("%a %Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ""


def _entry_iso(entry):
    """Horodatage ISO local (façon journalctl short-iso) d'une entrée, ou ''."""
    ts = entry.get("__REALTIME_TIMESTAMP")
    if not ts:
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(int(ts) / 1_000_000).astimezone()
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    except (ValueError, OSError, OverflowError):
        return ""


def _format_entries(entries):
    """Rend les entrées JSON sous une forme lisible proche de journalctl
    short-iso : « <iso> <hôte> <ident>[<pid>]: <message> »."""
    out = []
    for e in entries:
        head = _entry_iso(e)
        host = e.get("_HOSTNAME", "")
        ident = e.get("SYSLOG_IDENTIFIER") or e.get("_COMM") or ""
        pid = e.get("_PID") or e.get("SYSLOG_PID") or ""
        if host:
            head += " " + host
        if ident:
            head += " " + ident + (f"[{pid}]" if pid else "") + ":"
        out.append(f"{head} {_entry_message(e)}")
    return "\n".join(out)


def _run_result_exit(text):
    """Code porté par la ligne stable [run-result], ou None.

    Le mot « Done. » est imprimé aussi pour le code 5 (boucle terminée). Sans
    cette lecture, un passage incomplet serait affiché comme un succès."""
    for line in (text or "").splitlines():
        idx = line.find("[run-result] ")
        if idx < 0:
            continue
        try:
            payload = json.loads(line[idx + len("[run-result] "):])
        except ValueError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("exit"), int):
            return payload["exit"]
    return None


def _parse_result(text):
    """Déduit (ok, code) du texte d'une invocation. ok ∈ {True, False, None}
    (None = indéterminé, ex. passage encore en cours). Priorité à l'échec.

    On combine les marqueurs systemd (Failed with result, status=N/FAILURE,
    signal) et le marqueur applicatif du moteur (« Terminé. » = boucle de synchro
    menée à son terme ; « Une autre instance » = collision de verrou).
    Le code 5 (terminé avec échecs) reste un échec même si « Done. » est présent."""
    # Marqueurs multilingues : le journal peut contenir des passages en
    # français (historique) ET en anglais (source i18n) — on matche les deux.
    LOCK_MARKERS = ("Une autre instance", "Another instance")
    DONE_MARKERS = ("Terminé.", "Done.")
    run_exit = _run_result_exit(text)
    m = re.search(r"status=(\d+)/FAILURE", text)
    if "Failed with result" in text or (m and m.group(1) != "0"):
        if any(x in text for x in LOCK_MARKERS) and not m:
            return False, 1
        code = int(m.group(1)) if m else None
        if code == 5 or run_exit == 5:
            return False, 5
        return False, code
    if "code=killed" in text or "/TERM" in text:
        return False, None  # interrompu par signal
    if any(x in text for x in LOCK_MARKERS):
        return False, 1     # collision de verrou (le moteur a refusé de démarrer)
    if run_exit == 5:
        return False, 5
    if (any(x in text for x in DONE_MARKERS) or "Finished " in text
            or "Deactivated successfully" in text or run_exit == 0):
        return True, 0
    return None, None       # indéterminé (ex. passage en cours)


def _result_label(ok, code):
    if ok is False and code == 5:
        return _("⚠ completed with failures (code 5)")
    if ok is True:
        return _("✅ success") + (f" (code {code})" if code not in (None, 0) else "")
    if ok is False:
        if code is not None:
            return _("❌ failure (code {c})").format(c=code)
        return _("⏹ interrupted (signal)")
    return _("❔ undetermined (run in progress?)")


def last_run_summary():
    """Résumé du DERNIER passage planifié, dérivé du journal persistant.
    Retourne {when, result, exit_status, ok, label}, ou {} si aucun passage
    n'est trouvé dans le journal."""
    entries = _last_run_entries()
    if not entries:
        return {}
    when = _entry_local_time(entries[0])
    text = "\n".join(_entry_message(e) for e in entries)
    ok, code = _parse_result(text)
    result = "success" if ok is True else ("fail" if ok is False else "")
    return {"when": when, "result": result, "exit_status": code,
            "ok": ok, "label": _result_label(ok, code)}


def journal_last_run():
    """Journal de la DERNIÈRE exécution planifiée, isolée par sa frontière de
    démarrage (correct même après un reboot). Retourne (ok, texte). Ne crée aucun
    fichier : lit le journal systemd (borné et auto-purgé)."""
    entries = _last_run_entries()
    if not entries:
        return False, _("(no run found in the journal)")
    return True, _format_entries(entries)


def journal_for_date(date_str):
    """Journal du service pour une date 'AAAA-MM-JJ'. Retourne (ok, texte).
    La date est validée en amont pour éviter toute injection d'arguments."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str or ""):
        return False, _("(invalid date — expected format YYYY-MM-DD)")
    rc, out, err = _run(["journalctl", "--user", "-u", SERVICE_NAME,
                         "--since", f"{date_str} 00:00:00",
                         "--until", f"{date_str} 23:59:59",
                         "--no-pager", "-o", "short-iso"])
    if rc != 0:
        return False, (err.strip() or "(échec de lecture du journal)")
    return True, (out if out.strip()
                  else f"(aucun passage planifié le {date_str})")


# ---------- Écriture / actions ----------

def _write(path, content):
    os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def daemon_reload():
    return _run(["systemctl", "--user", "daemon-reload"])


def install_or_update(mappings_path, on_calendar="*-*-* 03:00:00", delete=False,
                      enable=True, engine_command=None, cli_line=None,
                      omit_cli=False):
    """Crée ou met à jour service + timer, recharge systemd, et active le timer
    si enable=True. Retourne (ok, message).

    Sans engine_command, le chemin du moteur et PROTON_DRIVE_CLI viennent du
    dossier qui exécute ce module (bouton Installer / Mettre à jour).
    refresh_units passe les valeurs déjà installées pour ne pas les déplacer."""
    try:
        _write(SERVICE_PATH, build_service_text(
            mappings_path, delete=delete, engine_command=engine_command,
            cli_line=cli_line, omit_cli=omit_cli))
        _write(TIMER_PATH, build_timer_text(on_calendar))
        _write(_cloud_service_path(), build_cloud_service_text(mappings_path))
    except OSError as e:
        return False, _("Failed to write the systemd files: {e}").format(e=e)

    rc, out, err = daemon_reload()
    if rc != 0:
        return False, _("daemon-reload failed: {e}").format(e=err or out)

    timer_enabled = False
    if enable:
        rc, out, err = _run(["systemctl", "--user", "enable", "--now", TIMER_NAME])
        if rc != 0:
            return False, _("Enabling the timer failed: {e}").format(e=err or out)
        timer_enabled = True

    # Le statut des gestionnaires de fichiers ne doit pas empêcher le timer
    # déjà activé de rester en place, mais l'échec reste visible.
    rc, out, err = _run(["systemctl", "--user", "enable", "--now", CLOUD_SERVICE_NAME])
    if rc != 0:
        detail = err or out
        if timer_enabled:
            return False, _(
                "Schedule installed and timer enabled, but the file-manager status service failed: {e}"
            ).format(e=detail)
        return False, _(
            "The file-manager status service failed: {e}").format(e=detail)
    if timer_enabled:
        return True, _("Schedule installed and timer enabled.")
    return True, _("Schedule installed (timer not enabled).")


def refresh_units():
    """Réécrit service, timer et l'unité de statut depuis leurs valeurs actuelles.

    Les installations déjà en place gardent l'ancien texte d'unité. Cette
    fonction y remet RestartPreventExitStatus=5 et l'unité de statut des
    gestionnaires de fichiers, sans changer le fichier de mappings, l'heure
    du timer, l'option --delete, le moteur sur ExecStart (interpréteur et
    chemin, ou lanceur), ni PROTON_DRIVE_CLI. Une ligne CLI absente n'est pas
    ajoutée. Seul le bouton Installer / Mettre à jour réécrit ces deux
    chemins."""
    mappings_path = read_service_mappings_path()
    engine_command = read_service_engine_command()
    if mappings_path is None or engine_command is None:
        return False, _("Service not found — install the schedule first.")
    cli_line = read_service_cli_line()
    calendar = read_timer_calendar() or "*-*-* 03:00:00"
    delete = read_service_delete()
    return install_or_update(mappings_path, on_calendar=calendar, delete=delete,
                             enable=timer_is_active(),
                             engine_command=engine_command,
                             cli_line=cli_line, omit_cli=(cli_line is None))


def set_delete(delete):
    """Bascule l'Option A/B en réécrivant le service avec le même mappings_path."""
    mappings_path = read_service_mappings_path()
    if mappings_path is None:
        return False, _("Service not found — install the schedule first.")
    cal = read_timer_calendar() or "*-*-* 03:00:00"
    return install_or_update(mappings_path, on_calendar=cal, delete=delete,
                             enable=timer_is_active())


def set_calendar(on_calendar):
    """Change l'heure (OnCalendar) en conservant le reste."""
    mappings_path = read_service_mappings_path()
    if mappings_path is None:
        return False, _("Service not found — install the schedule first.")
    delete = read_service_delete()
    return install_or_update(mappings_path, on_calendar=on_calendar, delete=delete,
                             enable=timer_is_active())


def enable_timer():
    rc, out, err = _run(["systemctl", "--user", "enable", "--now", TIMER_NAME])
    if rc != 0:
        return False, _("Failed: {e}").format(e=err or out)
    return True, _("Timer enabled.")


def disable_timer():
    rc, out, err = _run(["systemctl", "--user", "disable", "--now", TIMER_NAME])
    if rc != 0:
        return False, _("Failed: {e}").format(e=err or out)
    return True, _("Timer disabled.")


def pause_timer():
    """Arrête le timer SANS le désactiver (il reste enable pour le prochain boot).
    Sert à empêcher un déclenchement planifié pendant un amorçage/passage manuel
    long, sans altérer la configuration. Silencieux si le timer n'existe pas."""
    if not timer_exists():
        return True, ""
    _run(["systemctl", "--user", "stop", TIMER_NAME])
    return True, _("Timer paused.")


def resume_timer():
    """Réarme le timer après pause_timer. IMPORTANT : pour un timer OnCalendar, un
    simple `start` le rend « active » mais NE recalcule PAS le prochain
    déclenchement (l'état reste « Trigger: n/a » -> il ne se redéclenchera jamais).
    Il faut un `restart`, qui recharge l'unité et recalcule le prochain OnCalendar.
    On tolère l'absence du timer (rien à faire)."""
    if not timer_exists():
        return True, ""
    _run(["systemctl", "--user", "restart", TIMER_NAME])
    return True, _("Timer resumed.")


def run_now():
    """Déclenche immédiatement le service (test sans attendre l'heure)."""
    rc, out, err = _run(["systemctl", "--user", "start", SERVICE_NAME])
    if rc != 0:
        return False, _("Start failed: {e}").format(e=err or out)
    return True, _("Service started (see journalctl for the result).")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Proton Drive sync schedule")
    parser.add_argument(
        "--refresh-units", action="store_true",
        help="Rewrite the user service, timer and file-manager status unit from their current settings",
    )
    args = parser.parse_args(argv)
    if not args.refresh_units:
        parser.print_help()
        return 2
    ok, message = refresh_units()
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
