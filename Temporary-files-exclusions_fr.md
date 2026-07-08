🇬🇧 [English](Temporary-files-exclusions.md) | 🇫🇷 Français

# Exclusions — fichiers temporaires connus

Référence des motifs d'exclusion à utiliser dans **Exclusions globales** (et, au
besoin, par mapping) pour éviter d'envoyer vers Proton des fichiers temporaires,
verrous, corbeilles applicatives et reliques d'autres systèmes.

---

## Comment le filtrage fonctionne (rappel)

- **Noms exacts** : correspondance **littérale** du nom (pas de `*`). Insensible à
  la casse. Exclut tout dossier **ou** fichier portant exactement ce nom.
  Exemples : `__pycache__`, `.DS_Store`.
- **Motifs** : glob façon shell, le `*` remplace n'importe quelle suite de
  caractères (et `?` un seul caractère). Exemples : `*.tmp`, `.Trash-*`.
- Le filtrage porte sur le **nom** (dernier segment du chemin), pas sur le chemin
  complet.

> **Piège classique** : une entrée contenant `*` placée dans « Noms exacts » ne
> filtre **rien** (elle attend un fichier nommé littéralement `*.tmp`). Les motifs
> vont **toujours** dans la colonne « Motifs ».

**Effet au prochain passage `--delete`** : un fichier nouvellement exclu qui était
déjà monté sur Proton devient un « orphelin » (présent distant, absent de la liste
locale) → il part à la **corbeille Proton** (récupérable 30 jours). C'est le
nettoyage souhaité, mais sans surprise.

---

## Jeu recommandé — à coller tel quel

### Colonne « Noms exacts »

```
.windows-serial
__pycache__
lost+found
.directory
.gvfs
.caltrash
.dtrash
thumbnails-digikam.db
.thumbnails
LOST.DIR
.DS_Store
.Spotlight-V100
.Trashes
.fseventsd
.DocumentRevisions-V100
.TemporaryItems
Thumbs.db
ehthumbs.db
desktop.ini
$RECYCLE.BIN
System Volume Information
```

### Colonne « Motifs »

```
.Trash-*
.goutputstream-*
.pikepdf*
.nfs*
.fuse_hidden*
.trashed-*
.pending-*
.~lock.*#
~$*
._*
*~
*.tmp
*.temp
*.swp
*.swo
*.pyc
*.pyo
*.pid
*.lock
*-wal
*-shm
*.db-journal
*.part
*.crdownload
*.partial
```

> Ce jeu inclut ce que tu avais déjà (`.Trash-*`, `.goutputstream-*`, `*.tmp`,
> `*.swp`, `*~`, `*.pyc`, `__pycache__`, `.windows-serial`) **plus** les ajouts
> ci-dessous. Tu peux remplacer le contenu des deux colonnes en bloc.

---

## Détail par logiciel / catégorie

