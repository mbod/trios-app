#!/usr/bin/env bash
# Deploy the current version from GitHub.
#
#   ./deploy.sh              # deploy main
#   ./deploy.sh some-branch  # deploy another branch
#   ./deploy.sh main --force # deploy even if sessions are running (they will end)
set -euo pipefail
cd "$(dirname "$0")"

branch="${1:-main}"
force="${2:-}"

# 1. Get the code, and make sure the folder matches GitHub exactly
git fetch origin
git checkout "$branch"
git pull --ff-only origin "$branch"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Uncommitted changes in $(pwd); not deploying."
    git status --short
    exit 1
fi

# 2. Don't restart while sessions are running (restarting ends them)
active=$(curl -fsS --max-time 5 http://127.0.0.1:5555/health 2>/dev/null \
    | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["active_sessions"]))' \
    || true)
if [ -n "$active" ] && [ "$force" != "--force" ]; then
    echo "Sessions running in room(s): $active"
    echo "Try again when they've finished, or add --force."
    exit 1
fi

# 3. Build and restart, recording the commit in the image
export GIT_COMMIT=$(git rev-parse HEAD)
mkdir -p data/logs
docker compose build
docker compose up -d

echo "Deployed $branch at $GIT_COMMIT"
docker compose ps
