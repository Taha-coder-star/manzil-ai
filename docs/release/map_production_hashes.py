"""Map the hashes printed by the one read-only EC2 command to Git blobs.

    python docs/release/map_production_hashes.py prod-tree.txt [--merge 46c44d8] [--repo .]

prod-tree.txt is the pasted output of the command in docs/RELEASE_FRESH_SEARCH.md section 4a:
a section per container ("== deploy-web-1") with `sha256  path` lines, then its `docker diff`.
For every path it reports whether web and worker agree, whether the hash equals the merge
commit's blob, and otherwise which commit(s) of the repository history introduced exactly that
content. A hash that matches no blob is reported as UNMAPPED: it is never turned into an allowed
state automatically. With --profile it prints the lines for docs/release/production_profile.txt
for the files this release replaces.
"""
import argparse
import hashlib
import pathlib
import re
import subprocess
import sys

RELEASE_FILES = ["app/card_regen.py", "app/cards.py", "app/customer_intent.py", "app/customer_tools.py", "app/demo_media.py", "app/handoff.py", "app/handoff_actions.py", "app/handoff_alert.py", "app/media.py", "app/normalise.py", "app/render.py", "app/requirements.py", "app/search.py", "app/staff_cards.py", "app/turn_engine.py", "app/bench_live.py", "app/benchmark.py", "app/staff_bench.py"]


def git(repo, *args):
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-C", repo, *args],
                          capture_output=True, check=False)


def parse(text):
    """{container: {path: sha256}}, {container: [docker diff lines]}"""
    trees, diffs, cur, in_diff = {}, {}, None, False
    for line in text.splitlines():
        if line.startswith("== "):
            cur, in_diff = line[3:].strip(), False
            trees[cur], diffs[cur] = {}, []
        elif line.startswith("-- docker diff") and cur:
            in_diff = True
        elif cur and in_diff and line.strip():
            diffs[cur].append(line.strip())
        elif cur and (m := re.match(r"^([0-9a-f]{64})\s+\*?(\S+)$", line.strip())):
            trees[cur][m.group(2)] = m.group(1)
    return trees, diffs


def blob_history(repo, path, cache):
    """[(sha256, short commit, subject)] for every distinct content `path` ever had."""
    if path in cache:
        return cache[path]
    out = git(repo, "log", "--all", "--format=%H %h %s", "--", path).stdout.decode().splitlines()
    seen = {}
    for line in out:
        full, short, subject = line.split(" ", 2)
        blob = git(repo, "show", f"{full}:{path}")
        if blob.returncode:
            continue   # the commit deleted the file
        digest = hashlib.sha256(blob.stdout).hexdigest()
        seen.setdefault(digest, []).append((short, subject))
    cache[path] = seen
    return seen


def check_profile(args):
    """Every state of a production profile is ABSENT, a blob that exists in the repository history,
    or marked `diffed` (the owner compared the file by hand). Anything else fails."""
    cache, bad = {}, 0
    for line in pathlib.Path(args.check_profile).read_text(encoding="utf-8").splitlines():
        body, _, note = line.partition("#")
        parts = body.split()
        if not parts:
            continue
        if len(parts) != 2:
            bad += 1
            print(f"  MALFORMED {line!r}")
            continue
        path, state = parts
        if state == "ABSENT":
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", state):
            bad += 1
            print(f"  MALFORMED {path} {state}")
        elif state in blob_history(args.repo, path, cache) or "diffed" in note:
            continue
        else:
            bad += 1
            print(f"  UNEXPLAINED {path} {state[:16]}: no commit has this content and it is not marked 'diffed'")
    return 1 if bad else 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("tree", nargs="?")
    ap.add_argument("--check-profile", metavar="FILE")
    ap.add_argument("--merge", default="46c44d8")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--profile", action="store_true", help="print profile lines (mapped hashes only)")
    args = ap.parse_args(argv)

    if args.check_profile:
        return check_profile(args)
    if not args.tree:
        sys.exit("give the pasted prod-tree.txt, or --check-profile FILE")
    trees, diffs = parse(pathlib.Path(args.tree).read_text(encoding="utf-8"))
    if len(trees) != 2:
        sys.exit(f"expected the web and the worker section, found {sorted(trees)}")
    web, worker = (trees[k] for k in sorted(trees))
    cache, problems = {}, 0

    print("== web vs worker")
    for path in sorted(set(web) | set(worker)):
        if web.get(path) != worker.get(path):
            problems += 1
            print(f"  DIFFERENT {path}: web={web.get(path)} worker={worker.get(path)}")
    print("  identical" if not problems else "")
    for name, lines in diffs.items():
        if lines:
            problems += 1
            print(f"  docker diff of {name}: {lines}")

    print("== files compared with the merge commit", args.merge)
    listing = git(args.repo, "ls-tree", "-r", "--name-only", args.merge, "app", "migrations",
                  "demo").stdout.decode().split()
    profile = []
    for path in sorted(set(listing) | set(web)):
        got = web.get(path, "ABSENT")
        want = hashlib.sha256(git(args.repo, "show", f"{args.merge}:{path}").stdout).hexdigest() \
            if path in listing else None
        if got == want:
            if path in RELEASE_FILES:
                print(f"  = merge   {path}")
                profile.append(f"{path} {got}  # equals {args.merge} (already final)")
            continue
        if want is None:
            problems += 1
            print(f"  EXTRA     {path} {got}  (not in {args.merge})")
            continue
        if got == "ABSENT":
            print(f"  ABSENT    {path}")
            if path in RELEASE_FILES:
                profile.append(f"{path} ABSENT  # not on the server")
            continue
        hits = blob_history(args.repo, path, cache).get(got)
        if hits:
            first = ", ".join(f"{s} {subj[:50]}" for s, subj in hits[-1:])
            print(f"  mapped    {path} {got[:16]} <- {first}")
            if path in RELEASE_FILES:
                profile.append(f"{path} {got}  # {hits[-1][0]} {hits[-1][1][:60]}")
        else:
            problems += 1
            print(f"  UNMAPPED  {path} {got}  (no commit in the history has this content)")
    if args.profile:
        print("\n== lines for docs/release/production_profile.txt")
        print("\n".join(profile))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
