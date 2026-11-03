#!/usr/bin/env bash
# Fresh-search release, step 1 on the server (read-only): establish that the RUNNING web and
# worker containers are exactly the expected production build, then record the image id.
#
#   sudo --preserve-env=REL bash ./fresh_search_baseline.sh
#
# Needs in $REL (transferred and checksum-verified): SHA256SUMS.unchanged, SHA256SUMS.data,
# FILES.expected (the merge commit's file list), STATES.allowed (the verified production profile:
# every released file with the sha256 values, or ABSENT, production may hold).
# The gate collects EVERY mismatch, prints them all (with the actual hashes) and exits 1;
# nothing is accepted without an explained state. On success it writes $REL/PRE_ID (the image
# id both services run), which the switch and the rollback use instead of any tag.
set -Eeuo pipefail

REL=${REL:-/home/ubuntu/release-fresh}
DOCKER=${DOCKER:-docker}
problems=()
note() { problems+=("$*"); }
die()  { echo "STOP: $*" >&2; exit 1; }

for f in SHA256SUMS.unchanged SHA256SUMS.data FILES.expected STATES.allowed; do
  [[ -s "$REL/$f" ]] || die "$REL/$f is missing: transfer and verify the kit first"
done

web_image=$($DOCKER inspect deploy-web-1 --format '{{.Image}}')
worker_image=$($DOCKER inspect deploy-worker-1 --format '{{.Image}}')
[[ "$web_image" == sha256:* && "$web_image" == "$worker_image" ]] \
  || die "web ($web_image) and worker ($worker_image) are not on one image"

# stdin-fed checks need -i: without it docker exec reads nothing and the check would be empty
manifest_ok() {   # $1 container, $2 manifest
  local out
  out=$($DOCKER exec -i "$1" sh -c 'cd /app && sha256sum -c -' < "$2" 2>&1) \
    || note "$1: $(basename "$2"): $(grep -v ': OK$' <<<"$out" | tr '\n' ';')"
}

paths=$(cut -d' ' -f1 "$REL/STATES.allowed" | sort -u)

for c in deploy-web-1 deploy-worker-1; do
  # in-place edits: anything under /app that differs from the image the container started from
  changed=$($DOCKER diff "$c" | grep -E '^[ACD] /app' | grep -v '__pycache__' || true)
  [[ -z "$changed" ]] || note "$c: changes under /app that are not in its image: $(tr '\n' ';' <<<"$changed")"

  # what the running container really holds
  for m in SHA256SUMS.unchanged SHA256SUMS.data; do manifest_ok "$c" "$REL/$m"; done

  # files whose production state is explained one by one (hash values or ABSENT)
  for p in $paths; do
    got=$($DOCKER exec "$c" sh -c 'cd /app && if [ -e "$1" ]; then sha256sum "$1" | cut -d" " -f1; else echo ABSENT; fi' sh "$p")
    grep -qxF "$p $got" "$REL/STATES.allowed" \
      || note "$c: $p is in state $got; allowed: $(grep -F "$p " "$REL/STATES.allowed" | cut -d' ' -f2 | cut -c1-16 | tr '\n' ' ')"
  done

  # the file list: nothing extra; anything missing must be explained as ABSENT
  listing=$($DOCKER exec "$c" sh -c "cd /app && find app migrations demo -type f ! -name '*.pyc' ! -path '*__pycache__*' | LC_ALL=C sort")
  while IFS= read -r f; do note "$c: extra file $f"; done \
    < <(LC_ALL=C comm -13 "$REL/FILES.expected" <(printf '%s\n' "$listing"))
  while IFS= read -r f; do
    grep -qxF "$f ABSENT" "$REL/STATES.allowed" || note "$c: missing file $f"
  done < <(LC_ALL=C comm -23 "$REL/FILES.expected" <(printf '%s\n' "$listing"))
done

# the image new containers would start from (and the rollback target)
$DOCKER run --rm --entrypoint sh -v "$REL:/rel:ro" "$web_image" -c \
  'cd /app && sha256sum -c /rel/SHA256SUMS.unchanged >/dev/null && sha256sum -c /rel/SHA256SUMS.data >/dev/null' \
  || note "the image $web_image does not match the expected build"

if ((${#problems[@]})); then
  echo "STOP: ${#problems[@]} mismatch(es); nothing was recorded:" >&2
  printf '  - %s\n' "${problems[@]}" >&2
  exit 1
fi
printf '%s' "$web_image" > "$REL/PRE_ID"
echo "OK: web and worker run $web_image, which matches the expected production build; recorded in $REL/PRE_ID"
