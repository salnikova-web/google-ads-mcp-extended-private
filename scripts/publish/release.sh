#!/usr/bin/env bash
# Publishes committed main of the private repo to the public clone as ONE
# fresh commit (the public repo never sees private history). Pipeline:
# guard -> export -> neutrality gate -> clean-room test run -> sync ->
# commit -> tag -> gate again on the clone. It does NOT push: it prints
# the exact push commands so a human reviews the final state first.
#
# Usage: scripts/publish/release.sh <version>        e.g. 0.3.1
# Env:   GOOGLE_ADS_MCP_PUBLIC_CLONE  path to the public clone
#        (default: $HOME/Documents/Develop/google-ads-mcp-public)
set -euo pipefail

VERSION="${1:?usage: release.sh <version, e.g. 0.3.1>}"
PRIVATE_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEFAULT_CLONE="$HOME/Documents/Develop/google-ads-mcp-public"
PUBLIC_CLONE="${GOOGLE_ADS_MCP_PUBLIC_CLONE:-$DEFAULT_CLONE}"
VENV_PY="$PRIVATE_REPO/.venv/bin/python"

# Neutral identity for every object created in the public clone
# (GIT_AUTHOR_* forces the author, GIT_COMMITTER_* forces the committer
# and the tagger of annotated tags).
export GIT_AUTHOR_NAME="google-ads-mcp-extended contributors"
export GIT_AUTHOR_EMAIL="noreply@example.com"
export GIT_COMMITTER_NAME="google-ads-mcp-extended contributors"
export GIT_COMMITTER_EMAIL="noreply@example.com"

# --- Guards: only committed main of the private repo is publishable ---
if [ -n "$(git -C "$PRIVATE_REPO" status --porcelain)" ]; then
  echo "release.sh: private working tree is dirty; commit first." >&2
  exit 2
fi
BRANCH="$(git -C "$PRIVATE_REPO" branch --show-current)"
if [ "$BRANCH" != "main" ]; then
  echo "release.sh: private repo is on '$BRANCH', not main." >&2
  exit 2
fi

PYPROJECT_VERSION="$("$VENV_PY" - "$PRIVATE_REPO/pyproject.toml" <<'EOF'
import sys
import tomllib

with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f)["project"]["version"])
EOF
)"
if [ "$PYPROJECT_VERSION" != "$VERSION" ]; then
  echo "release.sh: version mismatch: pyproject=$PYPROJECT_VERSION" \
    "requested=$VERSION" >&2
  exit 2
fi

# --- Guards: public clone must exist, be clean, on main, up to date ---
if [ ! -d "$PUBLIC_CLONE/.git" ]; then
  echo "release.sh: no git repo at $PUBLIC_CLONE (see the first-publish" \
    "steps in CLAUDE.md)." >&2
  exit 2
fi
if [ -n "$(git -C "$PUBLIC_CLONE" status --porcelain)" ]; then
  echo "release.sh: public clone is dirty: $PUBLIC_CLONE" >&2
  exit 2
fi
PUB_BRANCH="$(git -C "$PUBLIC_CLONE" symbolic-ref --short HEAD)"
if [ "$PUB_BRANCH" != "main" ]; then
  echo "release.sh: public clone is on '$PUB_BRANCH', not main." >&2
  exit 2
fi
git -C "$PUBLIC_CLONE" fetch origin
if git -C "$PUBLIC_CLONE" rev-parse -q --verify origin/main >/dev/null; then
  git -C "$PUBLIC_CLONE" merge --ff-only origin/main
fi
if git -C "$PUBLIC_CLONE" rev-parse -q --verify \
  "refs/tags/v$VERSION" >/dev/null; then
  echo "release.sh: tag v$VERSION already exists in the public clone." >&2
  exit 2
fi

# --- Export and gate -------------------------------------------------
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gads-mcp-release.XXXXXX")"
EXPORT_DIR="$WORK_DIR/export"
"$PRIVATE_REPO/scripts/publish/export.sh" "$EXPORT_DIR"
"$VENV_PY" "$PRIVATE_REPO/scripts/publish/gate.py" --selftest
"$VENV_PY" "$PRIVATE_REPO/scripts/publish/gate.py" "$EXPORT_DIR"

# --- Clean-room test run: non-editable install of the exported tree --
# (fresh virtualenv on the repo venv's python 3.12; needs network or a
# warm pip cache for dependencies)
"$VENV_PY" -m virtualenv -q "$WORK_DIR/venv"
"$WORK_DIR/venv/bin/pip" -q install "$EXPORT_DIR" "pyfakefs>=5.0.0,<6.0"
(cd "$EXPORT_DIR" && "$WORK_DIR/venv/bin/python" -m unittest discover \
  --buffer -s tests -p "*_test.py")
(cd "$EXPORT_DIR" && "$WORK_DIR/venv/bin/python" -m unittest \
  tests.smoke.smoke_test)

# --- Sync, commit, tag ------------------------------------------------
rsync -a --delete --exclude=/.git "$EXPORT_DIR/" "$PUBLIC_CLONE/"
git -C "$PUBLIC_CLONE" add -A

if git -C "$PUBLIC_CLONE" rev-parse -q --verify HEAD >/dev/null; then
  MESSAGE="Release v$VERSION"
  if git -C "$PUBLIC_CLONE" diff --cached --quiet; then
    echo "release.sh: nothing changed since the last public release." >&2
    exit 2
  fi
else
  MESSAGE="Initial public release (v$VERSION)"
fi

git -C "$PUBLIC_CLONE" commit -m "$MESSAGE"
git -C "$PUBLIC_CLONE" tag -a "v$VERSION" -m "$MESSAGE"

# Final independent scan of exactly what is about to be pushed.
"$VENV_PY" "$PRIVATE_REPO/scripts/publish/gate.py" "$PUBLIC_CLONE"

echo
echo "release.sh: committed and tagged v$VERSION in $PUBLIC_CLONE" \
  "(NOT pushed)."
echo "Review, then push:"
echo "  git -C '$PUBLIC_CLONE' log --stat -1"
echo "  git -C '$PUBLIC_CLONE' push -u origin main"
echo "  git -C '$PUBLIC_CLONE' push origin v$VERSION"
