🇬🇧 [English](INSTALLATION-systemd.md) | 🇫🇷 Français

# Automatisation de la synchro Proton Drive avec systemd (timer --user)

> **Sauvegarde à sens unique** : ce logiciel envoie vos dossiers locaux **vers**
> Proton Drive et ne télécharge jamais depuis Proton.

Planifie la synchro une fois par jour à 3h00, dans le contexte de la session
utilisateur (donc avec accès au trousseau GNOME déverrouillé).

**Approche retenue** : timer systemd `--user` + session graphique gardée ouverte
en permanence sur la machine locale. Si la session n'est pas ouverte au moment du
déclenchement, le moteur le détecte, sort proprement (code 2) et réessaie le
lendemain — aucun blocage, aucune corruption.

> **Le plus simple : la fenêtre « ⏰ Planification… » du GUI.** Elle installe et
> règle le timer en un clic — elle **génère** `proton-sync.service` + `.timer`,
> recharge systemd et active le timer — et permet de choisir la **fréquence**
> (quotidien, **hebdomadaire** avec jour + heure, ou horaire) ainsi que
> l'Option A/B. La procédure manuelle ci-dessous reste valable comme **repli**,
> ou pour comprendre ce que le GUI fait sous le capot.
>
> Pour la **couche temps réel** (synchro instantanée par watchers inotify), voir
> `INSTALLATION-realtime.fr.md`. Avec elle, ce timer nocturne sert surtout de
> **filet** et se règle volontiers en hebdomadaire.

---

## Avant de commencer : où placer le binaire du CLI Proton

L'application n'exécute pas la synchro elle-même : elle pilote le binaire officiel
`proton-drive`, que vous téléchargez séparément. Elle résout le binaire dans
cet ordre :

1. la variable d'environnement `PROTON_DRIVE_CLI`, si elle est définie ;
2. le champ **« Chemin du binaire proton-drive »** de la fenêtre **Configuration**
   du GUI ;
3. `<dossier des scripts>/proton-drive`, lorsque ce fichier est exécutable ;
4. `proton-drive` sur le `PATH` ;
5. sinon le chemin de l'étape 3, pour que l'erreur puisse le nommer.

**Le plus simple** : installer `proton-drive` sur le `PATH`, ou déposer le
binaire **à côté des scripts** (étape 3). Pour le garder ailleurs, renseignez
le champ de la fenêtre Configuration.

> **Si vous changez ce chemin APRÈS avoir installé les services**, réinstallez-les
> (bouton « Installer / Mettre à jour », ou en régénérant les unités à la main) :
> les unités systemd embarquent le chemin **au moment de leur création** et
> continueraient sinon à pointer sur l'ancien emplacement.

---

## Où vivent les réglages

Les réglages sont dans `~/.config/proton-drive-sync/settings.json`
(`$XDG_CONFIG_HOME/proton-drive-sync/settings.json` si cette variable est définie).
Le fichier `settings.json` à côté des scripts n'est plus lu. S'il est le seul,
le premier lancement le copie une fois (mode 0600) et le laisse en place.
Une sauvegarde du dossier des scripts n'inclut plus la configuration :
ajoutez aussi `~/.config/proton-drive-sync`, ou `$XDG_CONFIG_HOME/proton-drive-sync`
si cette variable est définie. L'éditeur le dit une fois, à la première
ouverture après la copie. `PROTON_SYNC_SETTINGS`, si elle est définie, désigne
ce fichier à la place et ne lance pas la copie.

---

## Installation manuelle (repli) — pour CHAQUE utilisateur (User1, puis User2)

Les commandes ci-dessous sont à exécuter **dans la session de l'utilisateur
concerné** (connecté en graphique, trousseau déverrouillé). Ne pas utiliser
`sudo` — tout se passe au niveau utilisateur.

### 1. Copier les fichiers service et timer

```bash
mkdir -p ~/.config/systemd/user
cp proton-sync.service ~/.config/systemd/user/
cp proton-sync.timer   ~/.config/systemd/user/
```

