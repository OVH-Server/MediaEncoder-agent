#!/bin/bash
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Ce script doit être lancé en root (sudo $0)" >&2
  exit 1
fi

echo "=== Vérification du GPU NVIDIA dans WSL2 ==="
if [ ! -e /dev/dxg ]; then
  echo "ERREUR : /dev/dxg absent — WSL2 GPU passthrough non disponible." >&2
  exit 1
fi

if ls /usr/lib/wsl/lib/libcuda.so* &>/dev/null; then
  echo "libcuda.so présente — driver Windows OK"
else
  echo "AVERTISSEMENT : libcuda.so absente de /usr/lib/wsl/lib/"
  echo "  → Vérifie que le driver NVIDIA >= 520 est installé sur Windows"
  echo "  → Lance 'nvidia-smi' dans PowerShell pour confirmer"
  echo "  On continue quand même l'installation du toolkit..."
fi

echo ""
echo "=== Installation du NVIDIA Container Toolkit ==="

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update -qq
apt-get install -y nvidia-container-toolkit

echo ""
echo "=== Génération du spec CDI (fix cuInit WSL2) ==="
mkdir -p /etc/cdi
nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml 2>&1 | grep -E "level=(info|error|warning)" | grep -v "sandboxutils"
nvidia-ctk cdi list

echo ""
echo "=== Configuration du runtime Docker + CDI ==="
nvidia-ctk runtime configure --runtime=docker

# Active CDI dans le daemon Docker
DAEMON_JSON=/etc/docker/daemon.json
if ! grep -q '"cdi"' "$DAEMON_JSON" 2>/dev/null; then
  python3 - <<'PYEOF'
import json, sys
path = "/etc/docker/daemon.json"
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception:
    cfg = {}
cfg.setdefault("features", {})["cdi"] = True
with open(path, "w") as f:
    json.dump(cfg, f, indent=4)
print("CDI activé dans daemon.json")
PYEOF
fi

systemctl restart docker

echo ""
echo "=== Test nvidia-smi dans un container ==="
if docker run --rm --device nvidia.com/gpu=all ubuntu nvidia-smi; then
  echo ""
  echo "GPU détecté avec succès — tu peux lancer : docker compose up -d"
else
  echo ""
  echo "ECHEC : le container ne voit pas le GPU."
  echo "  1. Lance 'nvidia-smi' dans PowerShell (Windows) pour vérifier le driver"
  echo "  2. Si le driver est OK, redémarre WSL2 : 'wsl --shutdown' depuis PowerShell"
  echo "  3. Relance ce script"
  exit 1
fi
