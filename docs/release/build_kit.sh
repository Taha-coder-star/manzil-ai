#!/usr/bin/env bash
# Build the fresh-search release kit (the 18-file runtime archive) from committed bytes only.
#
#   bash docs/release/build_kit.sh <M> <outdir> [scripts-ref [profile-file]]
#
# M             the merge commit whose app/ files are released (e.g. 46c44d8)
# outdir        an empty or new directory; the kit and manzil-fresh-<M7>-kit.zip land there
# scripts-ref   where docs/release/* is taken from (default HEAD); the scripts are not in the image
# profile-file  the verified production combination (default: docs/release/production_profile.txt
#               at scripts-ref): per released file the sha256 values production may hold, or ABSENT
#
# The archive is the same 18 files whatever the profile says; the profile only decides which
# production states the baseline gate accepts. Every state must be a blob that exists in the
# repository history (mapped) or carry the word "diffed" (a hand comparison by the owner); the
# build stops if a released file has no state, so an unverified combination cannot ship.
# Every git call goes through g(), which pins core.autocrlf=false.
set -Eeuo pipefail

g()   { git -c core.autocrlf=false "$@"; }
die() { echo "STOP: $*" >&2; exit 1; }
blob_sha() { g show "$1:$2" | sha256sum | cut -d' ' -f1; }
PY=""; for c in python3 python; do "$c" -c 'import sys' >/dev/null 2>&1 && { PY=$c; break; }; done
[[ -n "$PY" ]] || die "a working python is needed"
# a native Windows python cannot open MSYS paths such as /tmp/x: hand it Windows paths
wp() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

M=$(g rev-parse --verify "${1:?merge commit}^{commit}"); S=${M:0:7}
OUT=${2:?output directory}
SCRIPTS=${3:-HEAD}
PROFILE=${4:-}
BASE=91b7cef      # the production lineage this release was prepared against

# 15 files this release changes (vs BASE) and 3 offline benchmark tools that production never
# received (unchanged between BASE and M): all 18 are replaced.
CHANGED="app/card_regen.py app/cards.py app/customer_intent.py app/customer_tools.py app/demo_media.py
 app/handoff.py app/handoff_actions.py app/handoff_alert.py app/media.py app/normalise.py
 app/render.py app/requirements.py app/search.py app/staff_cards.py app/turn_engine.py"
LAG="app/bench_live.py app/benchmark.py app/staff_bench.py"
CHANGED=$(echo $CHANGED)   # one line, single spaces: the case patterns below match " file "
ALL="$CHANGED $LAG"

want=$(printf '%s\n' $CHANGED | sort | tr '\n' ' ')
got=$(g diff --name-only "$BASE" "$M" -- app migrations requirements.txt Dockerfile demo deploy .dockerignore | sort | tr '\n' ' ')
[[ "$got" == "$want" ]] || die "files changed between $BASE and $M are not exactly the expected 15: $got"
for f in $LAG; do [[ "$(blob_sha "$BASE" "$f")" == "$(blob_sha "$M" "$f")" ]] || die "$f differs between $BASE and $M"; done

[[ ! -e "$OUT" || -z "$(ls -A "$OUT")" ]] || die "$OUT is not empty"
mkdir -p "$OUT"
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT

# --- the verified production combination ------------------------------------------------------
if [[ -z "$PROFILE" ]]; then
  g show "$SCRIPTS:docs/release/production_profile.txt" > "$work/profile.txt" \
    || die "no production_profile.txt at $SCRIPTS"
  PROFILE=$work/profile.txt
fi
g show "$SCRIPTS:docs/release/map_production_hashes.py" > "$work/map.py"
states=$(grep -vE '^\s*(#|$)' "$PROFILE" | sed -E 's/\s+#.*$//; s/\s+$//' | awk '{print $1" "$2}')
for f in $ALL; do
  grep -q "^$f " <<<"$states" || die "the production profile has no state for $f: the combination is not verified yet"
done
while read -r path _; do case " $ALL " in *" $path "*) ;; *) die "the profile names $path, which is not a released file";; esac; done <<<"$states"
"$PY" "$(wp "$work/map.py")" --check-profile "$(wp "$PROFILE")" --repo . || die "the profile has a state that is neither a repository blob nor marked 'diffed'"

