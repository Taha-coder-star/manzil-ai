#!/usr/bin/env bash
# Fresh-search release, step 1 on the server (read-only): establish that the RUNNING web and
# worker containers are exactly the expected production build, then record the image id.
#
#   sudo --preserve-env=REL ./fresh_search_baseline.sh
#
# Needs in $REL (already transferred and checksum-verified): SHA256SUMS.unchanged,
# SHA256SUMS.base, FILES.expected, TURN_ENGINE.allowed. Any failed check stops (exit 1) and
# writes nothing. On success it writes $REL/PRE_ID (the image id both services run), which the
# switch and the rollback use instead of any tag.
set -Eeuo pipefail

REL=${REL:-/home/ubuntu/release-fresh}
DOCKER=${DOCKER:-docker}
die() { echo "STOP: $*" >&2; exit 1; }

for f in SHA256SUMS.unchanged SHA256SUMS.base FILES.expected TURN_ENGINE.allowed; do
  [[ -s "$REL/$f" ]] || die "$REL/$f is missing: transfer and verify the kit first"
done

web_image=$($DOCKER inspect deploy-web-1 --format '{{.Image}}')
worker_image=$($DOCKER inspect deploy-worker-1 --format '{{.Image}}')
[[ "$web_image" == sha256:* && "$web_image" == "$worker_image" ]] \
  || die "web ($web_image) and worker ($worker_image) are not on one image"

for c in deploy-web-1 deploy-worker-1; do
  # in-place edits: anything under /app that differs from the image the container started from
  changed=$($DOCKER diff "$c" | grep -E '^[ACD] /app' | grep -v '__pycache__' || true)
  [[ -z "$changed" ]] || die "$c holds changes under /app that are not in its image: $changed"

  # what the running container really holds
  $DOCKER exec "$c" sh -c 'cd /app && sha256sum -c -' < "$REL/SHA256SUMS.unchanged" >/dev/null \
    || die "$c: a module this release does not change differs from the expected build"
  $DOCKER exec "$c" sh -c 'cd /app && sha256sum -c -' < "$REL/SHA256SUMS.base" >/dev/null \
    || die "$c: a module that this release replaces is not at the 91b7cef version"
  listing=$($DOCKER exec "$c" sh -c "cd /app && find app -name '*.py' | sort")
  [[ "$listing" == "$(<"$REL/FILES.expected")" ]] \
    || die "$c: the list of Python modules differs (an extra or missing file)"
  got=$($DOCKER exec "$c" sh -c 'cd /app && sha256sum app/turn_engine.py' | cut -d' ' -f1)
  grep -qx "$got" "$REL/TURN_ENGINE.allowed" \
    || die "$c: app/turn_engine.py ($got) is not a known state; diff it by hand (runbook 5)"
done

# the image new containers would start from (and the rollback target)
$DOCKER run --rm --entrypoint sh -v "$REL:/rel:ro" "$web_image" -c \
  'cd /app && sha256sum -c /rel/SHA256SUMS.unchanged >/dev/null && sha256sum -c /rel/SHA256SUMS.base >/dev/null' \
  || die "the image $web_image does not match the expected build"

printf '%s' "$web_image" > "$REL/PRE_ID"
echo "OK: web and worker run $web_image, which matches the expected production build; recorded in $REL/PRE_ID"