### Système & bureau Linux (généraux)
- `.Trash-*` — corbeilles freedesktop (`.Trash-1000`…).
- `.goutputstream-*` — écriture atomique GIO (xed, gThumb, Pix, exports…).
- `*~` — copies de sauvegarde par tilde (xed et beaucoup d'éditeurs).
- `*.tmp`, `*.temp` — temporaires génériques.
- `.fuse_hidden*` — fichiers cachés créés par FUSE.
- `.nfs*` — **spécifique à ton montage NFS** : fichiers « silly-rename » que le NFS
  crée quand un fichier encore ouvert est supprimé. À exclure.
- `lost+found` — dossier de récupération des systèmes de fichiers ext2/3/4 (présent
  à la racine d'un volume). Artefact système, jamais des données utiles.
- `.directory` — réglages d'affichage de dossier de KDE/Dolphin.
- `.gvfs` — ancien point de montage FUSE de GVFS (systèmes plus anciens).
- `*.pid` — fichiers de PID de processus (transitoires).
- `*.lock` — verrous génériques d'applications. (Les verrous LibreOffice
  `.~lock.*#` sont déjà couverts séparément.)

### Éditeurs de texte / code (xed, scripts)
- `*.swp`, `*.swo` — fichiers d'échange Vim.
- `*.pyc`, `*.pyo`, `__pycache__` — bytecode Python.

### Bureautique — LibreOffice (et MS Office en relique de copie)
- `.~lock.*#` — verrou d'ouverture LibreOffice (`.~lock.document.odt#`).
- `~$*` — fichiers propriétaire/verrou Microsoft Office (`~$rapport.docx`).
- `*.tmp` — temporaires de conversion/enregistrement (déjà couvert).

### PDF — PDF Arranger, xreader
- `.pikepdf*` — temporaires d'écriture atomique de PDF Arranger.
- xreader ne laisse pas de temporaire dans le dossier (annotations stockées dans
  `~/.local/share/xreader`).

### Images / photo — digiKam, GIMP, gThumb, Pix, xviewer
- `.dtrash` — **corbeille interne de digiKam** (vue dans tes photos).
- `thumbnails-digikam.db` — **cache de vignettes** (peut peser plus d'1 Go).
  Donnée **dérivée**, entièrement reconstructible depuis les images ; ne contient
  **aucun** catalogage. À une restauration, digiKam régénère les vignettes tout
  seul (au fil de la navigation, ou via *Outils → Maintenance → Reconstruire les
  vignettes*) — seul coût : du temps CPU. Comme il change à chaque nouvelle
  vignette, le garder = re-téléversements en boucle pour rien. **À exclure** (nom
  exact).
- `*-wal`, `*-shm`, `*.db-journal` — temporaires SQLite qui apparaissent **à côté**
  des bases digiKam. Transitoires, sûrs.
- **À GARDER absolument** — ce sont tes vraies données de catalogue, non
  régénérables :
  - `digikam4.db` — albums, mots-clés/tags, notations, légendes, géolocalisation,
    **assignations de visages**. Irremplaçable. (Déjà sauvegardé chez toi.)
  - `recognition.db` — modèle entraîné de reconnaissance faciale (optionnel : les
    tags de visages sont dans `digikam4.db` ; sans lui, digiKam ré-apprend juste
    la reconnaissance automatique). À garder pour l'avoir opérationnel dès la
    restauration.
  - `similarity.db` — empreintes de recherche de similarité/doublons (optionnel,
    régénérable).
- GIMP : peu de temporaires dans le dossier de travail. Voir « À ton jugement »
  pour `*.xcf~`.
- gThumb / Pix : stockent parfois des commentaires dans un dossier `.comments`.
  **Non exclu par défaut** (ce sont des métadonnées que tu peux vouloir garder).

### Audio — Audacity, Tenacity, SoundConverter, FFAudioConverter, Puddletag, VLC
- `*-wal`, `*-shm` — temporaires SQLite des projets `.aup3` (Audacity/Tenacity).
- Les convertisseurs (SoundConverter, FFAudioConverter) écrivent le fichier final ;
  pas de motif notable. Puddletag écrit les tags en place. VLC ne laisse pas de
  temporaire dans les dossiers médias.

### Livres numériques — Calibre, Sigil
- `.caltrash` — **corbeille de la bibliothèque Calibre**.
- `*-wal`, `*-shm`, `*.db-journal` — temporaires SQLite de `metadata.db` (Calibre).
- Sigil travaille dans un dossier temporaire système (hors arborescence surveillée).

### Gestionnaires de mots de passe / chiffrement — KeePassXC, VeraCrypt
- **Ne rien exclure par extension ici.** Le `.kdbx` (KeePassXC) et les conteneurs
  VeraCrypt sont tes **vraies données** — ils doivent être sauvegardés.
- KeePassXC enregistre de façon atomique (temporaire puis renommage). Si tu as
  activé « Sauvegarder la base avant enregistrement », il crée un `*.old.kdbx` —
  voir « À ton jugement » si tu veux l'exclure.

### Téléphone / Android (copie de dossier entier via SFTP/FolderSync)
- `.thumbnails` — dossier de vignettes.
- `.trashed-*` — fichiers en corbeille (Android 11+).
- `.pending-*` — fichiers MediaStore en cours d'écriture.
- `LOST.DIR` — dossier de récupération de carte SD.
- **Ne pas exclure** `.nomedia` (marqueur intentionnel, inoffensif et minuscule).

### Reliques d'autres systèmes (copies de support)
- **Apple** : `.DS_Store`, `._*` (forks AppleDouble), `.Spotlight-V100`,
  `.Trashes`, `.fseventsd`, `.DocumentRevisions-V100`, `.TemporaryItems`.
- **Windows** : `Thumbs.db`, `ehthumbs.db`, `desktop.ini`, `$RECYCLE.BIN`,
  `System Volume Information` (et l'ancien `RECYCLER` sur de vieux supports).

### Téléchargements partiels (généraux)
- `*.part`, `*.crdownload`, `*.partial` — téléchargements incomplets.

---

## À ton jugement — à n'ajouter que si tu es sûr

Ces motifs visent des fichiers **parfois** intentionnels. Ne les ajoute que si tu
ne gardes pas ce type de fichier volontairement.

- `*.bak`, `*.old`, `*.orig` — sauvegardes/renommages divers.
- `*.xcf~` — sauvegarde automatique GIMP.
- `*.old.kdbx` — sauvegarde KeePassXC (si l'option est activée). **Jamais** `*.kdbx`.
- *core dumps* — un programme qui plante laisse un fichier `core` ou `core.<pid>`.
  **Éviter** le motif large `core.*` : il attraperait des fichiers légitimes comme
  `core.js` ou `core.css` (présents dans du code sous `Logiciels`). Si tu en vois,
  exclus le nom précis (ex. `core.12345`) plutôt qu'un motif générique.

---

## Avertissements

- **Jamais** de motif qui pourrait matcher un conteneur **VeraCrypt** ou une base
  **KeePassXC** : ce sont des données, pas des temporaires.
- Rester prudent sur les extensions génériques. `*.tmp`, `*.part`, `.nfs*`,
  `*-wal`, `*-shm` sont sans danger. Les suffixes `*.bak` / `*.old` / `*.orig`
  demandent réflexion.
- Les exclusions **globales** sont poussées vers le NAS automatiquement à
  l'enregistrement ; elles s'appliquent donc aux deux watchers (machine locale et NAS).
- Rappel de comportement : le **watcher continue de déposer un marqueur** pour un
  chemin exclu (il n'est pas « conscient des exclusions ») ; c'est le **moteur**
  qui écarte le fichier — d'où un éventuel `🚫 exclu` dans le journal, sans
  téléversement. Point différé si le bruit devient gênant.

---

## Note voisine : extensions MAJUSCULES et aperçus Proton (hors exclusions)

Cas voisin d'un « fichier qui coince à l'upload », mais qui ne relève **pas** des
exclusions (le fichier doit être sauvegardé) : le CLI Proton déduit le type MIME
de l'extension **de façon sensible à la casse**. Une extension majuscule
(`DOC.PDF`, `IMG.JPG`) est mal typée → ni vignette, ni aperçu, ni icône dans les
apps Proton, **silencieusement**. Le moteur corrige ça à la source en
**renommant** l'extension en minuscule (voir README, section « Robustesse
d'upload, vignettes et détection MIME »), avec garde-fou anti-collision et journal
`~/.proton_sync/renamed-extensions.log`. Désactivable par `--no-rename-ext`.
Cas connexe : TIFF/HEIC/AVIF échouent la génération de vignette (codec image) —
le moteur re-téléverse alors automatiquement avec `--skip-thumbnails` (fichier
sauvegardé, sans aperçu intégré Proton ; convertir en JPEG/PNG pour un aperçu).
