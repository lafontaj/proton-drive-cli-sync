🇬🇧 [English](README.md) | 🇫🇷 Français

# Synchro Proton Drive via CLI officiel (Linux)

Document de référence pour ce projet.

---

## Captures d'écran

L'éditeur de mappings — la fenêtre principale : dossiers à synchroniser, exclusions par mapping, la zone de sortie partagée, et les commandes de lancement.

![Éditeur de mappings](docs/images/mappings-editor.png)

Le bas de la fenêtre sépare deux types d'action, parce qu'ils n'obéissent pas aux mêmes options :

- **Cache** — *Amorcer le cache* et *Réinitialiser le mapping*. Ce sont **toujours des passages réels**, pilotés par la configuration propre à chaque mapping ; l'option *Test (dry-run)* ne s'y applique **pas**, car le cache ne peut être armé que par une vraie synchronisation. L'encadré a son propre bouton *Arrêter*.
- **Synchronisation manuelle** — *Lancer la synchro*, avec les options qui n'agissent que sur elle : *Test (dry-run)*, *Propager suppressions*, et les options avancées.

La zone de sortie est commune aux deux : c'est pourquoi ses contrôles d'affichage (*Erreurs seules*, *Effacer la sortie*) se trouvent avec la sortie elle-même.

La fenêtre Temps réel : état des démons, délais, état du NAS, et le journal d'événements en direct.

![Fenêtre Temps réel](docs/images/real-time.png)

La fenêtre Planification (le timer systemd nocturne) :

![Fenêtre Planification](docs/images/schedule.png)

Le dialogue de configuration — compte Proton, chemin du CLI, langue de l'interface, réglages NAS, extensions de fichiers, barre des tâches, et le lanceur d'application (menu d'applications et/ou raccourci sur le bureau, ouvrant l'éditeur vide ou sur le fichier de mappings courant) :

![Dialogue de configuration](docs/images/configuration.png)

---

## Contexte

Proton a publié en juin 2026 un **CLI officiel** pour Proton Drive (`proton-drive`), disponible sur Linux, macOS et Windows. C'est le successeur logique du pont temporaire WinBoat + iSCSI + robocopy mis en place précédemment (toujours documenté dans le PDF `Guide-ProtonDrive-iSCSI-WinBoat.pdf`).

**Limitation importante du CLI** : ce n'est **pas** un moteur de synchro continue. C'est un outil bas niveau d'opérations atomiques (upload, list, info, etc.), destiné à être appelé depuis des scripts ou des tâches cron. La vraie application graphique avec moteur de synchro intégré est annoncée pour plus tard en 2026.

**Solution développée** : un moteur Python (`proton_sync.py`) qui orchestre le CLI pour obtenir un comportement équivalent à `robocopy /MIR` — détection des fichiers nouveaux/modifiés, sans réuploader bêtement tout à chaque passage. Plus un éditeur graphique Tkinter (`proton_mapping_editor.py`) pour gérer la liste des dossiers à synchroniser.

**État au 15 juillet 2026** : le moteur est pleinement fonctionnel et validé (premier passage complet sur User1 ~2960 dossiers en cache, et sur User2). Une **couche de synchronisation temps réel** (surveillance inotify + démons systemd, pilotée par le GUI) s'ajoute désormais au moteur batch et synchronise dès qu'un fichier change ; le batch planifié devient un **filet** (hebdomadaire). Automatisation systemd opérationnelle pour les deux profils. Système d'exclusions implémenté. Le projet est en production. Durcissements récents validés en conditions réelles : court-circuit d'authentification (plus de bruit « code 2 » quand le trousseau est verrouillé), relance automatique du passage planifié en cas de collision de verrou avec le temps réel, et watcher local **conscient des montages** (rattrapage des sources NAS montées tardivement au boot, et suivi des montages qui tombent/remontent en cours de session).

**Fonctionne sans NAS.** Le NAS est entièrement optionnel : une nouvelle installation démarre en **mode local seul** (les dossiers se synchronisent directement vers Proton Drive, sans NAS). Activer un NAS est un choix explicite dans les réglages. Quand un NAS configuré devient injoignable (reboot, réseau), le démon **suspend uniquement les mappings NAS et continue de synchroniser les mappings locaux**, puis reprend automatiquement au retour du NAS — sans blocage ni perte de changements (les marqueurs côté NAS sont préservés et un rebalayage complet a lieu au redémarrage du watcher NAS).

**Autres ajouts récents** : un **auto-test de correspondance des chemins NAS** de bout en bout (pastilles colorées par mapping, avec avertissement avant l'ajout d'un mapping NAS non validé) ; un **indicateur d'envoi discret** pendant qu'un lot est transféré ; des boutons **copier-vers-le-presse-papier** pour les commandes shell affichées dans les dialogues ; une sonde de joignabilité NAS non bloquante pour que la fenêtre temps réel ne se fige jamais quand le NAS est absent ; et une **alerte d'écart de scripts NAS** (badge « ! » sur l'icône de la barre des tâches et avertissement dans la fenêtre) qui signale, sans rien pousser tout seul, qu'un déploiement vers le NAS reste à faire.

---

## Environnement cible

- **Machine** : Linux Mint desktop, nom d'hôte `mypc`, utilisateur `myuser`
- **NAS** : Ubuntu, IP `192.168.1.10`, monté en NFS sous `/media/nas1/`
- **Compte Proton** : `you@proton.me`
- **CLI Proton Drive** : `/home/myuser/Logiciels/Proton-drive/proton-drive`
- **Téléchargement officiel** : <https://proton.me/download/drive/cli/index.html>
- **Version de CLI testée : 0.5.0.** Le comportement du CLI change d'une version à l'autre, et seule celle-ci a été validée ici — à partir de 0.5.0, le type de média est détecté correctement même avec une extension en majuscules, et la mise à la corbeille fonctionne dans un dossier « Partagé avec moi ». L'application vérifie la version installée au démarrage et avertit si elle diffère (antérieure *ou* postérieure), en proposant de quitter pour installer la version testée. Le moteur, qui tourne aussi sans surveillance depuis les timers, se contente d'une ligne d'avertissement et n'interrompt jamais un passage.
- Le CLI dépend de **libsecret** et du service `org.freedesktop.secrets` (trousseau GNOME, déjà actif sur Mint avec session graphique). Les identifiants sont stockés sous le service `ch.proton.drive/drive-sdk-cli`.

**Remplacer le binaire du CLI.** Linux refuse d'écraser un exécutable en cours d'utilisation (`Text file busy`), et le moteur de synchronisation peut très bien être en plein passage. Renommez l'ancien binaire au lieu de l'écraser — le renommage réussit même pendant l'utilisation, les processus en cours poursuivent sur l'ancien inode, et le prochain lancement prend le nouveau :

```bash
cd ~/Logiciels/Proton-drive
mv proton-drive proton-drive-0.4.6      # libère le nom sans rien perturber
cp /chemin/vers/le/nouveau/proton-drive proton-drive
chmod +x proton-drive && ./proton-drive --version
```

Conserver l'ancien binaire vous offre au passage un retour arrière immédiat. L'application mémorise la version détectée avec l'empreinte du binaire : un remplacement est donc pris en compte automatiquement au lancement suivant, sans rien à invalider.


### Pour le compte de User2

Comme le CLI ne supporte qu'un seul compte actif à la fois (les identifiants sont dans le trousseau de l'utilisateur Linux courant), la solution prévue est : **deux comptes Linux distincts** sur le même Mint, chacun avec sa propre session graphique et son propre trousseau. Pas besoin de VM Windows ni de deuxième conteneur — Linux gère nativement les sessions multi-utilisateur sans la limitation Windows Home/Pro.

Le verrou (voir plus bas) utilise `~/.proton_sync.lock` (dans le home de chaque utilisateur) et le cache `~/.proton_sync_cache/` — donc User1 et User2 peuvent tourner **simultanément** depuis leurs sessions respectives sans interférence.

---

## Racines disponibles sur Proton Drive

`filesystem list /` retourne les racines du compte :

```
/my-files               <- espace personnel principal (c'est celui qu'on utilise)
/devices                <- fichiers déposés par les clients de synchro officiels
                           (contient « Windows DESKTOP-01 » et « Windows MYPC_VM »
                            des installations WinBoat précédentes)
/shared-by-me
/shared-with-me
/trash
/albums
/photos
/photos-shared-by-me
/photos-shared-with-me
/photos-trash
```

**Décision** : on synchronise vers `/my-files`, pas `/devices`. La section `/devices` est réservée aux clients de synchro officiels (chaque sous-dossier a ses propres métadonnées machine). Y écrire via le CLI risquerait des conflits quand le client Linux natif sortira. `/my-files` est l'espace canonique et stable. Le partage avec User2 se fait via l'interface web depuis `/my-files`.

### Synchroniser vers un dossier « Partagé avec moi »

Un mapping peut aussi viser un dossier qu'une autre personne vous a partagé (`/shared-with-me/...`) — par exemple un espace collaboratif avec un sous-dossier par personne. Ce qui y fonctionne dépend de la version du CLI :

- **CLI 0.5.0 et suivants** : envoi, mise à jour sur place et **suppression** fonctionnent. Un élément supprimé part dans la corbeille **du propriétaire**, pas dans la vôtre.
- **CLI antérieur** : la suppression n'y est pas prise en charge. L'éditeur détecte ces destinations et verrouille le mapping en ajout seul — non par prudence, mais parce que chaque tentative échouerait une à une en descendant dans l'arbre, allongeant inutilement le passage.

> **⚠ La suppression sur un dossier partagé est potentiellement destructrice.** Un mapping écrit dans `<destination>/<nom de votre dossier source>` : c'est donc ce **sous-dossier**, et non le dossier partagé entier, que la suppression concerne. Avec la suppression activée, tout ce qui s'y trouve (et dans ses propres sous-dossiers) et qui est absent localement est supprimé et envoyé dans la **corbeille du propriétaire**, y compris les fichiers déposés par d'autres personnes. Ni le reste du dossier partagé ni le reste du Drive n'est concerné. Ne l'activez que sur un dossier dont vous êtes le seul contributeur, comme une transmission à sens unique vers son propriétaire. Tout autre usage risque d'effacer le travail d'autrui. L'application demande une confirmation explicite lorsque vous activez la suppression sur une telle destination, puis à nouveau si vous demandez à une réinitialisation d'en vider le dossier distant.

Deux choses ne changent pas, quelle que soit la version. Si le propriétaire n'a accordé qu'un accès en lecture, rien ne peut être écrit : plutôt que de tenter chaque fichier et d'échouer sur chacun, le moteur arrête ce mapping immédiatement avec un seul message invitant à demander les droits — le reste du passage se poursuit normalement. Et les emplacements de premier niveau (`/my-files`, `/shared-with-me`, `/photos`, `/devices`) sont des racines virtuelles fixes : elles existent toujours et ne peuvent pas être créées, donc le moteur ne tente jamais de le faire et descend simplement dedans.

---

## Validation du CLI

Tests effectués avant développement du moteur :

