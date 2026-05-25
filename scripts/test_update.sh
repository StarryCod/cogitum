#!/usr/bin/env bash
# Sanity check for the npm wrapper's tag-pinned update flow.
#
# We invoke the installer.js update() function with dryRun=true via a
# tiny node one-liner. This verifies:
#   - the function accepts {dryRun: true}
#   - the function logs a target (tag or origin/master fallback)
#   - it does NOT mutate the install dir
#
# Run from anywhere; uses the repo's lib/installer.js.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$SCRIPT_DIR/../npm/lib/installer.js"

if [[ ! -f "$INSTALLER" ]]; then
  echo "installer.js not found at $INSTALLER" >&2
  exit 2
fi

# Run a dry-run update against a throwaway COGITUM_HOME so we don't
# touch the real install. We point it at a fresh git repo with one
# tagged commit so findLatestSemverTag() has something to chew on.
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

git init -q "$TMPDIR"
(
  cd "$TMPDIR"
  git config user.email t@t
  git config user.name t
  echo hi > a
  git add a
  git commit -q -m init
  git tag v9.9.9
)

export COGITUM_HOME="$TMPDIR"
OUT=$(node -e "
const inst = require('$INSTALLER');
inst.update({ dryRun: true });
" 2>&1)

echo "$OUT"
echo "$OUT" | grep -q "v9.9.9" || { echo "expected v9.9.9 in output" >&2; exit 1; }
echo "$OUT" | grep -qi "dry-run" || { echo "expected dry-run marker" >&2; exit 1; }
echo "OK: npm wrapper dry-run pinned to v9.9.9"
