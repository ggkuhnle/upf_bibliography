#!/usr/bin/env bash
set -euo pipefail

OUTPUT="output"
HOST="gunter@kuhnle.co.uk"

PREFIX=$(python3 -c "import json; print(json.load(open('config.json'))['prefix'])")
REMOTE="${HOST}:~/misc/${PREFIX}"
INDEX_REMOTE="${HOST}:~/misc/"

echo "Deploying prefix='${PREFIX}' → ${REMOTE}"

rsync -avz --progress "${OUTPUT}/${PREFIX}/" "${REMOTE}/"

echo "Deploying landing page → ${INDEX_REMOTE}"
rsync -avz --progress deploy/server_index.html "${INDEX_REMOTE}index.html"

echo "Done."
