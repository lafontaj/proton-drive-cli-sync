🇬🇧 [English](INSTALLATION-realtime.md) | 🇫🇷 Français

# Installation — Temps réel (couche 5)

Complément à `INSTALLATION-systemd.fr.md`. Le temps réel ajoute **trois démons** au
timer nocturne déjà en place : deux sur la **machine locale** (pilotés depuis le GUI) et
un sur le **NAS** (installé manuellement, géré sur le NAS).

Principe retenu : **systemd partout, chaque machine garde ses propres démons
actifs.** Le GUI ne pilote que les démons locaux (machine locale) ; il **observe** le
watcher NAS via la file NFS, sans SSH.

---

## 1. la machine locale — watcher local + consommateur (via le GUI)

Rien à copier à la main. Dans l'éditeur de mappings :

1. Ouvre le fichier de mappings de l'utilisateur (`mappings-user1.json`…).
2. Bouton **« ⚡ Temps réel… »**.
3. **« 💾 Installer / Mettre à jour »** : crée et démarre les deux services
   `--user`, pointés sur le fichier de mappings actif :
   - `proton-watch.service`   → `local_watcher.py`
   - `proton-consume.service` → `realtime_consumer.py`

Ils redémarrent automatiquement à l'ouverture de session (`WantedBy=default.target`,
`Restart=on-failure`).

Équivalent manuel (pour référence), une fois les `.service` écrits dans
`~/.config/systemd/user/` :

```bash
systemctl --user daemon-reload
systemctl --user enable --now proton-watch.service proton-consume.service
systemctl --user status proton-consume.service
journalctl --user -u proton-consume.service -n 50 --no-pager
```

### Persistance hors session (linger)

Comme pour le timer nocturne, sans **linger** les démons `--user` s'arrêtent à la
déconnexion. La fenêtre temps réel affiche l'état du linger et rappelle la
commande (admin, une seule fois) :

```bash
sudo loginctl enable-linger <utilisateur>
```

---

## 2. NAS — watcher NAS (manuel, sur le NAS)

Le watcher NAS tourne **sur le NAS**, sous le compte `nas`, en service **système**
(il doit démarrer au boot sans session ouverte). Le GUI ne le pilote pas.

> **Pourquoi un service système (pas `--user`) et sans linger ?** La machine locale
> utilise des services `--user` avec *linger* car ils vivent dans votre session
> graphique. Le NAS, c'est différent : **on n'y ouvre jamais de session**. Un service
> `--user` n'aurait aucune session à laquelle se rattacher, et le linger n'a aucun
> sens. Un service **système** (`/etc/systemd/system/`, activé avec un simple
> `sudo systemctl enable`) démarre au boot, tourne sous son propre compte, et ne
> nécessite ni session ni linger — exactement ce qu'il faut pour un NAS sans écran.
> N'utilisez **pas** `systemctl --user` ni `enable-linger` sur le NAS.

Sur le NAS :

```bash
# Fichiers requis dans /home/nasuser/proton-sync/ (à copier ensemble) :
#   nas_watcher.py, local_watcher.py (helpers partagés), mount_check.py,
#   i18n.py + le dossier locale/ (traductions ; sans eux, logs en anglais).
# pyinotify installé (python3-pyinotify ou pip).
# Langue des logs : suit LANG du NAS ; pour forcer :
#   echo '{"language": "fr"}' > /home/nasuser/proton-sync/settings.json
sudo cp proton-nas-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proton-nas-watch.service
systemctl status proton-nas-watch.service
journalctl -u proton-nas-watch.service -n 50 --no-pager
```

Le watcher lit les copies de mappings dans `/home/nasuser/proton-sync/config/`
(`mappings-user1.json`, `mappings-user2.json`…) — c'est ce que **pousse le GUI**
via *Temps réel → ⬆ Pousser les mappings vers le NAS*. Il recharge ces copies à
chaud, donc un nouveau push est pris en compte sans redémarrer le service.

