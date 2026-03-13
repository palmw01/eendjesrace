#!/bin/bash
# Opstartscript voor Railway: start Litestream (SQLite-backup naar Cloudflare R2)
# en daarna gunicorn als beheerd subproces.
set -e

LITESTREAM_VERSION="0.3.13"
LITESTREAM_BIN="/tmp/litestream"

# Zorg dat DATABASE altijd een absoluut pad heeft (Railway volume staat op /app)
export DATABASE="${DATABASE:-/app/data/eendjes.db}"

# ── Litestream binary downloaden als die er nog niet is ──────────────────────
if [ ! -f "$LITESTREAM_BIN" ]; then
    echo "[start.sh] Litestream v${LITESTREAM_VERSION} downloaden..."
    LITESTREAM_URL="https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz"
    if command -v curl &>/dev/null; then
        curl -fsSL "$LITESTREAM_URL" | tar -xz -C /tmp
    elif command -v wget &>/dev/null; then
        wget -qO- "$LITESTREAM_URL" | tar -xz -C /tmp
    else
        python3 -c "
import urllib.request, tarfile, sys
url = sys.argv[1]
urllib.request.urlretrieve(url, '/tmp/litestream.tar.gz')
" "$LITESTREAM_URL"
        tar -xzf /tmp/litestream.tar.gz -C /tmp
    fi
    chmod +x "$LITESTREAM_BIN"
    echo "[start.sh] Litestream klaar."
fi

# ── Database herstellen als die nog niet bestaat (bijv. na volume-verlies) ───
if [ -n "$LITESTREAM_ACCESS_KEY_ID" ] && [ ! -f "$DATABASE" ]; then
    echo "[start.sh] Database niet gevonden — herstelpoging vanuit R2-replica..."
    "$LITESTREAM_BIN" restore -config litestream.yml "$DATABASE" 2>/dev/null \
        && echo "[start.sh] Database hersteld." \
        || echo "[start.sh] Geen replica gevonden, starten met lege database."
fi

# ── Start Litestream + Gunicorn ───────────────────────────────────────────────
echo "[start.sh] Litestream replicatie + Gunicorn starten..."
exec "$LITESTREAM_BIN" replicate -config litestream.yml \
    -exec "gunicorn app:app"
