#!/usr/bin/env bash
set -euo pipefail

REMOTE="gunter@kuhnle.co.uk:~/bibliometric/"
#REMOTE="gunter@kuhnle.co.uk:~/misc/"
OUTPUT="output"

PREFIX=$(python3 -c "import json; print(json.load(open('config.json'))['prefix'])")

echo "Deploying prefix='${PREFIX}' → ${REMOTE}"

rsync -avz --progress "${OUTPUT}/${PREFIX}"*.html "${REMOTE}"
rsync -avz --progress "${OUTPUT}/${PREFIX}"*.png  "${REMOTE}"
rsync -avz --progress "${OUTPUT}/index.html"      "${REMOTE}"

echo "Done."