---

## 3. Vérifier la chaîne complète

1. **GUI → Temps réel → ⬆ Pousser les mappings vers le NAS** : l'indicateur passe
   au 🟢 vert (« À jour sur le NAS »).
2. **Watcher NAS (observation)** : 🟢 « NAS joignable ».
3. Modifie un fichier dans une source surveillée → un marqueur apparaît dans la
   file, le consommateur le traite après le délai de debounce.
4. **Files de marqueurs** : le compteur revient à 0 une fois traité.

---

## Rappel — réglages temps réel

- **Délais** : `~/.proton_sync/realtime.conf` (JSON `debounce_seconds`,
  `cycle_seconds`), écrit par le GUI, relu **à chaud** par le consommateur à
  chaque cycle. Pas de redémarrage nécessaire.
- **Files** : marqueurs dans `~/.proton_sync/queue/` (locale) et
  `/media/home_nas/proton-sync/queue/<user>/` (NAS via NFS). Le GUI les compte et
  peut les vider.

---

## Détection immédiate, envoi temporisé

Deux temps distincts, à ne pas confondre :

- **La détection est instantanée.** Les watchers (inotify) sont événementiels :
  dès qu'un fichier bouge, le noyau les notifie et le marqueur est déposé
  **immédiatement**. Il n'y a aucun délai de grâce à ce niveau.
- **La temporisation est en aval, à la consommation.** Le `debounce` et le
  `cycle` (par défaut 60 s / 60 s) agissent sur le **consommateur** : il regroupe
  les marqueurs mûrs et **attend** avant de lancer le moteur vers Proton.

Le debounce est **ancré à la première observation** d'un dossier, mais chaque
nouvelle écriture dans ce dossier **réarme** l'échéance. Autrement dit, tant
qu'un dossier est activement modifié, il n'est pas considéré comme « stable » et
la synchro ne part pas : elle attend ~`debounce_seconds` **après la dernière
modification**. On peut donc éditer un dossier (montage PDF, retouches en série…)
aussi longtemps qu'on veut sans déclencher d'envoi ; la synchro ne part qu'une
fois l'activité calmée.

Conséquence pratique : voir plusieurs dizaines de marqueurs s'accumuler pendant
qu'on travaille est **normal** — c'est la file qui se remplit en attendant la
stabilisation. Ils fusionnent ensuite en une seule synchro du dossier (voir
déduplication ci-dessous). Pour une marge plus large, augmenter `debounce`
(section 2 du GUI) ; 60 s couvre déjà une session d'édition normale puisque le
compteur se réarme à chaque écriture.

---

## Qui voit quoi : les deux watchers et le NFS

La surveillance est **distribuée** entre deux watchers, et leur répartition tient
à une subtilité du NFS au niveau du noyau.

- Le **watcher de la machine locale** (`local_watcher.py`) surveille les sources locales
  (ext4) **et** les sources NAS montées en NFS (`/media/nas1…`). Tous ses
  marqueurs vont dans la file **locale** (`~/.proton_sync/queue/`).
- Le **watcher du NAS** (`nas_watcher.py`) surveille le **disque local du NAS** et
  écrit dans la file **NAS** (`/home/nasuser/proton-sync/queue/<user>/`, vue côté
  la machine locale comme `/media/home_nas/proton-sync/queue/<user>/`).

**Ce que le NAS voit des écritures NFS de la machine locale.** Contrairement à une idée
répandue, l'inotify du NAS n'est pas totalement aveugle aux écritures faites par
la machine locale via NFS. Le serveur NFS (`nfsd`) exécute les requêtes des clients
comme de vraies opérations locales sur le disque du NAS :

- **Créations, suppressions, renommages** (opérations *structurelles*,
  synchrones) → **vues** par l'inotify du NAS.
- **Modifications en place** d'un fichier existant → **pas fiablement vues** (le
  client NFS met les écritures de données en cache et les transmet de façon
  asynchrone).

