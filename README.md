# MediaEncoder — agent

Agent d'encodage NVENC pour le module encoder de
[manage-nas](https://github.com/OVH-Server/manage-nas) :
tourne sur un PC équipé d'un GPU NVIDIA, attend les jobs du serveur, télécharge
le fichier, le convertit (hevc_nvenc / h264_nvenc) et le renvoie.

- Un job à la fois ; progression (fps, ETA), annulation propre (kill ffmpeg).
- **Préchargement pipeline** : pendant l'encodage du job N, le fichier du job
  N+1 est téléchargé en arrière-plan (payload `prefetch` reçu dans la réponse
  à `/progress`) → zéro temps mort entre deux jobs. La progression du
  préchargement est rapportée au serveur (`prefetch_progress`) à chaque tick.
  Si le job préchargé est réclamé alors que le download est encore en cours,
  l'agent attend la fin du thread en relayant la progression (state
  `downloading`).
- **Déconnexion propre** : SIGTERM/SIGINT (`docker compose down`) interrompt
  les boucles via un `threading.Event` puis POST `/api/agent/disconnect` —
  le serveur marque l'agent hors-ligne immédiatement (sans attendre les 90s
  d'expiration du heartbeat) et libère ses jobs.
- **Intégrité des transferts (SHA-256)** : après téléchargement complet d'une
  source, l'agent compare son hash local (calculé au fil de l'écriture) au
  checksum retourné par `GET /transfer/<id>/checksum`. À l'upload, le hash du
  fichier encodé part dans le header `X-Content-Sha256` et le serveur vérifie
  côté réception. Mismatch → job en erreur (source) / 422 (upload), l'original
  n'est jamais remplacé par un fichier corrompu.
- Pistes audio, sous-titres, chapitres et pièces jointes copiés sans ré-encodage.
- Les conteneurs exotiques (avi, ts, wmv…) sont remuxés en `.mkv`.
- Vérifie au démarrage que NVENC s'initialise vraiment (GPU visible) ; repli
  CPU libx265/libx264 sinon.

## Déploiement

Prérequis : pilote NVIDIA +
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
(sous Windows : Docker Desktop + WSL2 avec support GPU).

```sh
cp .env.example .env   # même API_KEY que le serveur
docker compose up -d --build
```

| Variable | Rôle |
|---|---|
| `SERVER_URL` | URL du serveur (ex. `https://nas.example.com/encoder`) |
| `API_KEY` | Clé partagée serveur ↔ agent (`X-Agent-Key`) |
| `AGENT_ID` | Identifiant de l'agent côté serveur (défaut : hostname du conteneur) |
| `POLL_INTERVAL` | Secondes entre deux sondages `/jobs/claim` (défaut : 5) |
| `BASIC_AUTH` | `user:pass` optionnel si le serveur est derrière Authentik/Basic Auth |
| `WORK_PATH` | Chemin hôte de stockage des fichiers à compresser / compressés (ex. `/data/hdd_1To/media-encoder`) |

Vérifier que NVENC est détecté :

```sh
docker compose logs mediaencoder-agent
# → Encodeurs NVENC : ['hevc_nvenc', 'h264_nvenc', …] — GPU : NVIDIA GeForce GTX 1060…
```

Notes GTX 1060 : encodeur Pascal — pas d'AV1, pas de B-frames en HEVC ;
`hevc_nvenc` CQ 24 donne typiquement −40 à −60 % sur des sources H.264.

## Développement

Ce dépôt est un fork de
[RollinLondon/MediaEncoder-agent](https://github.com/RollinLondon/MediaEncoder-agent).

### Branches

| Branche | Rôle |
|---------|------|
| `main`  | Miroir exact de `upstream/main` — ne jamais committer directement dessus |
| `nfour` | Branche custom (fix GTX 1060, config OVH) — c'est ici que l'on travaille |

### Setup après un clone

```sh
git remote add upstream git@github.com:RollinLondon/MediaEncoder-agent
git fetch upstream
git checkout nfour
```

### Récupérer les nouveaux changements depuis l'upstream

```sh
git fetch upstream
git checkout main && git merge --ff-only upstream/main
git checkout nfour && git rebase main
```
