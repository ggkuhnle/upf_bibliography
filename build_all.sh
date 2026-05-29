#!/usr/bin/env bash
# Build all topics and deploy. Flavonoids run last (largest dataset).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Collect configs, sorting flavonoid to the end
configs=()
deferred=()
for cfg in config.json.*; do
    if [[ "$cfg" == *flavonoid* ]]; then
        deferred+=("$cfg")
    else
        configs+=("$cfg")
    fi
done
configs+=("${deferred[@]}")

echo "Build order: ${configs[*]}"
echo

original_config=""
if [ -f config.json ]; then
    original_config=$(cat config.json)
fi

restore_config() {
    if [ -n "$original_config" ]; then
        echo "$original_config" > config.json
    fi
}
trap restore_config EXIT

for cfg in "${configs[@]}"; do
    prefix=$(python3 -c "import json; print(json.load(open('$cfg'))['prefix'])")
    title=$(python3  -c "import json; print(json.load(open('$cfg'))['title'])")
    echo "════════════════════════════════════════"
    echo "  Topic : $title  ($prefix)"
    echo "  Config: $cfg"
    echo "════════════════════════════════════════"

    cp "$cfg" config.json

    echo "→ Building dashboards…"
    python make_dashboard.py

    echo "→ Deploying…"
    ./deploy.sh

    echo "✓ Done: $prefix"
    echo
done

echo "All topics built and deployed."
