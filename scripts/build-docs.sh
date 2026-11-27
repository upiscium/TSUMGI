#!/bin/sh
# Build the documentation site into the given directory (default: _site).
# Every warning fails the build, except the two known broken links in the frozen
# v0.1.1 release notes (release notes are pinned by SHA-256 and are never edited).
set -eu
out=${1:-_site}
log=$(mktemp)
sphinx-build -b html -q --keep-going docs "$out" 2>"$log" || true
unexpected=$(grep -i 'warning\|error' "$log" | grep -v "docs/releases/v0.1.1.*bonsai-llama-cpp.md" || true)
if [ -n "$unexpected" ]; then
  printf '%s\n' "$unexpected" >&2
  exit 1
fi
touch "$out/.nojekyll"
echo "documentation built in $out"