C'est précisément ce dernier trou — les modifications que le NAS rate — qui
justifie la surveillance NFS **côté machine locale** : elle rattrape ce que le NAS ne
voit pas. Règle à retenir : **le NAS voit les créations / suppressions /
renommages NFS de la machine locale, mais pas les modifications en place ; la surveillance
la machine locale reste indispensable pour ces dernières.**

**Double observation, sans double envoi.** Pour une opération structurelle (ex.
l'écriture atomique de PDF Arranger : temporaire créé puis renommé sur la cible),
les **deux** watchers déposent un marqueur — la machine locale via NFS (file locale) et
le NAS en local (file NAS). Ce n'est pas un défaut : le consommateur **fusionne
par dossier et applique le debounce**, si bien que ces marqueurs se résolvent en
**une seule** synchro du dossier concerné. Aucun fichier n'est transféré deux
fois.

---

## Exclusions en temps réel

Les watchers déposent un marqueur pour **tout** changement (ils n'appliquent pas
les exclusions) ; c'est le **moteur** qui filtre, avec un garde-fou dédié au mode
temps réel (`--subpath`) :

- le moteur teste **chaque segment** du chemin ciblé sous la racine du mapping —
  la cible elle-même (`__pycache__`, `logs`) **et ses ancêtres** (une cible
  `.Trash-1000/info` est sautée parce que `.Trash-1000` matche `.Trash-*`) ;
- un chemin exclu est sauté **proprement** : ni upload, ni création du dossier
  distant, ni suppression ;
- la ligne émise porte le **tag stable `[subpath-excluded]`** (hors traduction),
  que le consommateur détecte pour afficher « 🚫 exclu (nom filtré) — rien à
  synchroniser » au lieu d'un « ✓ ok » ambigu.

Le journal garde donc une trace de chaque tentative filtrée (la paire
`→ synchro` + `🚫 exclu`) — voulu pour le suivi. Supprimer ce bruit à la source
(watchers conscients des exclusions) reste une amélioration différée.

---

## Langue des démons (i18n)

Les démons lisent la préférence de langue au **démarrage** (cascade :
`settings.json` → langue du système → anglais). Après un changement de langue
dans le GUI (« 🌍 Language… »), redémarrer les démons pour l'appliquer :
`systemctl --user restart proton-watch.service proton-consume.service`.
Le journal conserve l'historique dans la langue d'origine (mélange normal
après une bascule) ; les **descriptions d'unités** affichées par systemd
(« Started … ») sont réécrites dans la langue courante au prochain
« Installer / Mettre à jour ».

---

## Persistance après un redémarrage

Pour que le temps réel reparte après un reboot, **trois conditions** doivent être
réunies, dans cet ordre. Il suffit qu'un seul maillon manque pour que tout
attende (les démons tournent, mais à vide).

1. **Réseau disponible au boot.** Profil de connexion *système* — c'est le cas
   par défaut de l'Ethernet filaire. Un profil « pour cet utilisateur seulement »
   (fréquent en Wi-Fi, clé stockée dans le trousseau) ne monte qu'à l'ouverture
   de session. Vérifier : `nmcli -f connection.permissions connection show "<nom>"`
   → vide (`--`) = système.

2. **NAS monté au boot.** *C'est le piège principal, et la cause d'un long
   diagnostic en production.* Si les montages NAS se font à l'ouverture de session
   (montages GVfs/Nemo), les démons démarrent sur des points de montage **vides**
   et ne voient ni les sources ni la file de marqueurs — donc rien ne se
   synchronise tant que personne n'a ouvert de session graphique.

   **Correctif : monter les NFS dans `/etc/fstab` avec `_netdev`** (montage ferme
   au boot, après le réseau). Important : **ne pas** ajouter
   `x-systemd.automount`, qui ne monterait qu'au *premier accès* — piège pour des
   watchers inotify qui démarrent avant cet accès.

   ```
   192.168.1.10:/media/nas1  /media/nas1      nfs  _netdev,nofail,rw,hard,proto=tcp,nfsvers=3,exec,auto,acl    0 0
   192.168.1.10:/media/nas2  /media/nas2      nfs  _netdev,nofail,rw,hard,proto=tcp,nfsvers=3,exec,auto,acl    0 0
   192.168.1.10:/home/nasuser    /media/home_nas  nfs  _netdev,nofail,rw,hard,proto=tcp,nfsvers=4.2,exec,auto,acl  0 0
   ```

   Les montages **bind** qui en dépendent reçoivent
   `bind,x-systemd.requires-mounts-for=/media/nasX` (garantit l'ordre : le NFS
   monte avant le bind) et `x-gvfs-hide` (évite d'encombrer la barre latérale de
   Nemo, surtout avec deux utilisateurs).

   **Filet supplémentaire — watcher conscient des montages.** Même si une course
   au boot subsiste (le watcher démarre avant que le montage NFS soit prêt), le
   watcher local **surveille immédiatement** ce qui est monté (les sources locales)
   puis **ré-scanne** les montages — vite au démarrage — pour ajouter les sources
   NAS dès qu'elles apparaissent. Il ne reste donc plus aveugle au NAS pour toute
   la session comme avant. En console au reboot, on voit soit directement les
   cibles complètes, soit une montée `0 NAS → 🔄 Ré-scan : N cible(s)` en quelques
   secondes. Il retire aussi une cible dont le montage tombe (`➖`) et la reprend
   s'il remonte (`➕`).

3. **Trousseau déverrouillé.** Le CLI Proton exige le trousseau GNOME, qui ne
   s'ouvre qu'à l'**ouverture de session graphique** — *pas* un login console/TTY.
   Tant qu'aucune session graphique n'est ouverte, le consommateur sonde
   l'authentification (`proton_sync.py --check-auth`), constate que c'est
   verrouillé, écrit **une seule** ligne « ⏳ En attente d'ouverture de session »
   et **conserve** ses marqueurs (rien perdu) — sans lancer de passage voué au
   code 2, donc sans rafales d'échecs dans le journal. À la connexion, il
   **reprend automatiquement** (« 🔓 Session ouverte — reprise ») et vide la file.

**Conséquence pratique :** après un redémarrage, il faut ouvrir **les deux
sessions graphiques** (User1 et User2) pour que chaque file se vide. Les fichiers
ajoutés entre-temps (ex. via SFTP depuis un téléphone) s'accumulent dans la file
NAS et se synchronisent à l'ouverture de la session concernée. **C'est le filet de
sécurité voulu, pas un défaut** : le trousseau protège les identifiants Proton au
repos. (Si un trousseau a un mot de passe *vide*, son démon peut synchroniser sans
login graphique — plus commode, mais identifiants accessibles au repos. Choix à
assumer explicitement.)

### Vérifier la chaîne après un reboot (console, AVANT login : Ctrl+Alt+F2)

```
# 1. NAS joignable sans session ?
ping -c2 192.168.1.10

# 2. NAS réellement monté au boot ? (le test décisif)
systemctl --type=mount --all | grep -E 'home_nas|nas1|nas2'   # attendu : 3x active/mounted
findmnt /media/home_nas /media/nas1 /media/nas2

# 3. La file est-elle visible ?
ls /media/home_nas/proton-sync/queue/                          # attendu : user1  user2
```

Si les trois montages sont `active/mounted` et la file visible **sans session
ouverte**, le montage est bon. Il ne reste alors que le trousseau, qui se
déverrouille à l'ouverture de session graphique — et les files se vident.

> Note de diagnostic : `findmnt` peut paraître vide juste après le boot alors que
> `systemctl --type=mount` montre bien `active/mounted` — c'est `systemctl` qui
> fait foi. Et un `#` collé en début de ligne de commande la transforme en
> commentaire (le shell l'ignore sans rien exécuter).
