# MediaEncoder — agent

Agent d'encodage NVENC pour
[MediaEncoder-server](https://github.com/RollinLondon/MediaEncoder-server) :
tourne sur un PC équipé d'un GPU NVIDIA, attend les jobs du serveur, télécharge
le fichier, le convertit (hevc_nvenc / h264_nvenc) et le renvoie.

- Un job à la fois ; progression (fps, ETA), annulation propre (kill ffmpeg).
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
| `API_KEY` | Clé partagée serveur ↔ agent |
| `FLASK_PORT` | Port publié sur l'hôte |
| `WORK_PATH` | Chemin hôte de stockage des fichiers à compresser / compressés (ex. `/data/hdd_1To/media-encoder`) |

Vérifier que NVENC est détecté :

```sh
docker compose logs mediaencoder-agent
# → Encodeurs NVENC : ['hevc_nvenc', 'h264_nvenc', …] — GPU : NVIDIA GeForce GTX 1060…
```

Notes GTX 1060 : encodeur Pascal — pas d'AV1, pas de B-frames en HEVC ;
`hevc_nvenc` CQ 24 donne typiquement −40 à −60 % sur des sources H.264.