**IMPORTANT pour User2** : avant de copier, éditer `proton-sync.service` et
remplacer `mappings-user1.json` par `mappings-user2.json` (une seule ligne,
celle du `ExecStart`).

### 2. Recharger systemd et activer le timer

```bash
systemctl --user daemon-reload
systemctl --user enable --now proton-sync.timer
```

### 3. Vérifier que le timer est bien armé

```bash
systemctl --user list-timers proton-sync.timer
```

Tu devrais voir la prochaine échéance (NEXT) à 3h00 le lendemain.

### 4. Tester le service immédiatement (sans attendre 3h00)

```bash
systemctl --user start proton-sync.service
```

Puis regarder le résultat :

```bash
# État du service (active/inactive, code de sortie)
systemctl --user status proton-sync.service

# Journal complet du dernier passage
journalctl --user -u proton-sync.service -n 50 --no-pager
```

---

## Permettre l'exécution même sans session graphique active (linger)

Par défaut, les services `--user` s'arrêtent quand l'utilisateur se déconnecte.
Comme on garde la session ouverte en permanence sur la machine locale, ce n'est pas
strictement nécessaire — mais activer le "linger" rend le système plus robuste
(le timer survit à une fermeture de session accidentelle, et se relance au
démarrage de la machine).

**Cette commande nécessite les droits admin (une seule fois, par utilisateur)** :

```bash
sudo loginctl enable-linger myuser
sudo loginctl enable-linger user2
```

ATTENTION : le linger fait tourner les services même sans session graphique
ouverte. MAIS le trousseau, lui, reste verrouillé tant que la session n'est pas
ouverte. Donc avec linger seul (sans session ouverte), le moteur tournera mais
sortira proprement en code 2 (auth impossible). C'est voulu : pas de blocage,
juste un passage sauté jusqu'à ce que la session soit rouverte.

Pour vérifier l'état du linger :

```bash
loginctl show-user myuser | grep Linger
```

---

## Comportement en cas de session non ouverte

Si le timer se déclenche alors que la session de l'utilisateur n'est pas
ouverte (trousseau verrouillé) :

1. Le moteur fait son test d'authentification (`filesystem list /`) au démarrage
2. Ça échoue (trousseau verrouillé)
3. Le moteur affiche un message clair et sort avec le **code 2**
4. systemd considère ça comme un succès (grâce à `SuccessExitStatus=0 2 4`), donc
   PAS de notification d'échec, PAS de service en "failed"
5. Le cache n'est pas touché, le verrou est libéré
6. Le lendemain à 3h00 (ou dès réouverture de session), nouvelle tentative

Pour voir si un passage a été sauté pour cette raison :

```bash
journalctl --user -u proton-sync.service | grep "Authentification"
```

---

## Collision avec le temps réel (relance automatique)

Le passage planifié et le **consommateur temps réel** partagent le verrou
`~/.proton-drive-sync/proton_sync.lock` : jamais deux passages en parallèle. Si le consommateur
tient le verrou au moment où le timer se déclenche (par ex. un backup de
téléphone en pleine nuit a réveillé le temps réel), le moteur planifié sort en
**échec (code 1)**.

Pour que ça ne saute pas tout le passage nocturne, le service est en
`Type=exec` avec `Restart=on-failure` + `RestartSec=120` : systemd le
**relance automatiquement ~2 min plus tard**, le temps que le consommateur ait
fini et libéré le verrou. `StartLimitBurst=6` (sur `StartLimitIntervalSec=1h`)
borne les tentatives pour éviter toute boucle si le problème persiste. Le code 2
(trousseau verrouillé) reste un succès (`SuccessExitStatus=0 2 4`) et ne provoque
donc **pas** de relance inutile.

> Ces réglages sont générés automatiquement par la fenêtre « ⏰ Planification… »
> du GUI (bouton « Installer / Mettre à jour »). Après mise à jour du projet,
> rejoue « Installer / Mettre à jour » **pour User1 ET pour User2** pour que la
> nouvelle unité remplace l'ancienne. Vérification :
>
> ```bash
> systemctl --user cat proton-sync.service | grep -E "Type=|Restart=|RestartSec="
> ```
>
> Tu dois voir `Type=exec`, `Restart=on-failure`, `RestartSec=120`.

