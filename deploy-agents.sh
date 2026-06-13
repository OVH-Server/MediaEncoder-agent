#!/usr/bin/env bash
#
# Déploie N agents MediaEncoder sur cet hôte, chacun dans son propre clone.
#
# Usage :
#   ./deploy-agents.sh <env-par-defaut> <nombre-agents> <branche>
#
# Exemple :
#   ./deploy-agents.sh ./default.env 3 nfour
#   ./deploy-agents.sh ~/secrets/agent.env 2 wsl-pc-portable
#
# Pour chaque agent i (1..N) :
#   • reclone le repo dans  ./agent-<branche>-<i>/  sur la branche demandée
#   • copie le .env fourni  (toutes les data : SERVER_URL, API_KEY, BASIC_AUTH…)
#   • AGENT_ID  = <base>-<i>          (base = AGENT_ID du .env, sinon hostname)
#   • WORK_PATH = <dossier-agent>/work (isolé — sinon le nettoyage WORK_DIR au
#                                       boot d'un agent effacerait le travail des autres)
#   • container_name unique           (sinon collision Docker sur le même hôte)
#   • docker compose up -d --build
#
set -euo pipefail

# ── Arguments ────────────────────────────────────────────────────────────────
if [[ $# -ne 3 ]]; then
    echo "Usage : $0 <env-par-defaut> <nombre-agents> <branche>" >&2
    echo "Exemple : $0 ./default.env 3 nfour" >&2
    exit 1
fi

ENV_FILE="$1"
COUNT="$2"
BRANCH="$3"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "✗ Fichier .env introuvable : $ENV_FILE" >&2
    exit 1
fi
if ! [[ "$COUNT" =~ ^[0-9]+$ ]] || [[ "$COUNT" -lt 1 ]]; then
    echo "✗ Nombre d'agents invalide : $COUNT (entier ≥ 1 attendu)" >&2
    exit 1
fi

# ── URL du repo (origin du clone courant, sinon URL connue) ──────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="$(git -C "$SCRIPT_DIR" config --get remote.origin.url 2>/dev/null || true)"
REPO_URL="${REPO_URL:-git@github.com:OVH-Server/MediaEncoder-agent.git}"

# ── Base de l'AGENT_ID : valeur du .env fourni, sinon hostname ───────────────
BASE_AGENT_ID="$(grep -E '^AGENT_ID=' "$ENV_FILE" | head -n1 | cut -d= -f2- || true)"
BASE_AGENT_ID="${BASE_AGENT_ID:-$(hostname)}"

# Nom de branche assaini pour les noms de dossier/conteneur (slashes → tirets)
SAFE_BRANCH="${BRANCH//\//-}"

# ── Helper : remplace ou ajoute KEY=VALUE dans un fichier .env ───────────────
set_env_var() {
    local file="$1" key="$2" val="$3"
    if grep -qE "^${key}=" "$file"; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$file"
    else
        printf '%s=%s\n' "$key" "$val" >> "$file"
    fi
}

# ── Commande docker compose (v2 « docker compose », repli « docker-compose ») ─
if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
else
    echo "✗ docker compose introuvable" >&2
    exit 1
fi

echo "════════════════════════════════════════════════════════════════"
echo " Déploiement de $COUNT agent(s)"
echo "   repo    : $REPO_URL"
echo "   branche : $BRANCH"
echo "   .env    : $ENV_FILE"
echo "   AGENT_ID: ${BASE_AGENT_ID}-1 … ${BASE_AGENT_ID}-${COUNT}"
echo "════════════════════════════════════════════════════════════════"

for i in $(seq 1 "$COUNT"); do
    DIR="agent-${SAFE_BRANCH}-${i}"
    AID="${BASE_AGENT_ID}-${i}"
    CNAME="mediaencoder-agent-${SAFE_BRANCH}-${i}"

    echo
    echo "── Agent $i/$COUNT  →  $DIR  (AGENT_ID=$AID) ───────────────────"

    # 1. Clone (ou mise à jour si le dossier existe déjà)
    if [[ -d "$DIR/.git" ]]; then
        echo "  • dossier existant → fetch + checkout $BRANCH"
        git -C "$DIR" fetch --quiet origin "$BRANCH"
        git -C "$DIR" checkout --quiet "$BRANCH"
        git -C "$DIR" reset --hard --quiet "origin/$BRANCH"
    elif [[ -e "$DIR" ]]; then
        echo "  ✗ $DIR existe mais n'est pas un clone git — ignoré" >&2
        continue
    else
        echo "  • clone $BRANCH"
        git clone --quiet --branch "$BRANCH" "$REPO_URL" "$DIR"
    fi

    # 2. .env : copie + overrides isolants
    cp "$ENV_FILE" "$DIR/.env"
    set_env_var "$DIR/.env" AGENT_ID  "$AID"
    set_env_var "$DIR/.env" WORK_PATH "$(cd "$DIR" && pwd)/work"
    mkdir -p "$DIR/work"

    # 3. container_name unique (évite la collision Docker sur le même hôte)
    if grep -qE '^\s*container_name:' "$DIR/docker-compose.yml"; then
        sed -i -E "s|^(\s*container_name:).*|\1 ${CNAME}|" "$DIR/docker-compose.yml"
    fi

    # 4. Build + up (nom de projet = nom du dossier, unique)
    echo "  • docker compose up -d --build"
    ( cd "$DIR" && "${DC[@]}" -p "$DIR" up -d --build )
    echo "  ✓ agent $AID lancé (conteneur $CNAME)"
done

echo
echo "════════════════════════════════════════════════════════════════"
echo " ✓ $COUNT agent(s) déployé(s)."
echo "   Logs   : docker logs -f $CNAME"
echo "   Stop   : cd <dossier-agent> && ${DC[*]} down"
echo "   Note   : plusieurs agents NVENC partagent le même GPU — la carte"
echo "            limite le nombre de sessions d'encodage simultanées."
echo "════════════════════════════════════════════════════════════════"
