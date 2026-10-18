#!/usr/bin/env bash
# Fresh-search release: switch web and worker to the release image, or roll back to the image
# recorded by the baseline gate. Owner-run on the EC2 instance (docs/RELEASE_FRESH_SEARCH.md).
#
#   SITE=<public host> sudo --preserve-env=SITE,REL,APP ./fresh_search_switch.sh switch <S>
#   SITE=<public host> sudo --preserve-env=SITE,REL,APP ./fresh_search_switch.sh rollback
#
# Every failed precondition stops BEFORE anything changes (exit 1). A failure after the switch
# rolls back by the recorded image ID (never by the tag) and exits 1; a rollback that cannot be
# verified exits 2 = manual intervention. Exit 0 = both services run the release image.
set -Eeuo pipefail

REL=${REL:-/home/ubuntu/release-fresh}      # the one release directory: manifests, PRE_ID
APP=${APP:-/home/ubuntu/manzil-ai}          # holds deploy/compose.yml and .env
DOCKER=${DOCKER:-docker}
CURL=${CURL:-curl}
SLEEP=${SLEEP:-3}
: "${SITE:?set SITE to the public host name (no scheme)}"

die()  { echo "STOP: $*" >&2; exit 1; }
fail() { echo "MANUAL INTERVENTION: $*" >&2; exit 2; }
compose() { $DOCKER compose -f deploy/compose.yml --env-file .env "$@"; }
image_of() { $DOCKER inspect "$1" --format '{{.Image}}'; }
id_of_image() { $DOCKER image inspect "$1" --format '{{.Id}}'; }

both_on() {   # both services' containers run image id $1
  local c
  for c in deploy-web-1 deploy-worker-1; do
    [[ "$(image_of "$c")" == "$1" ]] || return 1
  done
}

alive() {     # the public routes answer
  $CURL -fsS "https://$SITE/health" >/dev/null || return 1
  [[ "$($CURL -s -o /dev/null -w '%{http_code}' -X POST "https://$SITE/webhook" -d '{}')" == 401 ]]
}

worker_started() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    compose logs --since 3m worker 2>/dev/null | grep -q 'Worker started' && return 0
    sleep "$SLEEP"
  done
  return 1
}

release_ok() {   # $1 = release image id
  both_on "$1" || return 1
  local s
  for s in web worker; do   # the files the running containers actually hold
    compose exec -T "$s" sh -c 'cd /app && sha256sum -c -' < "$REL/SHA256SUMS" >/dev/null || return 1
  done
  alive && worker_started
}

cd "$APP"
[[ -s "$REL/PRE_ID" ]] || die "$REL/PRE_ID is missing: run the baseline gate first"
PRE_ID=$(<"$REL/PRE_ID")
[[ "$PRE_ID" == sha256:* ]] || die "PRE_ID is not an image id"

rollback_to_recorded() {
  echo "ROLLING BACK to the recorded image $PRE_ID" >&2
  $DOCKER image inspect "$PRE_ID" >/dev/null 2>&1 || fail "recorded image $PRE_ID is missing"
  $DOCKER tag "$PRE_ID" manzil-ai:latest || fail "could not re-tag the recorded image"
  compose up -d --no-deps --no-build --force-recreate web worker || fail "recreate failed"
  both_on "$PRE_ID" || fail "web / worker are not on $PRE_ID after the rollback"
  alive || fail "the public routes do not answer after the rollback"
  echo "rolled back: web and worker run $PRE_ID" >&2
}

case "${1:-}" in
  switch)
    S=${2:?release id: the first 7 characters of the merge commit}
    NEW="manzil-ai:fresh-$S"
    # --- preconditions: nothing has changed yet ---------------------------------------------
    [[ -s "$REL/SHA256SUMS" ]] || die "$REL/SHA256SUMS is missing"
    [[ "$(id_of_image manzil-ai:latest)" == "$PRE_ID" ]] \
      || die "manzil-ai:latest is not the recorded image $PRE_ID"
    both_on "$PRE_ID" || die "web and worker are not both on the recorded image"
    WANT=$(id_of_image "$NEW") || die "release image $NEW does not exist"
    [[ "$WANT" != "$PRE_ID" ]] || die "the release image is the recorded image"
    # --- the switch ---------------------------------------------------------------------------
    $DOCKER tag "$PRE_ID" manzil-ai:pre-fresh
    $DOCKER tag "$WANT" manzil-ai:latest
    if compose up -d --no-deps --no-build --force-recreate web worker && release_ok "$WANT"; then
      echo "OK: web and worker run $NEW ($WANT)"
      exit 0
    fi
    echo "the release did not verify" >&2
    rollback_to_recorded
    exit 1
    ;;
  rollback)
    rollback_to_recorded
    ;;
  *)
    die "usage: $0 switch <release id> | rollback"
    ;;
esac