Pour observer une relance après une vraie collision :

```bash
journalctl --user -u proton-sync.service --since today --no-pager
```

Tu verras un échec (« Une autre instance… est déjà en cours », `status=1`) suivi,
~2 min plus tard, d'un nouveau « Starting… » puis « Finished… » qui réussit.

---

## Codes de sortie

Le service planifié lance un passage complet. Voici les codes du moteur qui comptent pour cette unité :

| Code | Signification | systemd |
| --- | --- | --- |
| 0 | Passage terminé, rien n'a échoué | succès |
| 1 | Verrou tenu par un autre passage, ou le CLI ne peut pas démarrer | `Restart=on-failure` (borné) |
| 2 | Authentification échouée (trousseau verrouillé ou panne Proton/réseau) | `SuccessExitStatus`, pas de relance |
| 4 | Compte Proton changé ; rien n'a été synchronisé | `SuccessExitStatus`, pas de relance |
| 5 | Passage terminé, mais au moins un envoi, un listage, un dossier illisible, un refus de permission, un dossier sauté après blocage, une corbeille ou une source manquante a échoué | unité **failed**, `RestartPreventExitStatus=5` pour que systemd ne la relance pas. Le timer suivant, ou le cycle temps réel, réessaie |

Le code 3 ne concerne que `--subpath` quand le dossier n'a pas encore été indexé. L'unité planifiée n'utilise pas `--subpath`.

La dernière ligne d'un passage est un objet JSON `[run-result]` (code de sortie, mode, compteurs). `~/.proton-drive-sync/last-run.json` garde le dernier passage complet et le dernier passage `--subpath` séparément.

Les unités installées avant ce changement ne contiennent pas `RestartPreventExitStatus=5`. Ouvrir l'éditeur de mappings les réécrit à partir du fichier de mappings, de l'heure et de `--delete` déjà installés. Le chemin du moteur sur `ExecStart` et `PROTON_DRIVE_CLI` restent tels quels ; seul Installer / Mettre à jour change ces deux chemins. Si tu n'ouvres jamais l'éditeur, réécris-les une fois toi-même :

```bash
python3 schedule_manager.py --refresh-units
systemctl --user cat proton-sync.service | grep RestartPreventExitStatus
```

Tu dois voir `RestartPreventExitStatus=5` et `SuccessExitStatus=0 2 4`.

---

## Journal des passages (GUI)

Le bouton **« 📜 Journal des passages… »** de la fenêtre Planification donne accès
au journal du service planifié sans commande manuelle :

- **Dernière exécution** par défaut — isolée par sa **frontière de démarrage**
  dans le journal (fiable même après un reboot, contrairement à l'état runtime
  de systemd, vidé à chaque redémarrage) ;
- **résumé** en tête : date + résultat (« ✅ succès », « ❌ échec (code 1) » =
  collision de verrou, « ⏹ interrompu ») — marqueurs de détection FR **et** EN,
  l'historique reste lisible quelle que soit la langue ;
- **sélecteur de date** (calendrier) pour un jour précis.

Aucun fichier n'est créé : lecture du **journal systemd**, auto-limité
(`SystemMaxUse`, purge automatique) — plusieurs mois d'historique en pratique.
Équivalents ligne de commande :

```
journalctl --user -u proton-sync.service -n 200 --no-pager
journalctl --user -u proton-sync.service --since "2026-07-01" --until "2026-07-01 23:59:59"
```

---

## Changer l'heure ou la fréquence

**Le plus simple est de passer par la fenêtre « ⏰ Planification… » du GUI**
(menu Fréquence : Quotidien / Hebdomadaire / Toutes les heures, + jour et heure),
puis « Installer / Mettre à jour ». Elle réécrit le `OnCalendar` et recharge le
timer pour toi. Méthode manuelle équivalente : éditer
`~/.config/systemd/user/proton-sync.timer`, modifier la ligne `OnCalendar=`, puis :

