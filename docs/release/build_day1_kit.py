"""Package a merged, app-only Day 1 release from Git blobs, never working-copy bytes.

python docs/release/build_day1_kit.py --base 9331869 --target <merged-commit> --out <new-dir>
The owner must first verify production equals base (historical migration line endings allowed).
The existing baseline/image/switch gates are retained; no deployment is performed here.
"""
import argparse
import hashlib
import subprocess
from pathlib import Path


def git(*args):
    return subprocess.check_output(["git", "-c", "core.autocrlf=false", *args])


def files(ref, *roots):
    return git("ls-tree", "-r", "--name-only", ref, *roots).decode().splitlines()


def digest(ref, path):
    return hashlib.sha256(git("show", f"{ref}:{path}")).hexdigest()


def build(base, target, out):
    base = git("rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()
    target = git("rev-parse", "--verify", f"{target}^{{commit}}").decode().strip()
    git("merge-base", "--is-ancestor", base, target)
    git("merge-base", "--is-ancestor", target, "origin/main")
    changed = git("diff", "--name-only", base, target, "--", "app", "migrations", "demo",
                  "requirements.txt", "Dockerfile", "deploy", ".dockerignore").decode().splitlines()
    if not changed or any(not p.startswith("app/") or not p.endswith(".py") for p in changed):
        raise ValueError("only a nonempty app-only Python release is supported; no migration/data/deploy changes")
    base_files = set(files(base, "app", "migrations", "demo"))
    target_files = set(files(target, "app", "migrations", "demo"))
    if base_files - target_files:
        raise ValueError("layered release cannot remove files")
    if out.exists():
        raise ValueError("output must be a new directory")
    scripts = {name: git("show", f"HEAD:docs/release/{name}") for name in
               ("fresh_search_baseline.sh", "fresh_search_image_check.sh", "fresh_search_switch.sh")}
    archive = git("archive", "--format=tar", "--prefix=release-fresh/", target, *changed)
    def manifest(paths):
        return "".join(f"{digest(target, p)}  {p}\n" for p in sorted(paths))
    special = {p for p in target_files if any(p.startswith(f"migrations/{n:03}_") for n in range(1, 5))}
    states = [f"{p} {digest(base, p) if p in base_files else 'ABSENT'}" for p in changed]
    for p in sorted(special):
        b = git("show", f"{base}:{p}")
        states.extend(f"{p} {hashlib.sha256(v).hexdigest()}" for v in (b, b.replace(b"\n", b"\r\n")))
    short = target[:7]
    out.mkdir(parents=True)
    def write(name, text):
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    (out / f"manzil-fresh-{short}.tar").write_bytes(archive)
    write("SHA256SUMS", manifest(changed))
    write("SHA256SUMS.unchanged", manifest(p for p in target_files if p.startswith("app/") and p not in changed))
    write("SHA256SUMS.data", manifest(p for p in target_files if not p.startswith("app/") and p not in special))
    for name in ("FILES.expected", "FILES.release"):
        write(name, "\n".join(sorted(target_files)) + "\n")
    write("STATES.allowed", "\n".join(states) + "\n")
    write("Dockerfile.release", f"FROM manzil-ai:base-{short}\nCOPY release-fresh/app/ /app/app/\n")
    write("COMMITS.txt", f"base={base}\ntarget={target}\n")
    for name, body in scripts.items():
        (out / name).write_bytes(body)
    write("KIT.sha256", "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                               for p in sorted(out.iterdir())))
    return short


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    print(build(args.base, args.target, args.out))


if __name__ == "__main__":
    main()
