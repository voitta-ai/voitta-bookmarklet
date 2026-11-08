#!/usr/bin/env bash
# Deploy a tag or branch of voitta-bookmarklet on this host with Docker Compose.
#
#   deploy/deploy.sh v0.2.14                 # a release tag
#   deploy/deploy.sh feat/some-branch        # a branch (test before merging)
#   deploy/deploy.sh --rollback              # back to the image that ran before the last deploy
#
# Builds the image on this host (tagged with the version), restarts the
# container, and waits for /health. The previous image is kept and its tag
# written to .deploy-previous, so --rollback needs no rebuild.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f deploy/compose.yml)

wait_healthy() {
  for _ in $(seq 1 90); do
    if curl -fsS -o /dev/null http://127.0.0.1:12358/health; then return 0; fi
    sleep 2
  done
  echo "[deploy] /health did not come up; last logs:" >&2
  "${COMPOSE[@]}" logs --tail 80 >&2 || true
  return 1
}

[ -f .env ] || { echo "[deploy] .env missing — copy deploy/.env.example and fill it in" >&2; exit 1; }
mkdir -p data

current_tag() { docker inspect -f '{{.Config.Image}}' voitta-bookmarklet 2>/dev/null | sed 's/^voitta-bookmarklet://' || true; }

if [ "${1:-}" = "--rollback" ]; then
  prev="$(cat .deploy-previous 2>/dev/null || true)"
  [ -n "$prev" ] || { echo "[deploy] nothing to roll back to" >&2; exit 1; }
  echo "[deploy] rolling back to $prev"
  VOITTA_TAG="$prev" "${COMPOSE[@]}" up -d --no-build
  wait_healthy && echo "[deploy] up: $prev"
  exit
fi

REF="${1:?usage: deploy/deploy.sh <tag|branch> | --rollback}"
git fetch --tags --prune --quiet origin
if git rev-parse -q --verify "origin/$REF^{commit}" >/dev/null; then
  git checkout --quiet --detach "origin/$REF"
else
  git checkout --quiet --detach "$REF"
fi
VERSION="$(git describe --tags --always --dirty)"
VERSION="${VERSION//\//-}"

before="$(current_tag)"
echo "[deploy] building $VERSION (running now: ${before:-none})"
VOITTA_TAG="$VERSION" "${COMPOSE[@]}" build
VOITTA_TAG="$VERSION" "${COMPOSE[@]}" up -d
if [ -n "$before" ] && [ "$before" != "$VERSION" ]; then echo "$before" > .deploy-previous; fi
wait_healthy
echo "[deploy] up: $VERSION   (rollback: deploy/deploy.sh --rollback)"