```bash
systemctl --user daemon-reload
systemctl --user restart proton-sync.timer
```

Exemples de valeurs `OnCalendar` :
- `*-*-* 03:00:00`        -> tous les jours à 3h00
- `*-*-* 03,15:00:00`     -> tous les jours à 3h00 ET 15h00
- `Mon *-*-* 03:00:00`    -> tous les lundis à 3h00
- `*-*-* *:00:00`         -> toutes les heures pile

---

## Propagation des suppressions dans la planification : Option A vs Option B

**À comprendre absolument** : configurer `allow_delete: true` dans le JSON ne
suffit PAS pour que la planification supprime. Le moteur ne propage les
suppressions QUE si le flag `--delete` est présent sur la ligne `ExecStart` du
service. C'est donc le service systemd qui décide si la planification nocturne
est additive ou miroir — peu importe le contenu du JSON.

### Option A — Planification ADDITIVE (configuration actuelle, recommandée)

`ExecStart` SANS `--delete` (l'installation de base) :
```
ExecStart=/usr/bin/python3 %h/Logiciels/Proton-drive/proton_sync.py %h/Logiciels/Proton-drive/mappings-user1.json
```
Les passages de 3h envoient mais ne suppriment jamais. Les suppressions ne se
font que si on lance manuellement avec `--delete` (GUI : case « Propager
suppressions », ou ligne de commande). Mode prudent, contrôle total.

### Option B — Planification MIROIR (suppressions automatiques)

`ExecStart` AVEC `--delete` :
```
ExecStart=/usr/bin/python3 %h/Logiciels/Proton-drive/proton_sync.py %h/Logiciels/Proton-drive/mappings-user1.json --delete
```
Le passage de 3h devient un vrai miroir : ce qui est supprimé localement
disparaît de Proton (selon le `delete_mode` de chaque mapping, sous réserve du
garde-fou de montage). Filets : fenêtre de plusieurs heures avant 3h + corbeille
Proton (mappings en mode `trash`), qui conserve les éléments tant qu'on ne la
vide pas soi-même.

**Pour basculer A -> B** : éditer `~/.config/systemd/user/proton-sync.service`,
ajouter `--delete` en fin de ligne `ExecStart`, puis :
```bash
systemctl --user daemon-reload
systemctl --user restart proton-sync.timer
```

**Prérequis avant Option B** :
- `mount_check.py` DOIT être à côté de `proton_sync.py` (sinon suppressions refusées)
- Avoir fait plusieurs passages `--delete` manuels sans surprise au préalable
- Décider séparément pour User1 et pour User2 (chacun son service)

**Choix actuel : Option A** (additif ; suppressions manuelles uniquement).

---

## Vérification mensuelle approfondie (--verify-hash) — optionnel

Pour planifier en plus une vérification SHA1 mensuelle (équivalent du /IS de
robocopy), créer un 2e couple service+timer, ex. `proton-verify.service` :

ExecStart avec `--verify-hash` ajouté :
```
ExecStart=/usr/bin/python3 %h/Logiciels/Proton-drive/proton_sync.py %h/Logiciels/Proton-drive/mappings-user1.json --verify-hash
```

Et un timer `proton-verify.timer` :
```
OnCalendar=*-*-01 02:00:00     # le 1er de chaque mois à 2h00
Persistent=true
```

---

## Langue des unités (i18n)

Les fichiers `.service`/`.timer` sont générés avec leurs `Description=` dans la
**langue courante au moment de « Installer / Mettre à jour »**, puis figés
(nature de systemd). Après un changement de langue dans le GUI, refaire un
« Installer / Mettre à jour » (Planification **et** Temps réel) pour réécrire
les descriptions ; les lignes « Started … » du journal utiliseront alors la
nouvelle langue (l'historique, lui, ne change pas).

---

## Désactiver l'automatisation

```bash
systemctl --user disable --now proton-sync.timer
```
