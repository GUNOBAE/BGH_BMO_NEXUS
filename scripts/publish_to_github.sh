#!/usr/bin/env bash
# ONE-TIME user-run publish helper. Creates the public GitHub repo if it does not exist.
# Requires GitHub CLI already authenticated and a configured Git commit identity.
set -euo pipefail

REPO='GUNOBAE/BGH_BMO_NEXUS'
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

for c in gh git; do
  if ! command -v "$c" >/dev/null 2>&1; then
    printf 'ERROR: %s is not installed.\n' "$c" >&2
    exit 1
  fi
done

if [ -d .git ]; then
  printf 'ERROR: .git already exists. Do not run this initial-publish script twice.\n' >&2
  exit 1
fi

if gh repo view "$REPO" >/dev/null 2>&1; then
  printf 'ERROR: %s already exists or is visible. Stop to avoid overwriting any repository.\n' "$REPO" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  printf 'ERROR: GitHub CLI is not authenticated. Run: gh auth login\n' >&2
  exit 1
fi

if [ -z "$(git config user.name || :)" ] || [ -z "$(git config user.email || :)" ]; then
  printf '%s\n' 'ERROR: Git commit identity not set.' >&2
  printf '%s\n' 'Set git config --global user.name and user.email first.' >&2
  exit 1
fi

printf '\nWill create PUBLIC repository: %s\n' "$REPO"
printf '%s\n' 'This uploads documents and scripts in the extracted folder, NOT any BMO runtime files.'
read -r -p 'Type CREATE to continue: ' answer
if [ "$answer" != 'CREATE' ]; then
  printf '%s\n' 'Cancelled; no repository created.'
  exit 0
fi

git init -b main
git add README.md README_EN.md START_HERE_KR.md .gitignore docs scripts
# Refuse to create an empty commit.
git diff --cached --quiet && { echo 'ERROR: no files staged'; exit 1; }
git commit -m 'docs: initialize BMO NEXUS architecture and safe-start guide'
gh repo create "$REPO" --public --source=. --remote=origin --push --description 'Dual-mode Raspberry Pi BMO robot, personal cloud and web lab infrastructure'
printf '\nSuccess. Verify: https://github.com/%s\n' "$REPO"