- **Connexion via NFS** : un fichier sur `/media/nas1/...` s'upload sans problème, le CLI traite les chemins NFS comme des chemins locaux. Validé en pratique sur le passage complet.
- **`upload --file-conflict-strategy replace`** retransfère le fichier inconditionnellement, **sans détection intelligente** : 8,8 secondes mesurées pour un fichier de 24 Ko déjà identique côté serveur. C'est ce qui justifie d'avoir un moteur en amont qui décide quoi uploader.
- **`filesystem list -j`** retourne du JSON exploitable.

### Structure JSON retournée par le CLI

Champs clés à extraire (les noms imbriqués `{ok: true, value: ...}` doivent être déballés) :

```json
{
  "name": {"ok": true, "value": "nom-du-fichier.pdf"},
  "type": "file",
  "totalStorageSize": 1500256,
  "modificationTime": "2026-05-29T16:44:25.000Z",
  "activeRevision": {
    "ok": true,
    "value": {
      "claimedSize": 1500196,
      "claimedModificationTime": "2016-02-29T21:42:04.000Z",
      "claimedDigests": {
        "sha1": "3910e3b8a6898cd995dd324f1cf1a65581b5c516",
        "sha1Verified": false
      }
    }
  }
}
```

**Pièges importants** (confirmés en pratique) :

- `totalStorageSize` = taille **chiffrée** stockée côté Proton (overhead de chiffrement). Ne correspond **pas** à la taille locale du fichier original. **Ne pas utiliser pour comparer** — c'était un bug initial qui faisait tout réuploader.
- `activeRevision.value.claimedSize` = taille du fichier original déclarée par le client à l'upload. **C'est cette valeur qu'il faut comparer** à `os.path.getsize(local)`.
- `claimedDigests.sha1` = hash SHA1 du contenu original. Permet une vérification de contenu sans dépendre de la date de modification (utile pour détecter les changements de tags musicaux qui préservent la date et la taille).

### Commandes utiles du CLI

```bash
# Authentification (ouvre le navigateur)
./proton-drive auth login

# Lister les racines du compte
./proton-drive filesystem list /

# Création de dossier (parent + nom en deux arguments séparés)
./proton-drive filesystem create-folder /chemin/parent NomDossier

# Liste avec sortie JSON
./proton-drive filesystem list -j /chemin/distant

# Upload avec stratégie de conflit
./proton-drive filesystem upload -f replace -d merge fichier1 fichier2 /chemin/distant
```

Stratégies de conflit disponibles : `merge`, `keep-both`, `replace`, `skip`. `-f` pour fichiers, `-d` pour dossiers, `-c` pour les deux.

---

## Architecture

```
mappings-user1.json          <- liste des dossiers à synchroniser
mappings-user2.json           (un fichier par profil, édité par GUI ou à la main)

proton_mapping_editor.py    <- GUI Tkinter (édition JSON, lancement, planification, temps réel)
proton_sync.py              <- moteur de synchro batch (coeur)
mount_check.py              <- garde-fou de montage (obligatoire pour les suppressions)
schedule_manager.py         <- backend GUI du timer nocturne (systemd --user)

# Couche temps réel
local_watcher.py            <- watcher inotify des sources locales (machine locale)
nas_watcher.py              <- watcher inotify des sources NAS (tourne sur le NAS)
realtime_consumer.py        <- consommateur : lit les marqueurs, debounce, lance le moteur
realtime_manager.py         <- backend GUI du temps réel (démons, config, push NAS, files)
proton-nas-watch.service    <- unité systemd du watcher NAS (à installer sur le NAS)

~/.proton_sync.lock         <- verrou (créé automatiquement, par utilisateur)
~/.proton_sync_cache/       <- cache des empreintes de dossiers (par fichier de mappings)
~/.proton_sync/queue/       <- file de marqueurs temps réel (locale)
~/.proton_sync/realtime.conf<- réglages temps réel (debounce, cycle) — écrits par le GUI
/media/home_nas/proton-sync/ <- côté NAS via NFS : config/ (mappings poussés) + queue/<compte>/
```

### Format du fichier de mappings

DEUX formats acceptés (rétrocompatibilité) :

**Ancien format** — liste simple (toujours supporté) :

```json
[
  {
    "type": "folder",
    "source": "/media/nas1/Documents/Vers_Proton/Communs",
    "dest_parent": "/my-files"
  },
  {
    "type": "file",
    "source": "/media/nas1/Conteneurs/Veracrypt-e1",
    "dest_parent": "/my-files/Sauvegardes/Conteneurs"
  }
]
```

**Nouveau format** — objet avec exclusions optionnelles :

```json
{
  "exclusions": {
    "names": [".caltrash", "trash", ".Trash-1000"],
    "patterns": ["*.tmp", "~*"]
  },
  "mappings": [
    {
      "type": "folder",
      "source": "/media/nas1/Documents/Vers_Proton/Communs",
      "dest_parent": "/my-files",
      "exclusions": { "names": [".specifique-a-ce-mapping"] },
      "allow_delete": true,
      "delete_mode": "trash",
      "source_kind": "nfs"
    }
  ]
}
```

- `type: folder` -> tout le contenu du dossier est synchronisé récursivement.
- `type: file` -> un fichier unique (utile pour les conteneurs VeraCrypt — choix délibéré de liste blanche par fichier individuel, pour éviter qu'un nouveau conteneur sensible ajouté plus tard ne se retrouve par erreur sur Proton).

**Champs de suppression (optionnels, par mapping)** — voir la section « Propagation des suppressions » plus bas :
- `allow_delete` : `true`/`false` (absent = false = additif, jamais de suppression). Autorise ce mapping à propager les suppressions locales vers Proton.
- `delete_mode` : `"trash"` (corbeille Proton, récupérable 30 j) ou `"permanent"` (définitif, irréversible). Le mode du mapping fait foi.
- `source_kind` : `"nfs"` ou `"local"`. Détecté automatiquement par le GUI, confirmé à l'édition. Sert au garde-fou : une source `nfs` ne supprime que si le montage réseau est vivant.

### Exclusions

Deux mécanismes combinés, s'appliquant aux **dossiers ET fichiers**, par **nom** (pas chemin complet) :

- `names` : noms EXACTS, insensible à la casse (ex. `.caltrash`, `trash`, `.Trash-1000`)
- `patterns` : motifs glob façon shell, insensible à la casse (ex. `*.tmp`, `.Trash-*`, `~*`)

Deux niveaux qui se cumulent :
- **Globales** : valent pour tous les mappings
- **Par mapping** : s'ajoutent aux globales, uniquement pour ce mapping (clé `exclusions` dans l'entrée)

Un dossier exclu n'est pas visité du tout (son contenu entier est ignoré). Nuance importante voulue : on n'exclut PAS aveuglément tous les fichiers cachés (commençant par `.`) — un `.config_important` désiré est conservé, tandis qu'un `.caltrash` listé explicitement est exclu.

**Nettoyage automatique avec `--delete`** : un fichier exclu localement mais déjà présent sur Proton (uploadé avant l'ajout de l'exclusion) est vu comme un **orphelin** au prochain passage `--delete` et part à la corbeille Proton (récupérable 30 jours). La **signature du cache intègre une empreinte du jeu d'exclusions** : tout changement d'exclusions périme `delete_synced` et force la réconciliation au passage `--delete` suivant — le nettoyage est donc automatique, sans « Ignorer cache ». Contrepartie : ce premier passage après un changement d'exclusions revérifie tous les dossiers (plus long, une fois), puis les sauts rapides reprennent. À garder en tête : affine tes exclusions si tu veux conserver sur Proton certains fichiers exclus localement — ce qu'on exclut finit par disparaître du backup.

**Garde-fou en temps réel (`sync_subpath`)** : quand le watcher cible directement un sous-chemin, le moteur teste **chaque segment** du chemin relatif à la racine du mapping — la cible elle-même (`__pycache__`, `logs`) **et ses ancêtres** (`.Trash-1000/info` est sauté parce que `.Trash-1000` matche `.Trash-*`). Le moteur émet alors une ligne portant le **tag stable `[subpath-excluded]`** (indépendant de la langue), que le consommateur détecte pour afficher « 🚫 exclu (nom filtré) — rien à synchroniser » au lieu d'un « ✓ ok » ambigu. Ni upload, ni création distante, ni suppression pour ces chemins.

Les exclusions se gèrent visuellement dans `proton_mapping_editor.py` (boutons « Exclusions globales » et « Exclusions du mapping »). Un catalogue de motifs recommandés par logiciel est maintenu à part dans `Temporary-files-exclusions.fr.md`.

---

## Comportement du moteur (`proton_sync.py`)

### Logique de détection

Pour chaque fichier local, le moteur fait `filesystem list -j` sur le dossier distant (une seule fois par dossier), récupère `claimedSize` et compare à `os.path.getsize(local)`. Si les tailles diffèrent, upload. Si le champ taille distante est introuvable, **upload par prudence** (préfère uploader trop que de manquer un changement).

Avec `--verify-hash`, ajoute une comparaison SHA1 quand les tailles correspondent — détecte les changements de contenu sans changement de taille (équivalent du `/MIR /IS` mensuel de robocopy). **Attention** : lit chaque fichier en entier (lent sur gros volume) ET ignore le cache. À réserver à une vérification périodique, pas au quotidien.

### Cache local (optimisation majeure)

**Problème résolu** : sans cache, le moteur fait un appel `filesystem list` par dossier visité, à ~1-2 s chacun. Sur l'arborescence de User1 (le seul dossier `Communs` contient **1890 sous-dossiers**), un passage « rien à faire » prendrait des dizaines de minutes à plusieurs heures.

**Solution** : `~/.proton_sync_cache/<nom_du_mapping>.cache` (JSON). Pour chaque dossier synchronisé avec succès, on stocke une empreinte : mtime du dossier + liste triée des (nom, taille, mtime) de ses fichiers directs. Au passage suivant, si l'empreinte locale est identique -> on **saute complètement l'appel CLI** (« ⚡ cache valide ») et on descend juste dans les sous-dossiers. Résultat : un passage sans changement passe de plusieurs heures à quelques secondes.