g archive --format=tar --prefix=release-fresh/ -o "$OUT/manzil-fresh-$S.tar" "$M" $ALL
for f in $ALL; do printf '%s  %s\n' "$(blob_sha "$M" "$f")" "$f"; done > "$OUT/SHA256SUMS"

# the other app modules, as released (production holds them unchanged)
for f in $(g ls-tree -r --name-only "$M" app/ | grep '\.py$'); do
  case " $ALL " in *" $f "*) ;; *) printf '%s  %s\n' "$(blob_sha "$M" "$f")" "$f";; esac
done > "$OUT/SHA256SUMS.unchanged"

# data the image carries and this release does not touch; 001-004 are explained in STATES.allowed
for f in $(g ls-tree -r --name-only "$M" migrations demo); do
  case $f in migrations/00[1-4]_*) ;; *) printf '%s  %s\n' "$(blob_sha "$M" "$f")" "$f";; esac
done > "$OUT/SHA256SUMS.data"

# the merge commit's file list: production may lack only the files whose state is ABSENT
g ls-tree -r --name-only "$M" app migrations demo | LC_ALL=C sort > "$OUT/FILES.release"
cp "$OUT/FILES.release" "$OUT/FILES.expected"

{
  echo "$states"
  # migrations 001-004: the Git (LF) blob, or the CRLF working-copy form the image may hold
  for f in $(g ls-tree -r --name-only "$M" migrations | grep -E 'migrations/00[1-4]_'); do
    echo "$f $(blob_sha "$M" "$f")"
    echo "$f $(g show "$M:$f" | "$PY" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read().replace(b"\n", b"\r\n")).hexdigest())')"
  done
} > "$OUT/STATES.allowed"
cp "$PROFILE" "$OUT/production_profile.txt"

for f in fresh_search_baseline.sh fresh_search_image_check.sh fresh_search_switch.sh; do
  g show "$SCRIPTS:docs/release/$f" > "$OUT/$f"
done
# FROM takes a tag, not an image id: the runbook tags the recorded id as base-<S> and checks it
printf 'FROM manzil-ai:base-%s\nCOPY release-fresh/app/ /app/app/\n' "$S" > "$OUT/Dockerfile.release"
( cd "$OUT" && sha256sum "manzil-fresh-$S.tar" > "manzil-fresh-$S.tar.sha256" )
( cd "$OUT" && sha256sum -- * > KIT.tmp && mv KIT.tmp KIT.sha256 )

# --- self-checks: stop on any surprise --------------------------------------------------------
"$PY" - "$(wp "$OUT")" <<'PY'
import pathlib, sys
bad = [p.name for p in pathlib.Path(sys.argv[1]).iterdir()
       if p.suffix != ".tar" and b"\r" in p.read_bytes()]
sys.exit("CR bytes in: " + ", ".join(bad) if bad else 0)
PY
entries=$(tar -tf "$OUT/manzil-fresh-$S.tar" | grep -v '/$' | sed 's#^release-fresh/##' | sort | tr '\n' ' ')
[[ "$entries" == "$(printf '%s\n' $ALL | sort | tr '\n' ' ')" ]] || die "the archive does not hold exactly the 18 files"
mkdir "$work/x"; tar -xf "$OUT/manzil-fresh-$S.tar" -C "$work/x"
( cd "$work/x/release-fresh" && sha256sum -c "$OUT/SHA256SUMS" >/dev/null ) || die "archive bytes differ from the Git blobs"
[[ "$(wc -l < "$OUT/SHA256SUMS")" == 18 ]] || die "SHA256SUMS is not 18 lines"
[[ "$(( $(wc -l < "$OUT/SHA256SUMS.unchanged") + 18 ))" == "$(g ls-tree -r --name-only "$M" app | grep -c '\.py$')" ]] \
  || die "the manifests do not cover every app module exactly once"

# --- the ZIP (flat; scripts are run with bash, so no mode bits are needed) ----------------------
"$PY" - "$(wp "$OUT")" "$S" <<'PY'
import pathlib, sys, zipfile
out, s = pathlib.Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(out / f"manzil-fresh-{s}-kit.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(out.iterdir()):
        if p.suffix != ".zip":
            z.write(p, p.name)
PY
echo "kit for $M ($S) in $OUT:"; ls -1 "$OUT"
