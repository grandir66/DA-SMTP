#!/usr/bin/env bash
# Pacchettizza il codice del listener in un tarball, da includere nel bundle
# di installazione. Da rilanciare ogni volta che il listener cambia.
#
# Uso:
#   ./make-listener-bundle.sh                      # da /opt/stormshield-smtp-relay/
#   ./make-listener-bundle.sh /altro/path/listener # da path custom

set -euo pipefail

LISTENER_SRC="${1:-/opt/stormshield-smtp-relay}"
OUTPUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/listener-bundle"
OUTPUT="$OUTPUT_DIR/relay-bundle.tar.gz"

if [[ ! -d "$LISTENER_SRC/relay" ]]; then
    echo "ERROR: $LISTENER_SRC/relay non esiste" >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

cd "$LISTENER_SRC"
INCLUDED=("relay")
[[ -f "pyproject.toml" ]] && INCLUDED+=("pyproject.toml")
[[ -d "conf" ]] && INCLUDED+=("conf")

tar \
    --exclude='__pycache__' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.pytest_cache' \
    --exclude='build' \
    --exclude='dist' \
    --exclude='*.egg-info' \
    -czf "$OUTPUT" \
    "${INCLUDED[@]}"

SIZE=$(stat -c%s "$OUTPUT")
COUNT=$(tar -tzf "$OUTPUT" | wc -l)
echo "✓ Bundle creato: $OUTPUT"
echo "  size: $(numfmt --to=iec-i --suffix=B $SIZE)"
echo "  files: $COUNT"
echo "  src: $LISTENER_SRC"
