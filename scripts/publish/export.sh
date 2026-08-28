#!/usr/bin/env bash
# Exports the publishable tree of this repo (committed HEAD only) into an
# empty target directory using `git archive`, which honours the
# export-ignore rules in .gitattributes: CLAUDE.md, .claude/, scripts/
# and .gitattributes itself never leave this repository.
#
# Usage: scripts/publish/export.sh <empty-target-dir>
set -euo pipefail

PRIVATE_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET="${1:?usage: export.sh <empty-target-dir>}"

mkdir -p "$TARGET"
if [ -n "$(ls -A "$TARGET")" ]; then
  echo "export.sh: target dir is not empty: $TARGET" >&2
  exit 2
fi

if [ -n "$(git -C "$PRIVATE_REPO" status --porcelain)" ]; then
  echo "export.sh: WARNING: working tree is dirty; exporting committed" \
    "HEAD only - uncommitted changes are NOT included." >&2
fi

HEAD_COMMIT="$(git -C "$PRIVATE_REPO" rev-parse HEAD)"
git -C "$PRIVATE_REPO" archive --format=tar HEAD | tar -x -C "$TARGET"

# Belt and suspenders: these must never appear in an export. gate.py
# re-checks the same list (FORBIDDEN) independently.
for forbidden in CLAUDE.md .claude .gitattributes scripts; do
  if [ -e "$TARGET/$forbidden" ]; then
    echo "export.sh: FATAL: '$forbidden' leaked into the export." >&2
    exit 3
  fi
done

echo "export.sh: exported private commit $HEAD_COMMIT to $TARGET"
echo "export.sh: (do NOT put this commit hash anywhere public)"
