#!/usr/bin/env bash
# Build all topics and deploy.
# make_dashboard.py handles everything for each topic (dashboards + networks).
# Usage: ./build_all.sh [--fetch] [--no-networks]
set -euo pipefail

ARGS=()
for arg in "$@"; do
    case "$arg" in
        --fetch) ARGS+=("$arg") ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Collect configs from config/, sorting flavonoid to the end (largest dataset)
configs=()
deferred=()
for cfg in config/config.json.*; do
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
    echo "  $title  ($prefix)"
    echo "════════════════════════════════════════"
    cp "$cfg" config.json
    python make_dashboard.py "${ARGS[@]+"${ARGS[@]}"}"
    ./deploy.sh
    echo "✓ Done: $prefix"
    echo
done

echo "All topics built and deployed."