**Garde-fous** :
- Le cache n'est JAMAIS une source de vérité, juste un raccourci. Le supprimer force un re-scan complet.
- Un dossier dont un upload a échoué n'est PAS mis en cache -> automatiquement réessayé au prochain passage.
- Le `--dry-run` ne touche jamais au cache.
- Écriture atomique (tmp + rename) -> jamais de cache corrompu, même en cas de crash.
- `--ignore-cache` force un passage complet (et reconstruit le cache au fil de l'eau, entrée par entrée).

### Checkpoints (robustesse anti-crash)

Le cache est sauvegardé sur disque **après chaque entrée du mapping traitée** (pas seulement à la fin). Donc si la machine plante ou reçoit un `kill -9`, on garde tout le travail des mappings déjà traités, et la reprise saute ce qui est déjà en cache. Testé avec `kill -9` en plein passage : reprise propre.

### Verrou (anti-exécutions simultanées)

`flock` sur `~/.proton_sync.lock`. Empêche deux instances du moteur de tourner en même temps **sous le même utilisateur** (ex. cron qui se déclenche pendant un passage manuel). Le verrou est libéré automatiquement par l'OS à la fin du processus — propre, kill, Ctrl+C ou crash — donc pas de verrou orphelin. Comme c'est dans le home utilisateur, User1 et User2 (sessions Linux séparées) ne se bloquent pas mutuellement.

### Création automatique des dossiers

`ensure_remote_path()` parcourt le chemin distant segment par segment et crée chaque dossier manquant via `filesystem create-folder`. Récursif : chaque sous-dossier local découvert déclenche la même vérification côté distant. Aucune création préalable manuelle requise.

### Propagation des suppressions (`--delete`) — vrai miroir optionnel

Par défaut, le moteur est **additif** : il envoie les nouveautés et modifications, mais ne supprime jamais rien sur Proton. C'est un filet de sécurité (supprimer un fichier du NAS ne le perd pas sur Proton). Le mode `--delete` transforme ça en **miroir** : ce qui disparaît localement disparaît aussi sur Proton.

**Modèle à deux niveaux (important)** — une suppression ne se produit QUE si les DEUX conditions sont réunies :

1. **`--delete` au lancement** (l'interrupteur maître). Sans ce flag, AUCUNE suppression, peu importe le JSON.
2. **`allow_delete: true` sur le mapping** (dans le JSON, réglé via le GUI). Sans ça, ce mapping reste additif même avec `--delete`.

Ce double niveau est délibéré : le JSON déclare l'intention, la ligne de commande active la mécanique. Ça évite qu'une suppression parte par accident (ex. un passage planifié de routine).

**Mode de suppression** — défini par mapping via `delete_mode` :
- `"trash"` (défaut) : envoi à la corbeille Proton, récupérable 30 jours.
- `"permanent"` : suppression définitive, irréversible. Le mode du mapping fait foi dès que `--delete` est actif (pas de second flag).

**Garde-fou de montage (`mount_check.py`)** — la protection clé. Avant toute suppression dans un mapping, le moteur vérifie que la source est « saine » selon son `source_kind` :
- Si `source_kind: "nfs"`, la source DOIT être actuellement portée par un montage réseau vivant (nfs/nfs4). Si le NAS est déconnecté, le chemin retombe sur du local (ext4) et apparaît vide — le moteur détecte l'incohérence et **bloque toute suppression** dans ce mapping (les uploads, eux, continuent). C'est ce qui empêche la catastrophe « NAS tombé → tout semble supprimé → on vide le backup ».
- Si `source_kind: "local"`, on exige juste que la source existe et soit lisible.
- Si le type n'est pas déclaré, ou si `mount_check.py` est absent du dossier : suppression refusée par sécurité.

**`mount_check.py` DOIT être placé à côté de `proton_sync.py`** (même dossier). Sans lui, toutes les suppressions sont refusées (garde-fou).

**Interaction avec le cache (drapeau `delete_synced`)** — pour ne pas perdre la vitesse du cache en mode `--delete`, chaque entrée de cache porte un drapeau indiquant si le distant a déjà été réconcilié (orphelins traités) lors d'un passage `--delete`. Conséquence :
- Le **premier** passage `--delete` vérifie tout le distant (plus lent), puis marque les dossiers réconciliés.
- Les passages `--delete` **suivants** sautent les dossiers inchangés ET déjà réconciliés (rapides, comme un passage normal).
- Une suppression locale change l'empreinte du dossier → il est revérifié au prochain `--delete` → l'orphelin est propagé.
- Un passage SANS `--delete` ne marque pas les dossiers réconciliés → un `--delete` ultérieur rattrapera une suppression faite entre-temps. (Rétrocompatible avec les anciens caches, migrés au vol.)

**Pas de garde-fou anti-suppression-massive** : choix délibéré. Une suppression locale est considérée comme intentionnelle, et la fenêtre entre deux passages (synchro ponctuelle, pas continue) + la corbeille 30 j suffisent comme filets. Le seul garde-fou est celui du montage (panne technique, pas décision humaine).

**Mappings `file`** : un mapping de type fichier unique dont la source disparaît localement voit sa copie distante supprimée aussi (si `--delete` + `allow_delete`). Pour garder un fichier sur Proton tout en le retirant du NAS : retirer son entrée de mapping avant le prochain passage (l'ordre des opérations entre deux passages n'a pas d'importance).

**Test recommandé avant d'activer** : toujours un `--delete --dry-run` d'abord pour voir ce qui serait supprimé, sans rien effacer.

### Journal en temps réel

Le moteur force l'écriture immédiate de sa sortie (`line_buffering`), donc avec `| tee fichier.log` le journal se remplit en direct (et pas par gros blocs à la fin). Plus besoin du flag `-u`.

### Gestion des erreurs

- Source locale introuvable -> ignorée, message affiché, suite du traitement.
- Échec d'upload -> message d'erreur, le moteur continue avec les autres fichiers, et le dossier n'est pas mis en cache (réessai au prochain passage).
- **Erreurs 500 de l'API Proton** : observées pendant le premier passage (hoquets serveur temporaires côté `drive-api.proton.me`). Sans gravité — les fichiers concernés n'étant pas mis en cache, un simple relancement du script les rattrape. Confirmé en pratique : le 2e passage a rattrapé tous les fichiers manqués au 1er.
- Interruption par Ctrl+C -> sans danger : au prochain lancement, les fichiers déjà uploadés sont reconnus comme inchangés et sautés.
- **Noms de fichiers à métacaractères glob (accolades, crochets…)** : le CLI Proton applique une expansion de motif (glob) sur les chemins de fichiers locaux fournis à `filesystem upload`. Un nom comme `{43ed69b5-...}.xpi` (typique des extensions Thunderbird/Firefox) est alors interprété comme un motif, ne correspond à aucun fichier réel, et échoue avec « No paths matched ». **Correctif** : le moteur échappe automatiquement les métacaractères `{ } [ ] * ?` en les enveloppant en classes glob littérales (`{` → `[{]`, etc.) avant de passer le chemin au CLI. C'est un bug du CLI Proton (il ne devrait pas globber des chemins explicitement fournis) — à signaler à Proton ; en attendant, le contournement est transparent.

### Performance

- Le CLI a un coût fixe par invocation (~1-8 s, surtout latence réseau/authentification).
- Le moteur **regroupe les uploads** dans un seul appel par dossier (`upload f1 f2 f3 ... destParent`) pour amortir ce coût fixe.
- Le premier passage complet sur une arborescence vierge prend du temps (uploads + un `list` par dossier). Les passages suivants sont quasi instantanés grâce au cache.

### Utilisation

```bash
PROTON_DRIVE_CLI=~/Logiciels/Proton-drive/proton-drive \
python3 ~/Logiciels/Proton-drive/proton_sync.py \
~/Logiciels/Proton-drive/mappings-user1.json \
2>&1 | tee ~/Logiciels/Proton-drive/logs/sync-$(date +%Y%m%d-%H%M).log
```

Options :
- `--dry-run` : affiche ce qui serait fait sans rien transférer (et sans toucher au cache)
- `--verify-hash` : ajoute la vérification SHA1 (plus lent, lit chaque fichier ; ignore le cache ; usage mensuel)
- `--ignore-cache` : force la revérification complète côté Proton (reconstruit le cache au fil de l'eau)
- `--delete` : **interrupteur maître** de la propagation des suppressions. Sans lui, aucune suppression. Avec lui, chaque mapping ayant `allow_delete: true` propage ses suppressions locales vers Proton, selon son `delete_mode` (corbeille/définitif) et sous réserve du garde-fou de montage. Toujours tester avec `--dry-run` d'abord.
- `--subpath <dossier>` + `--mapping-source <source>` : ne traite qu'**un seul sous-dossier** d'un mapping donné, au lieu de tout balayer. Utilisé par la couche temps réel (le consommateur lance le moteur ciblé sur le dossier qui vient de changer).
- `--check-auth` : sonde **uniquement** l'authentification (le trousseau est-il déverrouillé ?) puis sort — code 0 = OK, code 2 = verrouillé. Ne prend pas le verrou, ne synchronise rien, ne touche pas au cache. Utilisé par le consommateur temps réel pour éviter de lancer des passages voués au code 2 quand la session n'est pas ouverte (réutilise exactement le test du moteur, pas de logique dupliquée).
- `-v` / `--verbose` : affiche aussi les fichiers `inchangé`, les sauts de cache, et le JSON brut du premier élément de chaque dossier
- `--no-rename-ext` : **désactive** la normalisation des extensions (voir ci-dessous ; active par défaut).

### Robustesse d'upload, vignettes et détection MIME (extensions)

Trois comportements ajoutés après des observations en production. Ils visent un backup dont les **aperçus** sont visibles dans Proton (web/mobile), pas seulement des fichiers récupérables.

**1. Détection MIME sensible à la casse de l'extension — normalisation automatique.** *Corrigé en amont dans le CLI 0.5.0 : le type de média est désormais détecté correctement même avec une extension en majuscules. Au premier lancement avec 0.5.0 ou plus récent, l'application désactive donc cette normalisation **d'office** en vous expliquant pourquoi — renommer vos propres fichiers n'est plus nécessaire. Vous pouvez la réactiver si vous préférez malgré tout normaliser vos extensions, ou pour réparer d'anciens téléversements envoyés en majuscules ; ce choix est alors conservé définitivement. La description ci-dessous vaut pour les versions antérieures du CLI.* Le CLI Proton déduit le type MIME de l'**extension**, mais de façon **sensible à la casse** : un fichier `DOC.PDF` ou `IMG.JPG` (extension majuscule) est mal typé (`application/octet-stream`), ce qui casse d'un coup **vignette, aperçu ET icône** dans les apps Proton — silencieusement, sans erreur. Confirmé côte à côte : `doc.pdf`/`photo.jpg` (minuscule) obtiennent leur type et leur aperçu, `DOC.PDF`/`PHOTO.JPG` (identiques) non. Vaut pour images **et** PDF (et vraisemblablement tout format à aperçu).
Pour régler ça à la racine **et** garder le cache cohérent, le moteur **renomme la SOURCE** : toute extension finale contenant des majuscules est mise en minuscule (`IMG_1949.JPG → IMG_1949.jpg`, base du nom inchangée). Un seul point d'injection (`sync_folder`) → couvre **manuel, amorçage/réinitialisation ET temps réel**. Sûretés : dossiers et fichiers **exclus** jamais touchés ; en cas de **collision** avec une cible existante on n'écrase **jamais** (suffixe `_ProtonEditExt`, puis compteur) ; `--dry-run` annonce sans renommer. **Portée (liste blanche).** Le seul usage restant étant de réparer d'*anciens* téléversements, et seuls les formats prévisualisables ayant jamais eu de vignette, la normalisation se limite aux extensions listées dans `rename_ext_whitelist` (images, vidéo, audio, documents — éditable dans ⚙ Configuration…). Renommer un `.CFG` de routeur ou un `.Backup` de téléphone modifierait un de vos fichiers sans rien réparer — et si un agent externe (application de sauvegarde de téléphone, `rsync`…) recrée le nom d'origine à chaque passe, le garde-fou anti-collision transforme un écrasement idempotent en **accumulation non bornée** : une copie suffixée de plus chaque nuit, sur le disque *et* sur le Drive (constaté en production, 12 copies d'un même fichier de 6 Ko). Liste vide = aucune restriction (comportement historique). Les **suffixes de doublon** ajoutés par les agents externes sont compris : `PHOTO.JPG (1)` est lu comme une photo et réparé en `PHOTO.jpg (1)`, le ` (1)` étant restitué intact — sans quoi `splitext` rend une extension `.JPG (1)` qui ne correspond à aucune liste. Chaque renommage est journalisé dans `~/.proton_sync/renamed-extensions.log`. Désactivable via `--no-rename-ext`. NB : renommer un fichier déjà monté jadis en majuscule laisse un orphelin distant (ancien nom), nettoyé par n'importe quel passage `--delete`.

> **Salve de marqueurs transitoire (temps réel).** La *première* normalisation d'un arbre renomme beaucoup de fichiers d'un coup ; chaque renommage est vu par le watcher comme un couple d'événements (`DEL` de l'ancien nom + `ADD` du nouveau), qui déposent des marqueurs temps réel. C'est **transitoire et auto-résorbant** : au passage suivant les fichiers sont déjà minuscules (plus de renommage, plus de marqueur), et les nouveaux fichiers arrivent presque toujours déjà en minuscule. La **déduplication par dossier** du consommateur borne d'ailleurs la salve — dix fichiers renommés dans un même dossier = **une** synchro de ce dossier, pas dix. Pour éviter la salve, faire la première normalisation via un **amorçage/réinitialisation** (passage `--delete`, consommateur en pause) plutôt que de laisser le temps réel tout découvrir.

**2. Vignettes impossibles pour certains formats (TIFF/HEIC/AVIF) — auto `--skip-thumbnails`.** Même avec une extension minuscule, le CLI **échoue la génération de vignette** pour ces formats sous Linux (`Failed to generate thumbnails … format not supported … require the OS codec`), et cet échec fait échouer **tout** l'upload du lot. Installer les codecs système (`libheif`, `libaom`, `libdav1d`, `libtiff`) **ne change rien** — vérifié : déjà installés, le TIFF échoue quand même ; le CLI (TypeScript/Bun) n'utilise pas les bibliothèques image du système. Réponse du moteur : sur cette signature précise, il **re-téléverse le fichier avec `--skip-thumbnails`** → le fichier est sauvegardé (intact, chiffré ; consultable avec une visionneuse tierce ou après téléchargement), seul l'aperçu **intégré** Proton manque. Pour un aperçu dans Proton, convertir en JPEG/PNG. Les fichiers concernés sont consignés `NO-THUMBNAIL` dans le journal d'échecs, avec la raison exacte.

**3. Isolation des échecs d'upload + journal dédié.** Sur un échec de lot, le CLI ne rapporte qu'un **compteur** (`N item(s) failed`), pas le fichier fautif — et la vraie raison est sur **stdout** (pas stderr). Le moteur relit alors le distant (saute ce qui est déjà monté), ré-essaie **fichier par fichier** pour nommer le coupable et capturer sa raison exacte, applique l'auto-`--skip-thumbnails` ci-dessus si pertinent, et consigne tout dans `~/.proton_sync/failures.log` (`❌ FAIL` = vrai échec ; `⚠ NO-THUMBNAIL` = monté sans vignette). Le GUI a une case **« ❗ Erreurs seules »** qui re-filtre l'affichage sur les seules lignes d'erreur.

**4. Le CLI peut se figer indéfiniment sur un envoi — disjoncteur côté moteur.** Constaté en production : sur un envoi de 2 Gio, le CLI s'est arrêté **plus de 4 heures** tout à la fin du transfert, verrou du moteur tenu pendant tout ce temps et toute la synchronisation à l'arrêt derrière, jusqu'à un `kill` manuel. Diagnostic : le CLI envoie des blocs de 4 Mio sur un **pool d'une vingtaine de connexions** ; quand l'une d'elles est purgée par un équipement intermédiaire pendant le silence de fin de transfert, la réponse n'arrive jamais, le processus dort dans `epoll_wait` et **aucun temporisateur TCP n'est armé** — rien, au niveau réseau, ne le réveillera. Le moteur surveille donc l'envoi qu'il a lancé (`Popen` + deux fils de drainage des tubes ; sans eux, un tampon plein à 64 Ko bloquerait le CLI, créant le problème qu'on veut résoudre) et l'interrompt après `cli_stall_minutes` d'**inactivité totale**, en retournant un échec pour que les marqueurs soient conservés et le dossier repris. Ce qui est échantillonné, c'est `rchar` dans `/proc/<pid>/io` (octets lus **par appel système**) : `read_bytes` est inutilisable — le cache de pages sert le fichier, si bien qu'il reste **figé plusieurs minutes en plein transfert sain**. Le débit instantané ne discrimine pas davantage (une fin d'envoi saine n'avance plus que de quelques Ko/min, même ordre de grandeur qu'un blocage) : **seule la durée les sépare** — mesurée à 1 min 24 s pour une finalisation saine, contre des heures pour un blocage. `cli_stall_max_kills` borne les tentatives *consécutives* sur une même destination : au-delà de la limite un passage est sauté et le compteur repart — le dossier n'est **jamais** abandonné définitivement, une sauvegarde qui cesserait silencieusement de sauvegarder étant pire que la bande passante qu'elle économiserait.

**5. Un listing distant en échec n'est plus pris pour un dossier vide.** Un `filesystem list` sans réponse était indiscernable d'un dossier réellement vide, et le moteur en concluait « rien n'est encore là-haut ». Conséquences : un dossier entier **renvoyé** ; après un lot en échec, tous les fichiers ré-essayés un par un **y compris ceux déjà montés** ; un mapping de type fichier renvoyé à chaque passage ; dans le navigateur de destination du GUI, « (aucun sous-dossier) » affiché **et mémorisé**, si bien que redéplier le nœud ne changeait rien (il fallait fermer et rouvrir la fenêtre) ; et le message d'avant-suppression annonçant « **0 élément** » pour un dossier qu'il n'avait pas réussi à compter. Le listing porte désormais l'information « la lecture a-t-elle réussi », et chaque appelant en tient compte : le dossier est sauté (rien envoyé, rien supprimé, une ligne au journal, repris au passage suivant), le fichier est sauté, pas de ré-essai de lot à l'aveugle, le comptage annonce « non mesuré », et le navigateur **retente une fois** avant d'afficher un message distinct sans mémoriser l'échec. À noter : le sens de l'erreur était déjà sûr — les suppressions se déduisent de ce qui existe **à distance**, donc un listing vide ne fournit aucun candidat ; un marqueur dit seulement *« regarde ce dossier »*, il ne nomme jamais de fichier à supprimer.

**Matrice observée (trois niveaux distincts).** « Pas de vignette » ≠ « pas consultable » : le type MIME, la vignette et le visualiseur intégré sont trois choses séparées. Observé (extension minuscule correcte) :

| Format (ext. minuscule) | Type / icône | Vignette | Visualiseur intégré |
|---|---|---|---|
| jpg / png / webp | oui | oui | oui |
| pdf | oui (icône PDF) | — (pas une image) | oui |
| bmp | oui | **non** | **oui (s'ouvre)** |
| tiff / heic / avif | oui | non (codec) | non (« erreur interne ») |

Règle transversale : **tout dépend d'abord de l'extension minuscule** (sinon mauvais type → ni icône, ni vignette, ni aperçu). Ensuite, qu'un fichier bien typé ait une vignette ou s'ouvre dans le visualiseur dépend du support par format côté Proton. Le BMP est l'exemple parlant : bon type, pas de vignette, mais consultable.

---

## Statut actuel du projet

- OK : CLI installé et authentifié sur Mint (User1 + User2, comptes Linux distincts)
- OK : test d'upload depuis montage NFS réussi
- OK : `proton_mapping_editor.py` fonctionnel (édition mappings + lancement synchro intégré + gestion exclusions)
- OK : `proton_sync.py` calibré sur la vraie sortie JSON (claimedSize), avec cache, checkpoints, verrou, journal temps réel, vérification d'auth, exclusions
- OK : **premier passage complet réussi** sur `mappings-user1.json` (~2960 dossiers en cache) ET sur `mappings-user2.json`
- OK : 2e passage validé — cache instantané (~1m40 pour User1 tout en cache) + rattrapage automatique des erreurs 500
- OK : **automatisation systemd opérationnelle** — timers `--user` armés pour User1 (3h00) et User2 (3h02), linger activé pour les deux
- OK : **exclusions** (corbeilles, fichiers temporaires) implémentées et testées
- OK : **propagation des suppressions (`--delete`)** — garde-fou de montage (`mount_check.py`), modes corbeille/définitif par mapping, cache enrichi `delete_synced`, dry-run validé en conditions réelles
- OK : **correctif glob** — noms de fichiers à accolades (extensions Thunderbird) désormais uploadés correctement
- OK : **couche temps réel** — watchers inotify (local + NAS), file de marqueurs, consommateur à debounce, chaîne complète validée en production sur les deux profils (sources locales ET NAS)
- OK : **fenêtre « ⚡ Temps réel… »** — 5 sections (démons, délais, push NAS + dérive, observation NAS, files) + journal d'événements en direct, rafraîchissement auto, taille adaptée à l'écran
- OK : **démons systemd** — `proton-watch` + `proton-consume` (`--user`, installés/pilotés depuis le GUI) ; `proton-nas-watch` (système, sur le NAS, observé sans SSH)
- OK : **push auto des mappings vers le NAS** à l'enregistrement (+ hash de version, indicateur de dérive)
- OK : **fréquence de planification réglable** (quotidien / hebdomadaire / horaire) depuis le GUI ; install du timer GUI-first
- OK : **correctif file NAS par compte** (le consommateur lit `queue/<compte>` déduit du mapping, plus `$USER`) ; **en-tête par mapping** rétabli dans la sortie du moteur
- OK : **court-circuit d'authentification** — quand le trousseau est verrouillé, le consommateur écrit une seule ligne « en attente d'ouverture de session » et reprend à la connexion (plus de rafales « code 2 ») ; sonde `--check-auth` réutilisant le test du moteur ; **validé au reboot**
- OK : **relance du passage planifié en cas de collision de verrou** — service en `Type=exec` + `Restart=on-failure` + `RestartSec=120` (borné par `StartLimitBurst`) : une collision avec le temps réel ne saute plus le passage nocturne ; installé User1 + User2
- OK : **watcher local conscient des montages** — surveillance immédiate + ré-scan adaptatif : rattrapage des sources NAS montées tardivement au boot, et suivi des montages qui tombent/remontent en session (journal `➕`/`➖`/`🔄`) ; **validé en pleine course au boot**
- OK : **garde-fou d'exclusion en temps réel** — `sync_subpath` teste la cible ET ses ancêtres (tag stable `[subpath-excluded]`), le consommateur affiche « 🚫 exclu » ; validé en prod (`logs`, `__pycache__`, `.Trash-1000/info`)
- OK : **cache conscient des exclusions** — l'empreinte du jeu d'exclusions entre dans la signature : un changement d'exclusions force la réconciliation au prochain `--delete` (nettoyage automatique des orphelins nouvellement exclus, ex. `.dtrash`, `thumbnails-digikam.db`)
- OK : **panneau « Journal des passages »** — dernière exécution par frontière de démarrage (fiable après reboot), sélecteur de date, résumé succès/échec ; validé (la collision du 1er juillet y est visible)
- OK : **internationalisation FR/EN complète** — GUI, moteur, démons, descriptions systemd ; sélecteur « 🌍 Language… », catalogue gettext (379 messages), tag stable et marqueurs multilingues pour les détections ; validée en prod sur les deux langues
- À FAIRE (optionnel) : décider d'activer ou non `--delete` dans la planification (voir Option A / Option B ci-dessous)
- À FAIRE (optionnel) : vérification `--verify-hash` périodique à planifier (équivalent /IS mensuel)
- À FAIRE (optionnel) : nettoyer les `.caltrash` déjà uploadés avant l'ajout des exclusions
- À FAIRE (différé, choix de User1) : watchers conscients des exclusions ; vue « ne montrer que ce qui est synchronisé » dans le journal temps réel

---

## Amorçage, garde-fou de complétude et pilotage (session juillet 2026)

Cette section décrit les comportements ajoutés lors de la mise en production du pilotage complet depuis le GUI. Ils reposent tous sur un principe directeur : **le temps réel synchronise le quotidien (changements ciblés sur des zones déjà connues), la planification réconcilie et bâtit l'arborescence.** Le temps réel ne construit jamais un gros sous-arbre inconnu.

### Complétude d'arbre (`subtree_complete`) — le garde-fou central

Le cache marque désormais, pour chaque dossier, un champ **`subtree_complete`** : vrai seulement si le dossier a été entièrement parcouru sans échec ET que tous ses enfants non exclus sont eux-mêmes complets. La complétude **remonte de bas en haut** : une racine complète implique que tout son arbre l'est.

En temps réel (`--subpath`), le moteur teste la complétude du **parent** du sous-chemin ciblé :
- Si le parent est complet, un **nouveau** dossier créé dedans est traité immédiatement (le cache de référence existe).
- Si la zone n'a **pas encore été analysée** (racine « tiède » héritée, enfants jamais parcourus), le temps réel **diffère au passage planifié** (code 3, message « dossier pas encore analysé — différé »). Il ne lance jamais un long parcours de découverte.

Ainsi, un mapping volumineux jamais consolidé ne bloque plus le temps réel : il attend un passage complet (amorçage ou planification). C'est le comportement qui a résolu l'emballement initial des gros mappings.

### Amorçage du cache (bouton « Amorcer le cache »)

Le GUI permet d'**amorcer** un ou plusieurs mappings sélectionnés : un passage complet `--delete` (corbeille) restreint aux mappings choisis via le nouvel argument moteur **`--only-source`** (répétable). L'amorçage marque `subtree_complete` et rend ces mappings « prêts pour le temps réel » dès la première passe (la suppression y est opérationnelle immédiatement pour les mappings qui l'autorisent).

L'orchestration est entièrement automatique et annoncée dans la sortie :
1. Vérification de la session Proton (arrêt net si expirée).
2. Arrêt du **consommateur uniquement** — le **watcher reste actif** : les vrais changements locaux sur les mappings déjà prêts continuent d'être captés (marqueurs conservés, traités au retour du consommateur) au lieu d'être manqués.
3. Mise en pause du timer planifié (arrêt non destructif).
4. Amorçage avec **progression visible** : compteur de dossiers analysés (lu du cache) + dossier courant, affichage épuré (un chemin par dossier).
5. Redémarrage du consommateur, **réarmement du timer** (`restart` recalcule le prochain `OnCalendar` — un simple `start` laisserait `Trigger: n/a`).
6. Bilan « X/Y mappings prêts ».

Sans sélection, l'amorçage porte sur tous les mappings. La sélection multiple se fait par Ctrl+A / Ctrl+clic / Maj+clic.

### Indicateur d'état par mapping (colonne « Prêt » ✅/⏳/—)

Le tableau des mappings affiche une colonne d'état :
- **✅** : mapping dossier prêt pour le temps réel (racine `subtree_complete` ET empreinte d'exclusions courante identique à celle stockée dans le cache) ;
- **⏳** : à amorcer (jamais analysé, OU exclusions changées depuis la consolidation — voir ci-dessous) ;
- **—** : mapping de type fichier (pas d'arbre à analyser, géré directement en temps réel, aucun amorçage requis).

L'indicateur **recalcule l'empreinte d'exclusions effective courante** (globales + propres au mapping, via la classe `Exclusions` du moteur réutilisée) et la compare à celle du cache. Ainsi, **modifier une exclusion fait immédiatement repasser un mapping de ✅ à ⏳** : c'est un rappel visuel qu'il doit être ré-amorcé. Le rafraîchissement se fait sur les événements clés (édition mapping, ajout/retrait d'exclusion globale ou du mapping, ajout/suppression de mapping), **pendant l'amorçage** (les mappings passent à ✅ un par un — indicateur d'avancement mapping par mapping), et par un timer lent (~30 s) pour capter les consolidations survenues hors GUI (planification, temps réel).

### Authentification Proton par navigateur (bouton « Se connecter à Proton »)

L'authentification du CLI se fait **par navigateur** : aucun identifiant ne transite par ce logiciel (mot de passe et 2FA restent entre le navigateur et Proton). Le GUI expose un bouton qui lance `proton-drive auth login`, diffuse sa sortie (URL de secours copiable + message de succès) et met à jour un indicateur « connecté / session expirée ».

- **Détection automatique** au démarrage et au retour de focus de la fenêtre (anti-rebond) : l'affichage ne reste jamais périmé.
- **Source de vérité = le résultat réel.** La sonde `--check-auth` (rendue indépendante du fichier de mappings) peut donner un faux positif si le CLI Proton garde un token en cache. Le moteur émet donc un **tag stable `[auth-failed]`** lorsqu'un vrai passage échoue à s'authentifier ; le GUI le détecte et corrige l'indicateur vers « session expirée », qui fait alors autorité sur la sonde de démarrage.

### Persistance progressive du cache

Le cache est écrit **au fil de l'eau** (throttlé), et un gestionnaire de signaux (SIGTERM/SIGINT) le sauve avant de quitter. Une interruption (Ctrl+C, arrêt du service, coupure) ne fait donc **plus repartir de zéro** :
- À l'interruption : « ⏹ Interrompu — progression du cache enregistrée » (affiché seulement si l'écriture a réussi, jamais de fausse promesse).
- Au démarrage sur un cache peuplé : « ↺ Reprise sur un cache existant — le travail déjà enregistré ne sera pas refait » (couvre le cas d'une coupure de courant où le message d'interruption n'a pas pu s'afficher).

### Dédoublonnage des marqueurs à l'écriture

Le watcher ne redépose pas un marqueur strictement identique (même chemin ET même intention de suppression) s'il est déjà en attente. Ajouts et suppressions ne sont jamais fusionnés. Un marqueur reste une **invitation à regarder** un dossier, jamais une garantie de travail : l'état du disque au moment du traitement fait foi.

### Affichage épuré unifié (amorçage + passage manuel)

Par défaut (*Détaillé* décoché), la sortie GUI n'affiche qu'**un chemin par dossier traité** (plus les messages d'orchestration et les erreurs), en masquant le détail fichier par fichier et les sorties techniques du CLI. *Détaillé* coché rétablit l'affichage brut complet. Cette case se trouve dans la barre de la zone de sortie, à côté de *Erreurs seulement*, car c'est un contrôle de **vue** : elle s'applique à tout ce qui s'y affiche — synchro manuelle, amorçage et réinitialisation — et agit sur les lignes à venir, pas sur celles déjà affichées. **Le log disque conserve toujours la sortie complète.** Un message « ✓ Rien à mettre à jour » évite l'écran vide quand tout est déjà synchronisé.

### Notes d'infrastructure

- **Session/trousseau Proton.** Le jeton de session a une durée de vie limitée et le trousseau (GNOME Keyring) peut se verrouiller avec la session. Un passage planifié qui se déclenche alors que le trousseau est verrouillé ne peut pas s'authentifier (marqueurs conservés, aucun envoi). Pour des passages **planifiés sans supervision** (nuit), il faut soit empêcher le trousseau de se verrouiller automatiquement, soit planifier aux heures où la session est active et déverrouillée.
- **La planification est le filet.** Avec `subtree_complete`, le temps réel diffère les zones non prêtes ; c'est la planification (ou un passage manuel) qui les réconcilie et les construit. Garder une planification active est donc nécessaire au bon fonctionnement d'ensemble — ce qui a toujours été le rôle du passage complet.
- **Piège systemd.** Un `systemctl start` sur un timer `OnCalendar` ne recalcule pas son prochain déclenchement (état `Trigger: n/a`). Il faut `restart` pour réarmer le calendrier.


---

## Synchronisation temps réel

Le moteur ci-dessus est **batch** : il balaie à chaque passage. Depuis le 30 juin 2026, une **couche temps réel** s'y ajoute pour synchroniser **dès qu'un fichier change**, sans attendre le passage planifié. Le batch devient alors un simple **filet** (voir « Planification » : passage hebdomadaire), tandis que le temps réel assure le quotidien.

### Principe et surveillance distribuée

La surveillance repose sur **inotify**, avec une répartition entre deux watchers dictée par le comportement de l'inotify au-dessus du NFS :

- Le **watcher de la machine locale** (`local_watcher.py`) surveille les sources **locales** (ext4) **et** les sources **NAS montées en NFS** (`/media/nas1…`). Il capte de façon fiable tout ce qui est écrit **depuis la machine locale**, y compris les modifications en place.
- Le **watcher du NAS** (`nas_watcher.py`) surveille le **disque local du NAS**. Il capte ce qui est écrit **directement sur le NAS** (dépôts SFTP d'un téléphone, processus locaux, autres clients) — ce que la machine locale ne voit pas.

Les deux sont **complémentaires**. Subtilité : l'inotify du NAS voit les opérations **structurelles** (création, suppression, renommage) faites par la machine locale via NFS — car `nfsd` les exécute comme de vraies opérations locales — mais **pas** les modifications en place, que seul la machine locale rattrape. Une même opération structurelle peut donc être vue par les deux watchers ; le consommateur **déduplique par dossier**, sans double envoi. Détails dans `INSTALLATION-realtime.fr.md` (« Qui voit quoi : les deux watchers et le NFS »).

### Architecture en couches

```
local_watcher.py     (machine locale)  inotify sur les sources locales ET NAS (NFS) -> marqueurs
nas_watcher.py       (NAS)      inotify sur le disque local du NAS -> dépose des marqueurs
                                 (file partagée, vue en NFS côté machine locale)
realtime_consumer.py (machine locale)  lit les marqueurs, debounce, déduplique,
                                 lance le moteur sur le sous-dossier concerné (--subpath)
realtime_manager.py  (machine locale)  backend GUI : install/contrôle des démons, config,
                                 push NAS, dérive de version, files
```

### File de marqueurs

Un **marqueur** est un petit fichier JSON `{"path": "...", "delete": bool}` déposé par un watcher quand un dossier change. Pour une suppression, le marqueur pointe le **dossier parent** : le moteur constate l'absence au passage, sans avoir besoin d'information sur le fichier disparu.

- File locale (machine locale) : `~/.proton_sync/queue/`
- File NAS : `/home/nasuser/proton-sync/queue/<compte>/`, vue en NFS sur la machine locale sous `/media/home_nas/proton-sync/queue/<compte>/`

**Identité = nom de compte, pas login Unix.** Le `<compte>` vient du nom du fichier de mappings (`mappings-user1.json` → `user1`) — convention partagée par le watcher NAS (qui écrit dans `queue/user1`), le consommateur (qui y lit) et le GUI. C'est volontairement **indépendant du login Linux**, qui peut différer (ex. `myuser` pour le compte `user1`) : s'appuyer sur `$USER` ferait lire la mauvaise file NAS. (Bug corrigé : le consommateur déduit désormais le compte du fichier de mappings, plus de `$USER`.)

### Le consommateur (`realtime_consumer.py`)

Tourne en boucle (cycle ~30 s) :

- relit sa config `~/.proton_sync/realtime.conf` (JSON `debounce_seconds`, `cycle_seconds`) **à chaque cycle** → réglage à chaud, sans redémarrage ;
- regroupe les marqueurs par dossier, applique le **debounce** (laisse retomber les rafales d'écritures avant d'agir), fusionne les conflits avec la règle **`delete=true` l'emporte** ;
- lance le moteur sur le seul sous-dossier mûr via `--subpath <dossier> --mapping-source <source>` (et `--delete` si le mapping l'autorise) — donc pas de balayage complet, juste ce qui a bougé.

Un marqueur n'est effacé qu'**après** un passage réussi ; un échec (typiquement le verrou tenu par un passage manuel) **conserve** le marqueur pour réessai au cycle suivant.

### Copie des mappings vers le NAS (+ dérive de version)

Le watcher NAS doit connaître les mappings pour savoir quels dossiers surveiller. Le GUI **pousse** donc la copie du fichier de mappings actif vers `/home/nasuser/proton-sync/config/mappings-<compte>.json` (le watcher NAS y découvre les comptes par `glob mappings-*.json` et recharge à chaud). Ce push est **automatique à chaque enregistrement** du mapping (et disponible à la demande via un bouton). Un **hash de version** (sha256, dans un sidecar `.version`) permet au GUI d'afficher la **dérive** : 🟢 à jour / 🟠 local modifié non poussé / 🔴 NAS injoignable.

### Fenêtre de pilotage (« ⚡ Temps réel… »)

Bouton dans la barre d'outils du GUI, à côté de « ⏰ Planification… ». Cinq sections :

1. **Démons de la machine locale** : installer/mettre à jour, démarrer, arrêter, redémarrer, désactiver le démarrage auto.
2. **Délais** : debounce + cycle (appliqués à chaud).
3. **Mappings vers le NAS** : push manuel + indicateur de dérive.
4. **Watcher NAS (observation)** : voyant de joignabilité + dernière activité (lecture seule).
5. **Files de marqueurs** : compte + nettoyage.

Plus une zone **« Événements temps réel »** qui suit en direct le journal des démons (`journalctl --user -f` sur les deux units), et un rappel du **linger**. La fenêtre se rafraîchit automatiquement et s'adapte à la résolution de l'écran (utile pour le poste de User2, en résolution réduite).

### Démons et systemd

- **machine locale** : deux services `systemd --user`, `proton-watch.service` (→ `local_watcher.py`) et `proton-consume.service` (→ `realtime_consumer.py`), installés et pilotés **depuis le GUI** (le bouton « Installer / Mettre à jour » les génère, recharge systemd et les active). `Restart=on-failure`, relance à l'ouverture de session.
  - **Watcher local conscient des montages** : il surveille immédiatement les sources déjà montées (les locales dès le démarrage) puis **ré-scanne** les montages — vite au boot pour rattraper les sources NAS dès qu'elles apparaissent (une course au boot ne le laisse plus aveugle au NAS pour toute la session), puis tranquillement pour prendre en compte un montage qui tombe ou remonte en cours de session. Chaque changement est journalisé (`➕` ajout, `➖` retrait, `🔄` ré-scan).
  - **Consommateur et trousseau verrouillé** : si la session n'est pas ouverte (trousseau verrouillé), le consommateur ne lance aucun passage voué à l'échec ; il écrit **une seule** ligne « ⏳ En attente d'ouverture de session », conserve les marqueurs, et **reprend automatiquement** à la connexion (« 🔓 Session ouverte — reprise »). Il sonde l'authentification via `proton_sync.py --check-auth`, la même détection que le moteur (pas de logique dupliquée).
- **NAS** : un service **système** `proton-nas-watch.service` (sous le compte `nas`), installé **manuellement sur le NAS** (démarre au boot, sans session). Le GUI ne le pilote pas — pas de SSH, pas d'identifiants à distance — il l'**observe** via la file NFS. Chaque machine garde ainsi ses propres démons vivants.
- **Linger** : nécessaire pour que les démons (et le timer nocturne) tournent hors session ouverte. Affiché et rappelé par le GUI ; activation `sudo loginctl enable-linger <user>` (une fois, droits admin).

**Persistance après un redémarrage** : pour que le temps réel reparte au boot, trois maillons doivent tenir — réseau au boot, **NAS monté au boot** (montage NFS en `/etc/fstab` avec `_netdev` ; un montage à l'ouverture de session laisse les démons démarrer sur des points vides), et trousseau (qui ne se déverrouille qu'à l'ouverture de session graphique). Sans session, les marqueurs s'accumulent dans la file (rien perdu) et se traitent à la connexion. Même en cas de **course au boot** (watcher démarré avant que le montage NFS soit prêt), le watcher local **rattrape** les sources NAS dès qu'elles apparaissent grâce à son ré-scan (voir plus haut), donc aucune source n'est perdue pour la session. Détail complet, fstab et commandes de vérification : `INSTALLATION-realtime.fr.md`, section « Persistance après un redémarrage ».

Installation détaillée : `INSTALLATION-realtime.fr.md`.

### Interaction avec le batch et le verrou

Temps réel, batch planifié et lancements manuels partagent le **verrou** `~/.proton_sync.lock` : jamais deux passages en parallèle sous le même utilisateur. Si un passage manuel tient le verrou, le consommateur **conserve** ses marqueurs et réessaie au cycle suivant — comportement sûr, observé en production (aucune perte).

Cas symétrique côté **planifié** : si le consommateur temps réel tient le verrou au moment du déclenchement du timer, le passage planifié sort en échec (code 1). Son service systemd est donc en `Type=exec` avec `Restart=on-failure` + `RestartSec=120` : il se **relance automatiquement ~2 min plus tard**, le temps que le consommateur ait fini et libéré le verrou (borné par `StartLimitBurst` pour éviter toute boucle). Sans ça, une seule collision suffisait à sauter tout le passage nocturne. Un code 2 (trousseau verrouillé) reste traité en succès (`SuccessExitStatus=0 2`) et ne déclenche donc pas de relance inutile.

### Limite connue (exclusions)

Les watchers **n'appliquent pas** les exclusions : ils déposent un marqueur pour tout changement, et c'est le **moteur** qui filtre. Depuis le garde-fou `sync_subpath` (voir « Exclusions » plus haut), un marqueur sur un chemin exclu — la cible ou un de ses ancêtres — est **sauté proprement** par le moteur (tag `[subpath-excluded]`) et le journal affiche « 🚫 exclu » au lieu d'un faux « ✓ ok ». Il reste donc un peu de **bruit de marqueurs** (une ligne `→ synchro` + `🚫 exclu` par rafale d'écritures exclues), mais plus aucun upload ni dossier distant créé. Rendre les watchers **conscients des exclusions** — ne plus créer le marqueur du tout — reste une amélioration **différée** (voir « Idées pour la suite »).

---

## Planification automatique (COMPLÉTÉE)

**Le problème connu** (confirmé par des discussions Reddit sur « PD CLI on Linux Desktop - Cron ») : le CLI a besoin du trousseau de secrets déverrouillé, ce que **cron classique ne fournit pas** (environnement minimal, pas de session graphique, pas de trousseau). Le contournement `bash -ic` vu sur Reddit règle les variables d'environnement mais PAS l'accès au trousseau — c'est la vraie racine du problème.

**Approche retenue (meilleure que cron), maintenant en place** :
- **Timer systemd `--user`** (pas cron) — tourne dans le contexte de la session utilisateur, avec accès naturel au trousseau. Fichiers `proton-sync.service` + `proton-sync.timer`, installés dans `~/.config/systemd/user/`.
- **`loginctl enable-linger myuser` et `user2`** — pour que les services utilisateur persistent même sans login graphique actif (activé via la session de User1 qui est sudoer, car User2 ne l'est pas).
- **Déverrouillage du trousseau** : choix retenu = **session graphique gardée ouverte en permanence sur la machine locale** (mini-PC dédié maison). Le trousseau reste déverrouillé, pas de compromis de sécurité.

**Gestion d'une session non ouverte (anti-blocage)** : le moteur fait un test d'authentification (`filesystem list /`) au démarrage. Si le trousseau est verrouillé (session non ouverte), il sort proprement avec le **code 2** et un message clair, sans rien uploader ni toucher au cache. Le service déclare `SuccessExitStatus=0 2`, donc systemd ne marque pas le service en échec. La tâche réessaie au prochain déclenchement (ou dès réouverture de session). Aucun blocage, aucune corruption.

Voir le guide détaillé `INSTALLATION-systemd.fr.md` et `INSTALLATION-realtime.fr.md` pour la couche temps réel.

**Installation et réglage depuis le GUI (chemin principal)** : la fenêtre « ⏰ Planification… » installe/met à jour le timer en un clic (elle **génère** `proton-sync.service` + `.timer`, recharge systemd et active le timer — aucune copie manuelle). Elle permet aussi de choisir la **fréquence** : quotidien, **hebdomadaire** (jour + heure au choix), ou toutes les heures. La copie manuelle (`cp` des unités) reste documentée comme repli.

**Détails systemd retenus** :
- `OnCalendar` **configurable depuis le GUI** : `*-*-* 03:00:00` (quotidien 3h) à l'origine ; avec la couche temps réel qui assure le quotidien, ce passage sert désormais de **filet** et se règle volontiers en **hebdomadaire** (ex. `Sun *-*-* 03:00:00`).
- `Persistent=true` (rattrape si la machine était éteinte — précieux pour un filet hebdo : un passage manqué se rejoue au démarrage suivant)
- `RandomizedDelaySec=300` (décale User1 et User2 de quelques minutes pour ne pas frapper l'API en même temps)
- `TimeoutStartSec=6h` (garde-fou contre un blocage)

### Journal des passages (GUI)

Le bouton **« 📜 Journal des passages… »** (fenêtre Planification) ouvre un panneau de consultation du journal du service planifié — sans fichier log dédié : il lit le **journal systemd** (borné et auto-purgé, plusieurs mois d'historique en pratique).

- **Vue par défaut : la dernière exécution**, isolée par sa **frontière de démarrage** (dernière ligne « Starting/Started proton-sync.service » du journal) — fiable même après un reboot, contrairement à l'état runtime de systemd (`InvocationID`), vidé à chaque redémarrage.
- **Résumé en tête** : date + résultat (« ✅ succès », « ❌ échec (code 1) » pour une collision de verrou, « ⏹ interrompu »). Les marqueurs de détection reconnaissent le **français et l'anglais** (l'historique du journal reste lisible après le passage à l'i18n).
- **Sélecteur de date** (calendrier zenity) pour remonter à un jour précis.

C'est ce panneau qui rend visible d'un coup d'œil un passage nocturne sauté (collision de verrou), auparavant repérable seulement via `journalctl`.

### Suppression et planification : Option A (actuelle) vs Option B

Point CRUCIAL à comprendre : **configurer `allow_delete: true` dans le JSON ne suffit PAS pour que la planification supprime.** Le moteur n'exécute la propagation des suppressions QUE si le flag `--delete` est passé au lancement. Le service systemd contrôle donc, à lui seul, si la planification nocturne supprime ou non — indépendamment de ce que contient le JSON.

**Option A — Planification ADDITIVE (configuration actuelle, recommandée au début)**

Le service systemd lance le moteur SANS `--delete` :
```
ExecStart=/usr/bin/python3 %h/Logiciels/Proton-drive/proton_sync.py %h/Logiciels/Proton-drive/mappings-user1.json
```
Conséquence : les passages de 3h sont purement additifs (envoient, ne suppriment jamais). Les `allow_delete: true` du JSON restent dormants la nuit. Les suppressions ne se propagent QUE lorsque l'utilisateur lance manuellement avec `--delete` (via le GUI en cochant « Propager suppressions », ou en ligne de commande). C'est le mode prudent : on contrôle chaque suppression, on les voit partir.

**Option B — Planification MIROIR (suppressions automatiques la nuit)**

Le service systemd lance le moteur AVEC `--delete` :
```
ExecStart=/usr/bin/python3 %h/Logiciels/Proton-drive/proton_sync.py %h/Logiciels/Proton-drive/mappings-user1.json --delete
```
Conséquence : le passage de 3h devient un vrai miroir. Ce qui est supprimé localement disparaît de Proton la nuit suivante (selon le `delete_mode` de chaque mapping, et sous réserve du garde-fou de montage). Filets de sécurité : la fenêtre de plusieurs heures avant 3h pour réaliser une erreur, plus la corbeille Proton 30 j (pour les mappings en mode `trash`).

Pour basculer de A vers B : éditer `~/.config/systemd/user/proton-sync.service`, ajouter `--delete` à la fin de la ligne `ExecStart`, puis :
```bash
systemctl --user daemon-reload
systemctl --user restart proton-sync.timer
```
**Prérequis avant de passer en Option B** : avoir fait plusieurs passages `--delete` manuels sans surprise, et s'assurer que `mount_check.py` est bien présent à côté de `proton_sync.py` (sinon les suppressions sont refusées). À décider séparément pour User1 et pour User2.

**Choix actuel : Option A** (planification additive, suppressions manuelles uniquement).

---

## Internationalisation (i18n)

Le projet est **bilingue français/anglais**, via **GNU gettext** (module Python standard) :

- **Langue source = anglais** (les chaînes du code, `msgid`) — convention GitHub : les futurs traducteurs partent de l'anglais. Le **français** est restitué par le catalogue `locale/fr/LC_MESSAGES/proton-sync.po` (source de traduction, éditable avec Poedit) compilé en `.mo` (binaire livré).
- **Résolution de la langue**, identique partout (GUI, moteur, démons) via `i18n.py` : **1)** préférence explicite dans `settings.json` (`{"language": "fr"}`) → **2)** sinon langue du système (`LANG`) → **3)** sinon anglais. Le sélecteur **« 🌍 Language… »** du GUI écrit `settings.json` ; le changement s'applique au **prochain lancement** du GUI et au **prochain redémarrage** des démons.
- **Cas du NAS (sans GUI)** : le watcher NAS suit la langue du système du NAS ; pour forcer, déposer à la main un `settings.json` à côté (`echo '{"language": "fr"}' > /home/nasuser/proton-sync/settings.json`). Sans `i18n.py`/`locale/`, rien ne casse : les messages restent en anglais (import guardé, même motif que `mount_check`).
- **Programmes externes** : zenity (calendrier) est lancé avec un environnement de locale ajusté (`i18n.subprocess_env()`) pour suivre la langue choisie et non celle du système. Limite : afficher une langue exige que sa locale soit **générée** sur le système (`locale -a`).
- **Descriptions des unités systemd** : générées **dans la langue courante au moment de « Installer / Mettre à jour »**, puis figées dans les fichiers `.service`/`.timer` (nature de systemd) — refaire un Install/Update après un changement de langue pour les réécrire.
- **`build_locales.sh`** : outil de **développement** uniquement (recompiler les `.po` après édition ; nécessite le paquet `gettext`). Jamais requis en production.

**Notes pour contributeurs** (pièges vécus) :
- **Jamais `_` comme variable jetable** (`ok, _ = f()`) dans un fichier marqué : cela masque le `_` de gettext dans toute la fonction (`UnboundLocalError`). Utiliser `_err`, `_x`, etc. Audit systématique avant livraison.
- **Détections découplées du texte affiché** : tout ce qui est machine-parsé passe par des **tags stables** hors traduction (`[subpath-excluded]`) ou des **listes de marqueurs multilingues** (`"Terminé."`/`"Done."` dans `_parse_result`) — jamais par la chaîne traduite seule.
- Pas de `_(f"...")` : l'interpolation précéderait la traduction. Utiliser `_("... {x}").format(x=...)`.

---

## Configuration (settings.json) et mode local seul

Depuis le chantier « configuration », tout ce qui varie d'une installation à l'autre est **externalisé dans `settings.json`** (le même fichier que la langue, à côté des scripts) — plus rien d'essentiel n'est codé en dur. Le module **`config.py`** est la source de vérité unique, partagée par le moteur, le GUI et les démons (import tolérant : sans lui, chaque fichier retombe sur ses défauts historiques).

**Réglages disponibles** (dialogue **« ⚙ Configuration… »** du GUI, chacun avec son bouton « ? » d'aide en langage non-programmeur ; ou édition directe du JSON — voir `settings.example.json`) :

| Clé | Défaut | Rôle |
|---|---|---|
| `language` | `"auto"` | Langue de l'interface (déjà existant, dialogue « Langue… ») |
| `nas_enabled` | `true` | **Interrupteur du mode NAS.** À `false` (mode **local seul**), l'application ne tente **jamais** de joindre un NAS : pas de poussée de mappings, pas de sonde de file NAS dans le consommateur, sections NAS **masquées** dans la fenêtre Temps réel — coupure nette, pas de « tentative puis échec » |
| `nas_mount_path` | `"/media/home_nas"` | Point de montage NFS où vivent `proton-sync/config` et `proton-sync/queue` du NAS |
| `proton_cli_path` | `null` | Chemin du binaire CLI Proton. `null` = résolution par défaut. Ordre de priorité partagé partout : **variable d'environnement `PROTON_DRIVE_CLI` > ce réglage > `<dossier des scripts>/proton-drive`**. S'il est réglé, les unités systemd générées l'utilisent aussi |
| `rename_ext_enabled` | `true` | Correction automatique des extensions majuscules (voir la section « Robustesse d'upload… ») — activable/désactivable durablement ; `--no-rename-ext` reste la surcharge ponctuelle |
| `rename_ext_auto_disabled` | `false` | Interne : mémorise que la désactivation unique de la normalisation d'extensions (CLI ≥ 0.5.0) a eu lieu, pour ne jamais revenir ensuite sur votre choix |
| `rename_ext_collision_suffix` | `"_ProtonEditExt"` | Suffixe inséré en cas de collision de renommage (jamais d'écrasement). Validé à la saisie : non vide, sans `/ \ " '` |
| `rename_ext_whitelist` | images, vidéo, audio, documents (37 entrées) | Extensions que la normalisation a le droit de toucher. Liste vide = aucune restriction (comportement historique) |
| `cli_stall_minutes` | `5` | Minutes d'inactivité totale au bout desquelles un envoi bloqué est interrompu. `0` désactive le disjoncteur |
| `cli_stall_max_kills` | `0` | Blocages consécutifs tolérés sur une destination avant de sauter un passage. `0` = illimité |
| `tray_enabled` | `false` | Icône d'état dans la barre des tâches (voir ci-dessous) |

Les changements s'appliquent au **prochain lancement** du GUI et au **prochain redémarrage** des démons (comme la langue). Le fichier est écrit de façon atomique et **préserve les clés inconnues**.

**Dossier de données unifié.** Le cache, la file temps réel et les journaux vivent désormais sous un seul parent : `~/.proton-drive-sync/` (`cache/`, `queue/`, `realtime.conf`, `failures.log`, `renamed-extensions.log`, `proton_sync.lock`). À la première exécution après mise à jour, une **migration automatique** renomme les anciens emplacements (`~/.proton_sync_cache`, `~/.proton_sync`, `~/.proton_sync.lock`) vers la nouvelle arborescence — un **renommage** au sein du même système de fichiers : instantané, contenu intact, **aucun re-balayage** du cache. Idempotente (plusieurs processus peuvent démarrer en même temps) et résiliente (en cas d'échec, les anciens chemins restent utilisés pour l'exécution en cours).

**Hygiène des chemins.** Les emplacements des scripts eux-mêmes (moteur, watchers, logs de passage) ne sont **pas** des réglages : chaque fichier les dérive de son propre emplacement (`__file__`, comme `i18n.py` l'a toujours fait). Installer le dossier complet ailleurs (`/opt/…`, un autre home) fonctionne sans rien configurer.

**Icône d'état dans la barre des tâches** (`tray_indicator.py`). Une double flèche circulaire près de l'horloge : **violette** = démons actifs + session Proton valide ; **violette avec un badge « ! » ambré** = démons actifs mais des scripts NAS attendent d'être poussés (voir « Alerte d'écart de scripts NAS » plus bas) ; **grise avec X rouge** = démons actifs mais session expirée/trousseau verrouillé ; **grise** = démons arrêtés. (Priorité : arrêtés > session expirée > scripts en attente > actif.) Clic gauche = ouvrir l'éditeur ; clic droit = menu. Activable par la case « Barre des tâches » de ⚙ Configuration… (démarrage immédiat + autostart de session via `~/.config/autostart/` ; à la désactivation, l'applet s'éteint d'elle-même). Techniquement : XApp.StatusIcon (libxapp — natif Cinnamon/MATE/Xfce ; paquets `python3-gi` + `gir1.2-xapp-1.0`, préinstallés sur Mint). L'applet ne contacte **jamais** Proton : elle lit uniquement le battement de cœur `status.json` que le consommateur réécrit à chaque cycle dans `~/.proton-drive-sync/` — zéro sonde supplémentaire, zéro contention de trousseau. Nuance assumée : la sonde d'auth du consommateur n'a lieu que lorsqu'il y a du travail ; au repos, l'icône reflète le dernier état connu.

**Identité NAS stable (`account_name`).** La copie de configuration et la file de marqueurs sur le NAS portent une identité **découplée à la fois du nom du fichier de mappings et du compte Proton** : le réglage persistant `account_name`. Défaut, semé automatiquement au bon moment : identité des unités existantes lors d'une mise à jour (préservation des installations historiques) ; sinon, sur une installation neuve, un **nom neutre unique `user{n}`** réclamé sur le NAS **au premier usage réel** (première poussée ou installation des services) par `mkdir queue/user{n}` — opération **atomique y compris sur NFS** : deux postes qui s'amorcent au même instant ne peuvent pas gagner le même numéro. Personnalisable dans ⚙ Configuration ; si l'ancien nom existe sur le NAS, l'application propose alors la **migration** (simple renommage de la file et de la copie — marqueurs préservés), puis rappelle de redémarrer les services. Quel que soit le nom du fichier local, la poussée écrit toujours `config/mappings-<identité>.json` et le consommateur lit toujours `queue/<identité>` : **une seule queue temps réel par personne**, stable à vie — renommer le fichier local est un non-événement côté NAS (plus de file fantôme). Les démons ne résolvent jamais l'adresse eux-mêmes (session parfois verrouillée à leur démarrage) : ils lisent le réglage, semé par le GUI.

**Changement de compte Proton (garde-fou).** Chaque cache porte une **estampille de compte** (clé réservée `__meta__` : l'adresse du compte qui l'a bâti). En début de passage, le moteur compare l'estampille au compte réellement connecté : s'ils diffèrent, il **refuse le passage** (code 4, message tagué `[account-changed]`, rien n'est touché) — faire confiance au cache produirait une sauvegarde silencieusement incomplète, et l'ignorer re-téléverserait tout sans prévenir. La voie de sortie : **« Amorcer le cache »** ou **« Réinitialiser »** depuis le GUI (ces actions passent `--accept-account-change` au moteur) — l'ancien cache est écarté, ré-estampillé, et les destinations sont (re)créées sur le nouveau Drive. Le consommateur temps réel reconnaît le refus : une seule ligne d'attente « ⛔ » au journal, marqueurs conservés, icône de barre des tâches grise + X ; reprise automatique dès la résolution. **Anciens caches sans estampille : valides tels quels**, estampillés au fil des sauvegardes — aucun ré-amorçage requis sur une installation existante.

**Changement de fichier actif (garde-fou).** À « Installer/Mettre à jour », si le fichier diffère de celui des unités, **tout changement demande confirmation** (des billets peuvent arriver à tout instant — pas d'exemption « zéro billet ») : le dialogue chiffre les billets en attente, **dont ceux visant des dossiers NON couverts par le nouveau fichier** — ceux-là seront écartés (lignes « ⊘ » au journal) et ne sont rattrapables qu'en rechargeant l'ancien fichier pour un passage manuel ou planifié. Deux issues : *Confirmer le changement* ou *Annuler (garder le fichier en cours)*. La file locale s'auto-nettoie (marqueur sans mapping → ignoré, ligne « ⊘ » visible) ; au démarrage, le watcher NAS signale (sans y toucher) toute file orpheline contenant des marqueurs — résidu historique éventuel.

**Synchro automatique des scripts vers le NAS.** À chaque « Installer/Mettre à jour » du temps réel, le poste pousse vers `proton-sync/` sur le NAS les fichiers dont le watcher NAS dépend (`nas_watcher.py`, `local_watcher.py`, `config.py`, `i18n.py`, `mount_check.py` + `locale/`), en copie différentielle (seuls les fichiers modifiés). Cela évite le piège du déploiement partiel — un `config.py` périmé côté NAS met le service en échec silencieux. Le fichier `.service`, lui, n'est pas installé à distance (cela demande sudo) : il est seulement **déposé** en `nas_watcher.service.new`, et si son contenu a changé, le message d'installation rappelle la commande à lancer sur le NAS (`sudo systemctl daemon-reload && sudo systemctl restart proton-nas-watch.service`).

**Alerte d'écart de scripts NAS.** Comme « Installer/Mettre à jour » n'est plus utilisé au quotidien (les démons se rechargent seuls), rien ne signalait qu'un déploiement de scripts vers le NAS restait à faire — le NAS pouvait garder silencieusement du code périmé. Le consommateur compare donc périodiquement (au démarrage puis toutes les ~5 min, uniquement si le NAS est joignable) les scripts et catalogues du poste à leurs copies sur le NAS — par sha256, sur exactement les mêmes fichiers que ceux que le push copierait — et inscrit le verdict dans `status.json`. En cas d'écart : l'**icône de la barre des tâches** passe au motif violet avec un **badge « ! » ambré**, et la **fenêtre temps réel** affiche un avertissement en section 4 (« ⚠ Scripts NAS obsolètes — lancez Installer/Mettre à jour pour les pousser, puis redémarrez le watcher NAS »). Les deux affichages lisent le **même champ** (une seule source de vérité). Un seul clic sur **Installer/Mettre à jour** pousse les fichiers ; le contrôle suivant, les voyant concordants, **efface l'alerte de lui-même** (le push a lieu **avant** le redémarrage des démons, pour que le contrôle au démarrage du consommateur voie déjà le NAS à jour). L'alerte ne s'allume jamais en mode local seul ni quand le NAS est absent — rien à pousser par conception.

**Côté NAS** : `nas_watcher.py` n'a plus de liste de préfixes codée en dur (`/media/nas1`…) — il filtre sur le champ **`source_kind == "nfs"`** déjà présent dans chaque mapping poussé (même information, zéro duplication). Son dossier de base reste paramétrable par `--config-dir`/`--queue-dir`.

---

## Fichiers de référence du projet

Les principaux fichiers du projet :

- `Guide-ProtonDrive-iSCSI-WinBoat.pdf` — l'ancienne solution (WinBoat/iSCSI), toujours fonctionnelle, à conserver en backup le temps que ce nouveau setup soit éprouvé
- `proton_mapping_editor.py` — GUI Tkinter (édition mappings, lancement, exclusions, réglages de suppression, fenêtres Planification et Temps réel)
- `proton_sync.py` — moteur batch (cache, checkpoints, verrou, journal temps réel, exclusions, suppressions, correctif glob, `--subpath`)
- `mount_check.py` — **module de garde-fou de montage, OBLIGATOIRE à côté de `proton_sync.py`** pour que les suppressions fonctionnent (détection nfs/local, blocage si NAS déconnecté)
- `schedule_manager.py` — backend GUI du timer nocturne (génération/installation des unités systemd `--user`)
- `local_watcher.py` — watcher inotify des sources locales (machine locale)
- `nas_watcher.py` — watcher inotify des sources NAS (**tourne sur le NAS**)
- `realtime_consumer.py` — consommateur temps réel (marqueurs, debounce, lancement ciblé du moteur)
- `realtime_manager.py` — backend GUI du temps réel (démons, `realtime.conf`, push NAS, dérive, files)
- `proton-nas-watch.service` — unité systemd du watcher NAS (à installer sur le NAS)
- `mappings-user1.json` — config User1 (vivante, modifiée régulièrement)
- `mappings-user2.json` — config User2
- `i18n.py` — socle de traduction (résolution de langue, gettext, `settings.json`) — **requis à côté des autres `.py`**, machine locale ET NAS
- `locale/` — catalogues de traduction (`fr/LC_MESSAGES/proton-sync.po` + `.mo` compilé, modèle `.pot`) — machine locale ET NAS
- `build_locales.sh` — outil de dev : recompilation des catalogues (jamais requis en production)
- `settings.json` — préférence de langue (créé par le GUI ; optionnel, à la main sur le NAS)
- `Temporary-files-exclusions.fr.md` — catalogue des motifs d'exclusion recommandés par logiciel (référence à part)
- `INSTALLATION-systemd.fr.md` — guide d'installation du timer nocturne
- `INSTALLATION-realtime.fr.md` — guide d'installation de la couche temps réel
- ce fichier `.md`

---

## Idées pour la suite

### Chantiers planifiés (prochaine étape)

- **Marqueurs temps réel : un dossier peut être synchronisé alors qu'un fichier s'écrit encore.** Une sauvegarde de téléphone qui dépose d'abord un petit fichier puis un gros arme le debounce sur le petit ; le moteur démarre pendant que le gros monte encore. Augmenter le debounce ne règle rien (la fenêtre d'écriture dépasse largement toute valeur raisonnable), et une quarantaine par âge non plus : l'application de sauvegarde conserve la date d'origine, si bien que le fichier paraît ancien alors qu'il grossit encore. Deux pistes subsistent — ajouter `IN_MODIFY` au masque du watcher, ou vérifier que les tailles ont cessé de bouger avant de lancer un passage.
- **Marqueurs temps réel : un marqueur arrivant pendant un passage peut être avalé.** Les marqueurs sont dédoublonnés par dossier tant qu'ils attendent dans la file ; celui qui arrive pendant que ce dossier est en cours de synchro est écarté, et le marqueur qui l'a fait écarter est ensuite nettoyé en cas de succès — le changement est donc oublié jusqu'au prochain passage planifié. Ne mord que si le passage réussit. Pistes : sortir les marqueurs de la file avant de lancer le moteur, ou ne nettoyer que ceux antérieurs au démarrage du passage.
- **Le passage planifié et le consommateur temps réel se disputent le verrou.** Quand le consommateur le détient, le service planifié sort en échec et n'est pas rejoué : un passage nocturne entier peut être sauté. Pistes : `Restart=on-failure` sur l'unité planifiée, ou acquisition bloquante avec délai en mode planifié.
- **Journal temps réel plus silencieux.** Session graphique fermée, le consommateur relance le moteur à chaque cycle et échoue à chaque fois sur un trousseau verrouillé. Il devrait tester le trousseau une fois par cycle et journaliser une seule ligne d'attente. Connexe : la colonne de formulaire de la fenêtre temps réel gaspille de la largeur tandis que le volet du journal tronque les chemins longs.
- **Régénérer les PDF** (`build_pdf.py`) maintenant que la documentation a changé ; revoir la table de substitution des symboles.

### Pistes plus lointaines


- **Watchers conscients des exclusions** (différé, choix de User1) : ignorer un événement matchant une exclusion AVANT d'écrire le marqueur, pour ne plus générer de marqueurs ni de synchros à vide (ex. `__pycache__`).
- **Vue « ne montrer que ce qui est synchronisé »** dans le journal temps réel (différé) : niveau facile = filtre GUI suivant seulement `proton-consume.service` (masquer les ADD/DEL du watcher) ; niveau profond = ne plus afficher les synchros à vide (le moteur doit distinguer « envoyé » de « rien à faire »).
- Logs structurés (JSON) en plus de la sortie console, pour faciliter l'analyse post-mortem
- Notification (mail ou pop-up bureau) en cas d'erreur lors d'une tâche planifiée
- Statistiques de chaque passage (durée, nombre de fichiers transférés, volume total) dans un fichier d'historique
- Retry automatique des erreurs 500 dans le même passage (au lieu d'attendre le passage suivant) — petit `time.sleep` + 2-3 tentatives
- Migrer le pont WinBoat/iSCSI vers archive une fois que le CLI tourne en production depuis 1-2 mois sans incident
