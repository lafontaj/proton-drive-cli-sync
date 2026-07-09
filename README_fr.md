🇬🇧 [English](README.md) | 🇫🇷 **Français**

# Synchro Proton Drive via le CLI officiel (Linux)

Un moteur Python + une interface graphique qui pilotent le **CLI officiel de Proton Drive** pour obtenir une vraie synchronisation de dossiers vers Proton Drive sur Linux — l'équivalent d'un `robocopy /MIR` : seuls les fichiers nouveaux ou modifiés sont envoyés à chaque passage, pas de re-téléversement aveugle. Deux modes complémentaires : **temps réel** (surveillance des dossiers, envoi dès qu'un fichier change) et **planification** (passage complet périodique qui réconcilie et sert de filet de sécurité).

Pensé au départ pour synchroniser un NAS vers Proton Drive pour toute une famille, mais utilisable pour n'importe quel dossier local ou réseau.

> ℹ️ Le CLI de Proton Drive est un outil bas niveau (upload, list, delete…) sans moteur de synchronisation continue. Ce projet ajoute par-dessus la couche « synchronisation » qui manque, en attendant l'application Linux complète annoncée par Proton.

---

## ⚠️ Prérequis : le CLI de Proton Drive (NON inclus)

**Le CLI officiel de Proton Drive n'est PAS fourni dans ce dépôt.** Vous devez le télécharger directement chez Proton :

**➡️ https://proton.me/download/drive/cli/index.html**

Ensuite :

1. Rendez le binaire exécutable : `chmod +x proton-drive`
2. Placez le binaire `proton-drive` **dans le même dossier que les fichiers `.py`** de ce projet (le plus simple), ou indiquez son chemin dans **⚙ Configuration → CLI Proton Drive** (ou via la variable d'environnement `PROTON_DRIVE_CLI`).
3. Authentifiez-vous une fois : `./proton-drive auth login` (ouvre votre navigateur). La session est stockée de façon sécurisée par le trousseau du système (libsecret / GNOME Keyring).

## Autres dépendances

- **Python 3** avec **Tkinter** — interface graphique : `sudo apt install python3-tk`
- **PyGObject + libxapp** — icône d'état dans la barre des tâches (Cinnamon/MATE/Xfce) : `sudo apt install python3-gi gir1.2-xapp-1.0` *(déjà présents sur Linux Mint)*
- **pyinotify** — surveillance temps réel : `sudo apt install python3-pyinotify`
- **Zenity** *(optionnel, recommandé)* — sélecteurs de fichiers/dossiers et calendrier natifs GTK : `sudo apt install zenity`. Absent, l'application se rabat automatiquement sur les dialogues Tkinter (moins jolis mais fonctionnels).
- **libsecret / trousseau** (GNOME Keyring, actif dès qu'une session graphique tourne) — le CLI y stocke la session Proton
- **systemd** (mode utilisateur, avec *linger*) — pour les démons temps réel et les passages planifiés
- **`nfs-common`** *(uniquement si votre source est un NAS monté en NFS)* — client NFS pour monter les partages : `sudo apt install nfs-common`. Inutile si vous ne synchronisez que des dossiers locaux.

## Démarrage rapide

```bash
git clone https://github.com/CapitaineFlamQuebec/proton-drive-cli-sync.git
cd proton-drive-cli-sync

# 1. Placer le binaire proton-drive ici (voir ci-dessus) et s'authentifier
./proton-drive auth login

# 2. Créer vos fichiers de configuration à partir des exemples
cp settings.example.json settings.json
cp mappings.example.json mappings-user1.json

# 3. Lancer l'éditeur graphique
python3 proton_mapping_editor.py mappings-user1.json
```

Depuis l'éditeur, la première mise en place d'un dossier se fait en général ainsi :

1. **Ajouter le dossier** (bouton « ➕ Dossier… »). Un navigateur des destinations Proton (« 🔍 Parcourir Proton… ») évite de taper les chemins à la main.
2. **Décider du comportement de suppression** dans le mapping :
   - *Additif* (par défaut) : la synchro n'efface jamais rien sur Proton. Sûr, aucun réglage particulier.
   - *Miroir* : cochez la suppression et choisissez le mode **corbeille** (`trash` — récupérable 30 jours sur Proton, recommandé) ou **permanent** (définitif). Ce qui disparaît localement disparaîtra alors de Proton.

   > ⚠️ **DANGER — lisez ceci avant d'activer le mode miroir.** Le miroir rend la destination Proton **identique à la source locale**. Si le dossier de destination sur Proton **contient déjà des fichiers ou dossiers qui ne sont PAS présents dans la source locale**, ils seront **supprimés** (corbeille ou définitivement, selon le mode) — pour que Proton devienne un miroir exact du local. Ce n'est pas « envoyer mes fichiers locaux en plus » : c'est « faire correspondre exactement ». Ces suppressions se déclenchent dès qu'un mapping est configuré en miroir (champ corbeille rempli) : à son amorçage comme à ses passages ultérieurs. Un mapping additif (corbeille vide) ne supprime jamais rien. **Avant d'activer le miroir sur une destination qui contient déjà des données, faites un essai `--delete --dry-run`** (voir la section « Propagation des suppressions ») pour voir précisément ce qui serait effacé, sans rien supprimer.
3. **Amorcer le mapping** (bouton « 🌱 Amorcer le cache »). C'est l'étape à ne pas sauter : elle fait un premier passage complet qui construit le cache et marque l'arborescence comme « complète » — ce qui rend le mapping **prêt pour le temps réel**. **L'amorçage suit la configuration de chaque mapping** (son champ corbeille), sans aucune option à choisir au moment d'amorcer :
   - un mapping **additif** (corbeille vide) est téléversé sans jamais rien supprimer ;
   - un mapping **miroir** (corbeille ou suppression définitive) réconcilie la destination selon son mode, dès ce premier passage.

   L'amorçage gère tout seul l'arrêt/redémarrage des démons et affiche sa progression. **Changer la vocation plus tard** se fait dans l'éditeur du mapping (via le champ corbeille) : passer d'additif à miroir réinitialise l'état d'amorçage du mapping (il faudra le ré-amorcer, l'éditeur vous en avertit) ; passer de miroir à additif ne demande aucun ré-amorçage. Astuce : décidez la vocation d'un mapping **avant** qu'il contienne beaucoup de données, pour qu'un éventuel ré-amorçage reste rapide.
4. **Activer le temps réel et/ou la planification** si souhaité (fenêtres « ⚡ Temps réel… » et « ⏰ Planification… »).

> 💡 Pour un premier essai sans risque, commencez en mode additif : vous voyez vos fichiers monter sur Proton sans aucune possibilité d'effacement. Vous activerez le mode miroir plus tard, une fois à l'aise.

---

## Comment ça marche

### Détection des changements (comme `robocopy /MIR`)

À chaque passage, le moteur (`proton_sync.py`) compare l'état local à un **cache** et n'envoie que ce qui a changé (nouveau ou modifié). Sans ce cache, le CLI ferait un appel réseau par dossier visité (~1–2 s chacun) — sur une grosse arborescence, un passage « rien à faire » prendrait des heures. Avec le cache, ces passages sont quasi instantanés.

- **Cache local** : une signature par dossier ; un dossier inchangé est sauté sans appel réseau.
- **Verrou** (`flock`) : empêche deux passages simultanés (temps réel vs planifié) de se marcher dessus.
- **Reprise après interruption** : un Ctrl+C est sans danger — au passage suivant, les fichiers déjà envoyés sont reconnus et sautés.
- **Création automatique** des dossiers de destination manquants sur Proton.

### Format du fichier de mappings

Un mapping décrit *quel dossier local* va *vers quel dossier Proton*. Deux formats sont acceptés.

**Format complet** (recommandé — avec exclusions) :

```json
{
  "exclusions": {
    "names": [".caltrash", "trash", ".Trash-1000"],
    "patterns": ["*.tmp", "~*"]
  },
  "mappings": [
    {
      "type": "folder",
      "source": "/media/nas1/Documents",
      "dest_parent": "/my-files",
      "exclusions": { "names": [".specifique-a-ce-mapping"] },
      "allow_delete": true,
      "delete_mode": "trash",
      "source_kind": "nfs"
    }
  ]
}
```

**Format simple** (liste, toujours supporté) :

```json
[
  { "type": "folder", "source": "/home/moi/Documents", "dest_parent": "/my-files" }
]
```

Champs d'un mapping :

- `type` : `folder` (tout le contenu, récursif) ou `file` (un fichier unique — utile pour un conteneur chiffré, en liste blanche explicite pour éviter qu'un nouveau conteneur parte par erreur).
- `source` : chemin local ou réseau à synchroniser.
- `dest_parent` : dossier **parent** sur Proton où le contenu atterrit (ex. `/my-files`, `/my-files/Sauvegardes`).
- `allow_delete` *(optionnel)* : `true`/`false`. Absent = `false` = mode **additif** (jamais de suppression). À `true`, les suppressions locales sont propagées sur Proton (vrai miroir).
- `delete_mode` *(optionnel)* : `"trash"` (corbeille Proton, récupérable 30 jours) ou `"permanent"` (définitif).
- `source_kind` *(optionnel)* : `"nfs"` ou `"local"`. Détecté par le GUI. Garde-fou : une source `nfs` ne supprime que si le montage réseau est vivant (un montage tombé ne déclenche pas un miroir destructeur).

### Exclusions

Deux mécanismes combinés, appliqués aux **dossiers et fichiers**, par **nom** (jamais le chemin complet), insensibles à la casse :

- `names` : noms exacts (ex. `.caltrash`, `trash`, `.Trash-1000`)
- `patterns` : motifs glob façon shell (ex. `*.tmp`, `.Trash-*`, `~*`)

Deux niveaux qui se cumulent : **globales** (tous les mappings) et **par mapping** (en plus, pour un mapping précis). Un dossier exclu n'est pas visité du tout.

Choix délibéré : on **n'exclut pas** aveuglément tous les fichiers cachés (commençant par `.`). Un `.config_important` que vous voulez garder est conservé ; seul ce qui est listé explicitement est exclu.

Avec `--delete`, un fichier exclu localement mais déjà présent sur Proton (envoyé avant l'ajout de l'exclusion) est traité comme un **orphelin** et part à la corbeille au passage suivant. L'empreinte du jeu d'exclusions fait partie de la signature du cache : tout changement d'exclusions force cette réconciliation automatiquement (le premier passage suivant est plus long, puis les sauts rapides reprennent).

`mappings.example.json` fournit une **liste d'exclusions par défaut** couvrant les fichiers temporaires courants (Linux, macOS, Windows) — un bon point de départ. Un catalogue de motifs par logiciel est maintenu dans `Temporary-files-exclusions_fr.md`.

### Propagation des suppressions (`--delete`) — miroir optionnel

Par défaut, la synchro est **additive** : elle n'efface jamais rien sur Proton. Activer `--delete` (ou `allow_delete: true` sur un mapping) transforme la synchro en **vrai miroir** : ce qui disparaît localement disparaît de Proton (corbeille ou définitif selon `delete_mode`). Plusieurs garde-fous entourent cette opération sensible : double condition de sécurité (`--delete` **et** `allow_delete: true` requis ensemble), vérification que le montage réseau est vivant pour les sources `nfs` (un NAS tombé ne déclenche jamais un miroir destructeur), et arborescence considérée complète avant toute suppression.

> ⚠️ **Le miroir supprime aussi le contenu Proton préexistant absent du local.** Le but du miroir est que la destination Proton soit **exactement** la source locale. Donc tout fichier ou dossier déjà présent sur la destination Proton **mais absent de la source locale** est traité comme un « orphelin » et **supprimé** (corbeille 30 j ou définitif). Cela vaut **dès l'amorçage**, pas seulement pour les suppressions faites plus tard. Si votre destination Proton contient déjà des données que vous voulez garder, **n'activez pas le miroir dessus** sans avoir d'abord vérifié.

**Toujours tester d'abord avec `--dry-run`.** Un passage `--delete --dry-run` affiche exactement ce qui serait supprimé, **sans rien effacer**. C'est le réflexe à avoir avant d'activer le miroir sur une destination qui n'est pas vide. En ligne de commande :

```bash
python3 proton_sync.py mappings-user1.json --delete --dry-run
```

Le mode `permanent` (`delete_mode: "permanent"`) est **irréversible** : préférez `trash` (corbeille Proton, récupérable 30 jours) tant que vous n'êtes pas certain du comportement.

---

## Interface graphique (`proton_mapping_editor.py`)

- **Édition des mappings** : ajout/modification de dossiers et fichiers, avec un **navigateur des destinations Proton** (« 🔍 Parcourir Proton… ») qui liste `/my-files` et `/shared-with-me` — plus besoin de taper les chemins à la main.
- **⚙ Configuration** (un seul bouton) : compte Proton (connexion), chemin du CLI, langue, NAS (activation, point de montage, identité), extensions, icône de barre des tâches. Chaque réglage a une aide « ? ».
- **Témoin de connexion** en bas à droite : le vrai compte connecté (« 🔑 Proton : connecté à … »).
- **Colonne « Prêt »** par mapping (✅/⏳/—) et **journal des passages** (dernière exécution, succès/échec).
- **Icône de barre des tâches** (optionnelle) : double flèche circulaire — violette (actif + session OK), grise + X (session expirée), grise (démons arrêtés). Clic gauche = ouvrir l'éditeur.

## Synchronisation temps réel

Surveillance des dossiers par `inotify` : dès qu'un fichier change, un **marqueur** est déposé et un petit démon (le « consommateur ») lance le moteur sur la zone concernée, avec anti-rebond et dédoublonnage. La surveillance est **distribuée** : un watcher sur la machine locale, un autre sur le NAS (pour les dossiers réseau), chacun écrivant dans une file de marqueurs. Le temps réel gère le quotidien (petits changements ciblés) ; la planification bâtit et réconcilie l'arborescence complète.

Fenêtre « ⚡ Temps réel… » : état des démons, délais, files de marqueurs, journal en direct, et boutons Installer/Mettre à jour.

## Planification automatique

Passages complets périodiques via des **timers systemd utilisateur** (avec *linger* pour survivre à la fermeture de session). Sert de filet de sécurité : ce que le temps réel aurait manqué (démon arrêté, montage tombé…) est rattrapé au passage planifié. Le journal des passages, dans le GUI, montre la dernière exécution par frontière de démarrage.

## Identité NAS et files de marqueurs

Chaque installation a une **identité stable** (réglage `account_name`, découplée du nom du fichier de mappings et du compte Proton) qui nomme sa copie de config et sa file de marqueurs sur le NAS (`mappings-<identité>.json`, `queue/<identité>`). Sur une installation neuve, un nom neutre unique (`user1`, `user2`…) est réclamé automatiquement sur le NAS sans collision possible. Renommer le fichier de mappings, ou changer de compte Proton, ne casse donc plus rien côté NAS.

## Deux réglages système à connaître (démons temps réel et planification)

Ces deux points ne sont pas évidents pour qui débute avec systemd, et leur oubli provoque des symptômes déroutants. Prenez une minute pour les vérifier.

### 1. Activer *linger* (sinon les démons s'arrêtent à la déconnexion)

Les démons (surveillance temps réel, passages planifiés) tournent en **services systemd utilisateur**. Par défaut, un service utilisateur **s'arrête quand vous fermez votre session** et ne redémarre qu'à la prochaine ouverture. Symptôme typique : « la synchro marche quand je suis connecté, mais s'arrête la nuit / quand je me déconnecte ». La solution est d'activer *linger*, qui autorise vos services à continuer sans session ouverte :

```bash
sudo loginctl enable-linger $USER
```

À faire **une seule fois**, sur la machine locale **et** sur le NAS (avec le nom d'utilisateur concerné sur chacun). Pour vérifier : `loginctl show-user $USER | grep Linger` doit afficher `Linger=yes`.

### 2. Augmenter la limite inotify si vous surveillez beaucoup de dossiers

La surveillance temps réel « pose une sentinelle » (*watch*) sur chaque dossier surveillé. Le noyau Linux limite le nombre total de sentinelles par utilisateur (`fs.inotify.max_user_watches`, souvent 8192 par défaut sur les systèmes anciens, bien plus sur les récents). Sur une grosse arborescence (plusieurs milliers de dossiers), la limite peut être atteinte — la surveillance devient alors incomplète, sans erreur visible. L'application **vérifie la capacité au démarrage** et l'affiche dans son journal ; si elle signale un manque, augmentez la limite :

```bash
# Vérifier la limite actuelle
cat /proc/sys/fs/inotify/max_user_watches

# L'augmenter durablement (ex. 512000) — à faire là où tourne le watcher
echo 'fs.inotify.max_user_watches=512000' | sudo tee /etc/sysctl.d/40-proton-sync.conf
sudo sysctl --system
```

Sur un NAS avec une très grande arborescence, c'est le réglage à surveiller en priorité si le watcher NAS peine à tout suivre.

## Prérequis NAS : montages et accès (important)

Si votre source est un NAS, l'architecture repose sur **deux invariants** à respecter — sans eux, la surveillance temps réel côté NAS ne fonctionnera pas.

**1. Chemins de données identiques des deux côtés.** Les dossiers surveillés doivent être accessibles au **même chemin** sur la machine locale et sur le NAS. Par exemple, si un dossier est `/media/nas1/Documents` vu depuis la machine locale (montage NFS), il doit aussi être `/media/nas1/Documents` vu depuis le NAS lui-même (montage *bind*, ou le chemin réel si le watcher tourne sur le NAS). C'est ce qui permet aux marqueurs déposés par un watcher d'être compris par l'autre : un marqueur désigne un chemin, et ce chemin doit signifier la même chose partout. C'est l'essence du fonctionnement multi-machines.

**2. Accès en écriture au dossier d'échange du NAS.** Le dossier `proton-sync/` sur le NAS (contenant `config/` et `queue/`) doit être **accessible en écriture** depuis la machine locale — typiquement via un montage NFS dédié (dans l'installation de référence : `/media/home_nas/proton-sync/`). La machine locale y **pousse** la copie des mappings (pour que le watcher NAS sache quoi surveiller) et y **lit** les marqueurs déposés par le watcher NAS. Sans écriture sur ce dossier, le push échoue et le temps réel côté NAS reste muet.

**Le watcher NAS tourne sur le NAS** (`nas_watcher.py`), installé en service systemd. Il a besoin des mêmes fichiers Python que la machine locale (`nas_watcher.py`, `local_watcher.py`, `config.py`, `i18n.py`, `mount_check.py`) dans son dossier `proton-sync/` — la machine locale les y **synchronise automatiquement** à chaque Installer/Mettre à jour du temps réel (copie différentielle). Le fichier de service `.service`, lui, n'est pas installé à distance : il est déposé, et si son contenu change, un message rappelle la commande à lancer sur le NAS (`sudo systemctl daemon-reload && sudo systemctl restart proton-nas-watch.service`).

> 💡 En **mode local seul** (pas de NAS), rien de tout cela ne s'applique : `nas_enabled: false` et l'application ne cherche jamais à atteindre un NAS.

## Configuration (`settings.json`) et mode local seul

Tout ce qui varie d'une installation à l'autre est dans `settings.json` (voir `settings.example.json`) : langue, activation du NAS et point de montage, chemin du CLI, identité, réglages d'extensions, icône de barre des tâches. En **mode local seul** (`nas_enabled: false`), l'application ne tente jamais d'atteindre un NAS — les sections NAS sont masquées.

Le cache, la file de marqueurs et les journaux vivent sous un dossier unique : `~/.proton-drive-sync/`.

## Internationalisation

L'interface est disponible en **six langues** : français, anglais, allemand, espagnol, italien et portugais (catalogue gettext). La langue se choisit dans ⚙ Configuration. Les `.mo` compilés sont fournis (l'appli s'affiche traduite dès le clone) ; pour régénérer après modification des traductions : `./build_locales.sh`.

Le **français** et l'**anglais** sont les langues de référence, maintenues par l'auteur. L'**allemand, l'espagnol, l'italien et le portugais** sont des traductions communautaires complètes mais perfectibles : corrections et améliorations sont les bienvenues (voir ci-dessous). La documentation (README, guides) reste en français et anglais.

### Contribuer aux traductions

Les traductions vivent dans `locale/<langue>/LC_MESSAGES/proton-sync.po`. Pour corriger ou améliorer une langue : éditez le fichier `.po` correspondant (chaque `msgid` est la chaîne anglaise, chaque `msgstr` sa traduction), puis lancez `./build_locales.sh` pour recompiler. Les règles à respecter : ne jamais traduire les balises entre crochets (`[account-changed]`, `[config]`, `[DRY-RUN]`…), préserver les champs `{x}` et les emojis. Les propositions de correction ou de nouvelles langues sont bienvenues via une *issue* ou une *pull request*.

---

## Structure du dépôt

| Fichier | Rôle |
|---|---|
| `proton_sync.py` | Moteur de synchronisation (détection, cache, upload, suppressions) |
| `proton_mapping_editor.py` | Interface graphique (édition, configuration, pilotage) |
| `realtime_manager.py`, `realtime_consumer.py` | Couche temps réel (gestion + consommateur de marqueurs) |
| `local_watcher.py`, `nas_watcher.py` | Surveillance `inotify` (machine locale / NAS) |
| `schedule_manager.py` | Passages planifiés (timers systemd) |
| `config.py` | Réglages partagés (`settings.json`) |
| `i18n.py`, `locale/` | Traductions (FR, EN, DE, ES, IT, PT) |
| `tray_indicator.py` | Icône de barre des tâches |
| `mount_check.py` | Vérification des montages réseau |
| `*.example.json` | Modèles de configuration à copier |
| `build_locales.sh`, `build_pdf.py` | Utilitaires (traductions ; guides PDF — nécessite `wkhtmltopdf`) |

## Licence

MIT — voir [LICENSE](LICENSE).

Projet indépendant, non affilié à Proton AG. « Proton » et « Proton Drive » appartiennent à Proton AG.
