#!/usr/bin/env bash
# Fix hardcoded "UPF" references in generated HTML files.
set -euo pipefail

OUTPUT="output"

fix() {
  local prefix="$1" title="$2"
  local dir="${OUTPUT}/${prefix}"
  if [ ! -d "$dir" ]; then
    echo "Skipping ${dir} (not found)"
    return
  fi
  echo "Fixing ${dir}/ → title='${title}'"
  for f in "${dir}"/*.html; do
    [ -f "$f" ] || continue
    sed -i '' \
      -e "s|UPF Co-authorship Network Explorer|${title} — Co-authorship Network Explorer|g" \
      -e "s|UPF Co-authorship Network|${title} — Co-authorship Network|g" \
      -e "s|Papers (UPF)|Papers|g" \
      "$f"
  done
}

fix upf       "Ultra-Processed Food Research"
fix flavanol  "Flavanol research"
fix flavonoid "Flavonoid research"
fix cf        "Cocoa flavanol research"

echo "Done."
