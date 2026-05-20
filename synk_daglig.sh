#!/bin/bash
# synk_daglig.sh — Daglig synk av finsk riksdags- och rättsdata.
#
# Körordning:
#   1. Finlex (inkrementell via årsbaserad checkpoint — statute, consolidated, propositioner, fördrag)
#   2. Chunkning + embedding för nya/uppdaterade dokument (fi + sv)
#
# Anropas av launchd dagligen kl. 04:15.
# Kör manuellt: bash ~/MCP-Servers/finland/synk_daglig.sh

set -euo pipefail

MAPP="$HOME/MCP-Servers/finland"
PYTHON="$HOME/MCP-Servers/.venv/bin/python3"
LOGG="$MAPP/logs/synk_daglig.log"

mkdir -p "$MAPP/logs"

echo "=============================" >> "$LOGG"
echo "Daglig synk startad: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOGG"
echo "=============================" >> "$LOGG"

# Ladda .env om den finns
if [ -f "$MAPP/.env" ]; then
    set -a
    source "$MAPP/.env"
    set +a
fi

cd "$MAPP"

# ---------------------------------------------------------------------------
# Steg 1: Finlex (inkrementell — checkpoint per dokumenttyp och år)
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 1: Finlex inkrementell synk" >> "$LOGG"
"$PYTHON" "$MAPP/01_synka_finlex.py" >> "$LOGG" 2>&1
echo "[$(date '+%H:%M:%S')] Steg 1 klar" >> "$LOGG"

# ---------------------------------------------------------------------------
# Steg 2: Chunkning + embedding för nya dokument (båda språken)
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 2: Chunkning och embedding (fi + sv)" >> "$LOGG"
"$PYTHON" "$MAPP/03_chunka_och_embedda.py" --sprak bada >> "$LOGG" 2>&1
echo "[$(date '+%H:%M:%S')] Steg 2 klar" >> "$LOGG"

echo "Daglig synk avslutad: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOGG"
echo "" >> "$LOGG"
