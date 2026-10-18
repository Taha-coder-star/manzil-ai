#!/usr/bin/env bash
# Build the fresh-search release kit (18 runtime files) from committed bytes only.
#
#   bash docs/release/build_kit.sh <M> <outdir> [scripts-ref]
#
# M            the merge commit whose app/ files are released (e.g. 46c44d8)
# outdir       an empty or new directory; the kit and manzil-fresh-<M7>-kit.zip land there
# scripts-ref  where docs/release/*.sh are taken from (default HEAD); they are not in the image
#
# Every git call goes through g(), which pins core.autocrlf=false, so a Windows checkout cannot
# turn LF into CRLF in the archive, the hashes or the scripts. The build stops on any surprise.
set -Eeuo pipefail

g()   { git -c core.autocrlf=false "$@"; }
die() { echo "STOP: $*" >&2; exit 1; }
blob_sha() { g show "$1:$2" | sha256sum | cut -d' ' -f1; }
PY=""; for c in python3 python; do "$c" -c 'import sys' >/dev/null 2>&1 && { PY=$c; break; }; done
[[ -n "$PY" ]] || { echo "STOP: a working python is needed" >&2; exit 1; }

M=$(g rev-parse --verify "${1:?merge commit}^{commit}"); S=${M:0:7}
OUT=${2:?output directory}
SCRIPTS=${3:-HEAD}
BASE=91b7cef      # the production lineage this release was prepared against
LAG_REF=7f5cd3d   # last build whose benchmark tools production still carries (see the runbook)

# 15 files this release changes (vs BASE), and 3 offline benchmark tools that production never
# received (unchanged between BASE and M, but older or absent on the server): all 18 are replaced.
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

g archive --format=tar --prefix=release-fresh/ -o "$OUT/manzil-fresh-$S.tar" "$M" $ALL
for f in $ALL; do printf '%s  %s\n' "$(blob_sha "$M" "$f")" "$f"; done > "$OUT/SHA256SUMS"

# the other app modules, as released
for f in $(g ls-tree -r --name-only "$M" app/ | grep '\.py$'); do
  case " $ALL " in *" $f "*) ;; *) printf '%s  %s\n' "$(blob_sha "$M" "$f")" "$f";; esac
done > "$OUT/SHA256SUMS.unchanged"

# replaced modules that production holds at the BASE version (turn_engine has several states; new
# and benchmark files are listed in STATES.allowed instead)
for f in $CHANGED; do
  case $f in app/turn_engine.py|app/card_regen.py|app/customer_intent.py) ;;
    *) printf '%s  %s\n' "$(blob_sha "$BASE" "$f")" "$f";; esac
done > "$OUT/SHA256SUMS.base"

# data the image carries and this release does not touch; 001-004 are explained in STATES.allowed
for f in $(g ls-tree -r --name-only "$M" migrations demo); do
  case $f in migrations/00[1-4]_*) ;; *) printf '%s  %s\n' "$(blob_sha "$M" "$f")" "$f";; esac
done > "$OUT/SHA256SUMS.data"

g ls-tree -r --name-only "$BASE" app migrations demo | LC_ALL=C sort > "$OUT/FILES.expected"
g ls-tree -r --name-only "$M" app migrations demo | LC_ALL=C sort > "$OUT/FILES.release"

{
  # turn_engine.py: 91b7cef; + hotfix 608f4d6; + hotfix + the greeting line (f4cc48d's change)
  echo "app/turn_engine.py $(blob_sha "$BASE" app/turn_engine.py)"
  echo "app/turn_engine.py $(blob_sha 608f4d6 app/turn_engine.py)"
  echo "app/turn_engine.py 7ab72a7b0916b33cb0f049dd95dc6718833ac3f2f7c12e274f5d7764f54e1d00"
  # benchmark tools: the versions of $LAG_REF (never shipped since), or absent
  echo "app/benchmark.py $(blob_sha "$LAG_REF" app/benchmark.py)"
  echo "app/staff_bench.py $(blob_sha "$LAG_REF" app/staff_bench.py)"
  echo "app/bench_live.py ABSENT"
  # migrations 001-004: the Git (LF) blob, or the CRLF working-copy form the image may hold
  for f in $(g ls-tree -r --name-only "$M" migrations | grep -E 'migrations/00[1-4]_'); do
    echo "$f $(blob_sha "$M" "$f")"
    echo "$f $(g show "$M:$f" | "$PY" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read().replace(b"\n", b"\r\n")).hexdigest())')"
  done
} > "$OUT/STATES.allowed"

for f in fresh_search_baseline.sh fresh_search_image_check.sh fresh_search_switch.sh; do
  g show "$SCRIPTS:docs/release/$f" > "$OUT/$f"
done
# FROM takes a tag, not an image id: the runbook tags the recorded id as base-<S> and checks it
printf 'FROM manzil-ai:base-%s\nCOPY release-fresh/app/ /app/app/\n' "$S" > "$OUT/Dockerfile.release"
( cd "$OUT" && sha256sum "manzil-fresh-$S.tar" > "manzil-fresh-$S.tar.sha256" )
( cd "$OUT" && sha256sum -- * > KIT.tmp && mv KIT.tmp KIT.sha256 )

# --- self-checks: stop on any surprise --------------------------------------------------------
"$PY" - "$OUT" <<'PY'
import pathlib, sys
bad = [p.name for p in pathlib.Path(sys.argv[1]).iterdir()
       if p.suffix != ".tar" and b"\r" in p.read_bytes()]
sys.exit("CR bytes in: " + ", ".join(bad) if bad else 0)
PY
entries=$(tar -tf "$OUT/manzil-fresh-$S.tar" | grep -v '/$' | sed 's#^release-fresh/##' | sort | tr '\n' ' ')
[[ "$entries" == "$(printf '%s\n' $ALL | sort | tr '\n' ' ')" ]] || die "the archive does not hold exactly the 18 files"
tmp=$(mktemp -d); tar -xf "$OUT/manzil-fresh-$S.tar" -C "$tmp"
( cd "$tmp/release-fresh" && sha256sum -c "$OUT/SHA256SUMS" >/dev/null ) || die "archive bytes differ from the Git blobs"
rm -rf "$tmp"
[[ "$(wc -l < "$OUT/SHA256SUMS")" == 18 ]] || die "SHA256SUMS is not 18 lines"

# --- the ZIP (flat; scripts are run with bash, so no mode bits are needed) ----------------------
"$PY" - "$OUT" "$S" <<'PY'
import pathlib, sys, zipfile
out, s = pathlib.Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(out / f"manzil-fresh-{s}-kit.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(out.iterdir()):
        if p.suffix != ".zip":
            z.write(p, p.name)
PY
echo "kit for $M ($S) in $OUT:"; ls -1 "$OUT"
