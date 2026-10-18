#!/usr/bin/env bash
# Fresh-search release, after the build and before the switch (read-only): prove that the release
# image holds EXACTLY the tested merge commit's app/, migrations/ and demo/ trees.
#
#   sudo --preserve-env=REL bash ./fresh_search_image_check.sh manzil-ai:fresh-<S>
#
# Checks (all collected, then exit 1 if any failed): the 18 released files, the 43 other app
# modules and the 22 data files match their manifests; the file list equals FILES.release; the
# migrations 001-004 are in one of the explained states (Git LF blob or CRLF working-copy form);
# the user is not root; the runtime and the benchmark modules import.
set -Eeuo pipefail

REL=${REL:-/home/ubuntu/release-fresh}
DOCKER=${DOCKER:-docker}
IMG=${1:?image name or id}
problems=()
note() { problems+=("$*"); }

for f in SHA256SUMS SHA256SUMS.unchanged SHA256SUMS.data FILES.release STATES.allowed; do
  [[ -s "$REL/$f" ]] || { echo "STOP: $REL/$f is missing" >&2; exit 1; }
done
run() { $DOCKER run --rm --entrypoint sh -v "$REL:/rel:ro" "$IMG" -c "$1"; }

for m in SHA256SUMS SHA256SUMS.unchanged SHA256SUMS.data; do
  out=$(run "cd /app && sha256sum -c /rel/$m" 2>&1) \
    || note "$m: $(grep -v ': OK$' <<<"$out" | tr '\n' ';')"
done

listing=$(run "cd /app && find app migrations demo -type f ! -name '*.pyc' ! -path '*__pycache__*' | LC_ALL=C sort")
while IFS= read -r f; do note "extra file $f"; done \
  < <(LC_ALL=C comm -13 "$REL/FILES.release" <(printf '%s\n' "$listing"))
while IFS= read -r f; do note "missing file $f"; done \
  < <(LC_ALL=C comm -23 "$REL/FILES.release" <(printf '%s\n' "$listing"))

while read -r hash path; do   # sha256sum prints "<hash>  <path>"
  grep -qxF "$path $hash" "$REL/STATES.allowed" || note "$path is in an unexplained state $hash"
done < <(run 'cd /app && sha256sum migrations/00[1-4]_*')

[[ "$($DOCKER run --rm --entrypoint id "$IMG" -un)" == appuser ]] || note "the image does not run as appuser"
$DOCKER run --rm --entrypoint python "$IMG" -c \
  'import app.web, app.worker, app.customer_intent, app.card_regen, app.benchmark, app.staff_bench, app.bench_live; print("imports ok")' \
  >/dev/null 2>&1 || note "a module does not import"

if ((${#problems[@]})); then
  echo "STOP: ${#problems[@]} problem(s) in $IMG:" >&2
  printf '  - %s\n' "${problems[@]}" >&2
  exit 1
fi
echo "OK: $IMG holds exactly the tested app/, migrations/ and demo/ trees; imports and user are fine"
