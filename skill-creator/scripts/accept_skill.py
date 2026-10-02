#!/usr/bin/env python3
"""Accept a staged skill update, or propagate a vault skill to Claude Code, without
clobbering runtime state. See references/updating.md.

Usage (cwd = vault root):
  .venv/bin/python3 .codex/skills/skill-creator/scripts/accept_skill.py --run <R> --key <key> --live <skill dir> [--dry-run]
  .venv/bin/python3 .codex/skills/skill-creator/scripts/accept_skill.py --propagate <vault skill dir> [--dest-root ~/.claude/skills] [--dry-run] [--overwrite-dest]
  .venv/bin/python3 .codex/skills/skill-creator/scripts/accept_skill.py --state-paths <skill dir>
  .venv/bin/python3 .codex/skills/skill-creator/scripts/accept_skill.py --self-test

Exit codes: 0 ok, 2 usage/missing input, 3 busy (lock held), 4 live diverged from baseline,
5 post-copy verification mismatch, 6 destination is a symlink (refused), 7 destination has changes
not made by this script (refused unless --overwrite-dest).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required. Run with the repo virtualenv: .venv/bin/python3 ...") from exc

ALWAYS_EXCLUDED = ("__pycache__", ".DS_Store")  # caches and Finder noise, never content
RECORD_VERSION = 2  # bump when tree_entries/tree_hash change; older sync hashes then count as foreign


def vault_root() -> Path:
    if os.environ.get("KB_ROOT"):
        return Path(os.environ["KB_ROOT"]).expanduser().resolve()
    here = Path(__file__).resolve()
    if here.parents[2].name == "skills" and here.parents[3].name == ".codex":
        return here.parents[4]
    return Path("~/Documents/AI_Workspace/obsidian/knowledge_base").expanduser()


def state_paths(skill_dir: Path, identity: Path | None = None) -> set[str]:
    """Runtime-state paths a skill declares inside its own dir (security.write_paths entries
    under .codex/skills/<name>/), relative to the skill dir. Missing/invalid manifest -> empty."""
    manifest = skill_dir / "skill.yaml"
    if not manifest.is_file():
        return set()
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, ValueError):
        return set()
    paths = ((data.get("security") or {}).get("write_paths") or []) if isinstance(data, dict) else []
    # A declared path is runtime state of this skill when it lands inside the skill dir, written
    # either root-relative (".codex/skills/<name>/x", ".agents/skills/<name>/x" — matched on
    # "<parent dir name>/<name>/") or absolute / home-relative.
    # The vault convention ".codex/skills/<name>/" is always recognised, because mirrors (e.g.
    # ~/.claude/skills/<name>) carry manifests written for the vault location.
    # `identity` = the canonical live dir when reading a baseline/staging copy, so declarations
    # are interpreted for the real target, not for the copy's physical location.
    ident = identity or skill_dir
    markers = {f".codex/skills/{ident.name}/", f"{ident.parent.name}/{ident.name}/"}
    real_dir = str(ident.resolve())
    out: set[str] = set()
    for entry in paths:
        if not isinstance(entry, str):
            continue
        entry = os.path.expanduser(entry.strip()).removeprefix("./")
        hit = next((m for m in markers if not entry.startswith("/")
                    and (entry.startswith(m) or f"/{m}" in entry)), None)
        if entry.startswith("/"):  # absolute: compare after resolving symlinks (/var -> /private/var)
            rel = os.path.relpath(os.path.realpath(entry), real_dir)
            if rel.startswith(".."):
                continue
        elif hit:
            rel = entry.split(hit, 1)[1]
        else:
            continue
        rel = os.path.normpath(rel)
        if rel not in (".", "") and not rel.startswith(".."):  # never protect the whole dir
            out.add(rel)
    return out


def accepted_protect(skill_dir: Path, vault: Path) -> set[str]:
    """Protection recorded by earlier acceptances of this skill (keyed by canonical path)."""
    key = hashlib.sha256(str(skill_dir.resolve()).encode()).hexdigest()[:8]
    record = vault / "outputs" / "skill-evals" / "accepted" / f"{key}.json"
    return set(json.loads(record.read_text()).get("protect", [])) if record.is_file() else set()


def effective_protect(skill_dir: Path, vault: Path, explicit: set[str] = frozenset()) -> set[str]:
    """Everything that must never be overwritten in this skill dir: declared state, earlier
    accepted protection, and explicit --protect paths."""
    return state_paths(skill_dir) | accepted_protect(skill_dir, vault) | {os.path.normpath(e) for e in explicit}


def excluded(rel: str, protect: set[str]) -> bool:
    parts = rel.split("/")
    if any(p in ALWAYS_EXCLUDED for p in parts):
        return True
    return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in protect)


def tree_entries(root: Path, protect: set[str]) -> dict[str, tuple]:
    """lstat view of a tree: files (exec bits + content hash) and symlinks (target).
    Protected/excluded paths are skipped and not descended into. Directories are entries (with
    their permission bits) only when they hold unprotected content, so a dir that exists only to
    hold protected state never counts as a difference, while a permission change on a content dir
    does. The root's own mode is not compared (copies are created by mkdir)."""
    out: dict[str, tuple] = {}
    if not root.exists():
        return out
    for dirpath, dirs, names in os.walk(root):  # does not follow symlinked dirs
        kept = []
        for n in sorted(dirs) + sorted(names):
            p = Path(dirpath) / n
            rel = os.path.relpath(p, root)
            if excluded(rel, protect):
                continue
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                kept.append(n)
            else:
                out[rel] = ("file", st.st_mode & 0o7777, hashlib.sha256(p.read_bytes()).hexdigest())
        dirs[:] = [d for d in dirs if d in kept]
    for rel in list(out):
        parent = os.path.dirname(rel)
        while parent and parent not in out:
            out[parent] = ("dir", os.lstat(root / parent).st_mode & 0o7777)
            parent = os.path.dirname(parent)
    return out


def tree_diff(a: Path, b: Path, protect: set[str]) -> list[str]:
    """Paths whose type, exec bits, symlink target or content differ (protected paths ignored)."""
    ea, eb = tree_entries(a, protect), tree_entries(b, protect)
    return sorted(k for k in ea.keys() | eb.keys() if ea.get(k) != eb.get(k))


def tree_hash(root: Path, protect: set[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(tree_entries(root, protect).items())).encode()).hexdigest()


def outward_links(root: Path, protect: set[str] = frozenset()) -> list[str]:
    """Symlinks under root (outside protected paths) that are absolute or resolve outside root,
    dangling ones included. Edits through such links would change files outside the copy."""
    real_root = os.path.realpath(root)
    bad = []
    for dirpath, dirs, names in os.walk(root):
        for n in dirs + names:
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root)
            if os.path.islink(p) and not excluded(rel, protect):
                target = os.readlink(p)
                resolved = os.path.realpath(p)
                if os.path.isabs(target) or not resolved.startswith(real_root + os.sep):
                    bad.append(f"{rel} -> {target}")
        dirs[:] = [d for d in dirs if not excluded(os.path.relpath(os.path.join(dirpath, d), root), protect)]
    return sorted(bad)


def rsync(src: Path, dest: Path, protect: set[str], dry_run: bool) -> int:
    # --checksum: the default size+mtime quick check skips same-size edits made within one second
    cmd = ["rsync", "-a", "--checksum", "--delete"]
    cmd += [f"--exclude={p}" for p in ALWAYS_EXCLUDED]
    # anchored; also shields them from --delete. Escape wildcard chars so "state[1].json" stays literal.
    cmd += ["--exclude=/" + re.sub(r"([*?\[\]\\])", r"\\\1", p) for p in sorted(protect)]
    if dry_run:
        cmd += ["--dry-run", "--itemize-changes"]
    cmd += [f"{src}/", f"{dest}/"]
    return subprocess.run(cmd).returncode


def single_child(d: Path) -> Path | None:
    kids = [k for k in d.iterdir() if k.is_dir()] if d.is_dir() else []
    return kids[0] if len(kids) == 1 else None


def accept(run: Path, key: str, live: Path, dry_run: bool, vault: Path,
           explicit: set[str] = frozenset()) -> int:
    live = live.resolve()
    baseline, staging = single_child(run / "baseline" / key), single_child(run / "staging" / key)
    if not (live.is_dir() and baseline and staging):
        print(f"error: need live dir and exactly one baseline/staging dir under {run}/*/{key}", file=sys.stderr)
        return 2
    if not (baseline.name == staging.name == live.name):
        print(f"error: name mismatch live={live.name} baseline={baseline.name} staging={staging.name}", file=sys.stderr)
        return 2
    # Protection accepted earlier for this path persists even if a later update stops declaring it;
    # retiring a state path is a separate, explicit decision (delete the record entry by hand).
    record = vault / "outputs" / "skill-evals" / "accepted" / f"{key}.json"
    rec_protect = set(json.loads(record.read_text()).get("protect", [])) if record.is_file() else set()
    protect = (effective_protect(live, vault, explicit) | state_paths(baseline, live) | state_paths(staging, live)
               | rec_protect)
    locks = vault / "outputs" / "skill-evals" / "locks"
    try:
        locks.mkdir(parents=True, exist_ok=True)
        lock = open(locks / f"{key}.lock", "w")
    except OSError as exc:
        print(f"error: cannot create lock under {locks}: {exc}", file=sys.stderr)
        return 2
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released when the process exits
        except BlockingIOError:
            print(f"busy: another process is accepting {key}", file=sys.stderr)
            return 3
        drift = tree_diff(live, baseline, protect)
        if drift:
            print(f"diverged: {live} changed since staging; re-stage and re-review. Paths: {drift[:10]}", file=sys.stderr)
            return 4
        print(f"protected runtime state: {sorted(protect) or 'none'}")
        if not dry_run:  # record first: a later update must never lose this protection
            try:
                record.parent.mkdir(parents=True, exist_ok=True)
                tmp = record.with_suffix(".json.tmp")
                tmp.write_text(json.dumps({"v": RECORD_VERSION, "live": str(live), "protect": sorted(protect)}) + "\n")
                os.replace(tmp, record)
            except OSError as exc:
                print(f"error: cannot record protection at {record}: {exc}; nothing copied", file=sys.stderr)
                return 2
        if rsync(staging, live, protect, dry_run) != 0:
            return 5
        if dry_run:
            return 0
        mismatch = tree_diff(staging, live, protect)
        if mismatch:
            print(f"verify failed: {mismatch[:10]}", file=sys.stderr)
            return 5
    print(f"accepted: {live}")
    return 0


def propagate(src: Path, dest_root: Path, dry_run: bool, vault: Path, overwrite_dest: bool = False,
              explicit: set[str] = frozenset()) -> int:
    src = src.resolve()
    if not (src / "SKILL.md").is_file():
        print(f"error: {src} has no SKILL.md", file=sys.stderr)
        return 2
    dest = dest_root.expanduser() / src.name
    if dest.is_symlink():
        print(f"refused: {dest} is a symlink (maintained elsewhere)", file=sys.stderr)
        return 6
    # The destination is a mirror. A record of our last sync (in the vault) holds the protected set
    # and the destination hash we left behind, so (a) state paths stay protected even after an update
    # stops declaring them, and (b) we refuse when the destination changed outside this script.
    record = vault / "outputs" / "skill-evals" / "propagated" / (
        hashlib.sha256(str(dest.resolve()).encode()).hexdigest()[:8] + ".json")
    rec = json.loads(record.read_text()) if record.is_file() else {}
    rec_protect = set(rec.get("protect", []))
    protect = effective_protect(src, vault, explicit) | state_paths(dest) | rec_protect
    print(f"protected runtime state: {sorted(protect) or 'none'}")
    if dest.exists() and tree_diff(src, dest, protect):
        foreign = (not rec or rec.get("v") != RECORD_VERSION
                   or rec.get("hash") != tree_hash(dest, rec_protect))
        if foreign and not overwrite_dest:
            print(f"refused: {dest} has changes not made by a previous propagation "
                  f"({'modified since last sync' if rec else 'never synced by this script'}). "
                  f"Differing paths: {tree_diff(src, dest, protect)[:10]}. Reconcile them into the vault "
                  f"copy first, or rerun with --overwrite-dest after the user confirms.", file=sys.stderr)
            return 7
    if dry_run and not dest.exists():
        print(f"would create {dest} with {sum(e[0] == 'file' for e in tree_entries(src, protect).values())} file(s)")
        return 0
    def save(hash_: str | None) -> None:
        record.parent.mkdir(parents=True, exist_ok=True)
        tmp = record.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"v": RECORD_VERSION, "dest": str(dest), "protect": sorted(protect),
                                   "hash": hash_}) + "\n")
        os.replace(tmp, record)

    if not dry_run:
        try:  # protection first, keeping the previous sync hash until this copy is verified
            save(rec.get("hash") if rec.get("v") == RECORD_VERSION else None)
        except OSError as exc:
            print(f"error: cannot record protection at {record}: {exc}; nothing copied", file=sys.stderr)
            return 2
        dest.mkdir(parents=True, exist_ok=True)
    if rsync(src, dest, protect, dry_run) != 0:
        return 5
    if dry_run:
        return 0
    mismatch = tree_diff(src, dest, protect)
    if mismatch:
        print(f"verify failed: {mismatch[:10]}", file=sys.stderr)
        return 5
    save(tree_hash(dest, protect))
    print(f"propagated: {src} -> {dest}")
    return 0


def self_test() -> None:
    def write(p: Path, s: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(s, encoding="utf-8")

    def manifest(state: str) -> str:
        return f"name: demo\nsecurity:\n  write_paths: ['.codex/skills/demo/{state}', 'outputs/']\n"

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        vault, run = t / "vault", t / "run"
        live = vault / ".codex" / "skills" / "demo"
        write(live / "SKILL.md", "old\n")
        write(live / "skill.yaml", manifest("last_run.json"))
        write(live / "last_run.json", "LIVE")
        for kind in ("baseline", "staging"):
            dst = run / kind / "k1" / "demo"
            subprocess.run(["cp", "-R", f"{live}/.", str(dst) + "/"] if dst.mkdir(parents=True) is None else [])
        write(run / "staging" / "k1" / "demo" / "SKILL.md", "new\n")
        write(run / "staging" / "k1" / "demo" / "last_run.json", "STALE")

        assert state_paths(live) == {"last_run.json"}
        assert not (vault / "outputs").exists()
        # accept: content updated, runtime state preserved, locks/ parent created
        assert accept(run, "k1", live, False, vault) == 0
        assert (live / "SKILL.md").read_text() == "new\n"
        assert (live / "last_run.json").read_text() == "LIVE"
        assert (vault / "outputs" / "skill-evals" / "locks").is_dir()

        # diverged: live changed after baseline -> exit 4, nothing written
        write(live / "SKILL.md", "edited elsewhere\n")
        assert accept(run, "k1", live, False, vault) == 4
        assert (live / "SKILL.md").read_text() == "edited elsewhere\n"

        # busy: another process holds the lock -> exit 3
        write(live / "SKILL.md", "old\n")  # back to baseline content
        lockfile = vault / "outputs" / "skill-evals" / "locks" / "k1.lock"
        holder = subprocess.Popen([sys.executable, "-c",
            "import fcntl,sys,time;f=open(sys.argv[1],'w');fcntl.flock(f,fcntl.LOCK_EX);print('held',flush=True);time.sleep(30)",
            str(lockfile)], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "held"
            assert accept(run, "k1", live, False, vault) == 3
        finally:
            holder.kill()
            holder.wait()

        # propagate: update renamed its state path; destination still uses last_run.json
        src = vault / ".codex" / "skills" / "demo"
        write(src / "skill.yaml", manifest("state/new.json"))
        write(src / "last_run.json", "SRC-STALE")
        dest_root = t / "claude-skills"
        write(dest_root / "demo" / "skill.yaml", manifest("last_run.json"))
        write(dest_root / "demo" / "last_run.json", "DEST")
        write(dest_root / "demo" / "obsolete.md", "gone after sync\n")
        # first sync into a pre-existing, differing destination is refused unless confirmed
        assert propagate(src, dest_root, False, vault) == 7
        assert (dest_root / "demo" / "obsolete.md").exists()
        assert propagate(src, dest_root, False, vault, overwrite_dest=True) == 0
        assert (dest_root / "demo" / "last_run.json").read_text() == "DEST"
        assert not (dest_root / "demo" / "obsolete.md").exists()
        assert (dest_root / "demo" / "SKILL.md").read_text() == "old\n"

        # destination that is a symlink (maintained elsewhere) is refused, nothing written
        other = vault / ".codex" / "skills" / "other"
        write(other / "SKILL.md", "other\n")
        elsewhere = t / "elsewhere"
        elsewhere.mkdir()
        (dest_root / "other").symlink_to(elsewhere)
        assert propagate(other, dest_root, False, vault) == 6

        # after a recorded sync: unchanged destination syncs freely; out-of-band edit is refused
        write(src / "SKILL.md", "newer in vault\n")
        assert propagate(src, dest_root, False, vault) == 0
        write(dest_root / "demo" / "SKILL.md", "edited in ~/.claude\n")
        write(src / "SKILL.md", "vault again\n")
        assert propagate(src, dest_root, False, vault) == 7
        assert (dest_root / "demo" / "SKILL.md").read_text() == "edited in ~/.claude\n"
        # runtime-state-only differences never block
        assert propagate(src, dest_root, False, vault, overwrite_dest=True) == 0
        write(dest_root / "demo" / "last_run.json", "DEST2")
        assert propagate(src, dest_root, False, vault) == 0
        assert not any(elsewhere.iterdir())

        # dry-run into a missing destination previews without creating it
        assert propagate(other, t / "nowhere", True, vault) == 0
        assert not (t / "nowhere").exists()

        # a retargeted symlink in the destination counts as a foreign change
        (src / "link").symlink_to("SKILL.md")
        assert propagate(src, dest_root, False, vault) == 0
        (dest_root / "demo" / "link").unlink()
        (dest_root / "demo" / "link").symlink_to("skill.yaml")
        assert propagate(src, dest_root, False, vault) == 7

        def stage(run_dir: Path, key: str, skill: Path) -> Path:
            for kind in ("baseline", "staging"):
                dst = run_dir / kind / key / skill.name
                dst.mkdir(parents=True)
                subprocess.run(["cp", "-R", f"{skill}/.", f"{dst}/"], check=True)
            return run_dir / "staging" / key / skill.name

        # exec-bit change on the live copy after staging is drift
        live2 = vault / ".codex" / "skills" / "demo2"
        write(live2 / "SKILL.md", "v1\n")
        write(live2 / "run.sh", "echo hi\n")
        write(live2 / "skill.yaml", manifest("last_run.json").replace("demo", "demo2"))
        write(live2 / "last_run.json", "STATE")
        st = stage(t / "run2", "k2", live2)
        os.chmod(live2 / "run.sh", 0o755)
        assert accept(t / "run2", "k2", live2, False, vault) == 4
        os.chmod(live2 / "run.sh", 0o644)
        assert accept(t / "run2", "k2", live2, False, vault) == 0  # records protect {last_run.json}

        # later updates that stop declaring the state path still preserve it
        st = stage(t / "run3", "k2", live2)
        write(st / "skill.yaml", "name: demo2\nsecurity:\n  write_paths: ['outputs/']\n")
        write(st / "last_run.json", "STAGED-STALE")
        assert accept(t / "run3", "k2", live2, False, vault) == 0
        assert (live2 / "last_run.json").read_text() == "STATE"
        st = stage(t / "run4", "k2", live2)  # no manifest declares it any more
        (st / "last_run.json").unlink()
        write(st / "SKILL.md", "v2\n")
        assert accept(t / "run4", "k2", live2, False, vault) == 0
        assert (live2 / "last_run.json").read_text() == "STATE" and (live2 / "SKILL.md").read_text() == "v2\n"

        # state paths written for other roots or as absolute paths are recognised
        other_root = t / "proj" / ".agents" / "skills" / "agt"
        write(other_root / "skill.yaml", "name: agt\nsecurity:\n  write_paths: ['.agents/skills/agt/state.json', "
              f"'{other_root}/cache/', '~/elsewhere/x', 'outputs/']\n")
        assert state_paths(other_root) == {"state.json", "cache"}, state_paths(other_root)

        # propagation honours protection recorded at acceptance, even with --overwrite-dest,
        # after every manifest stopped declaring the path; explicit --protect also works
        src3 = vault / ".codex" / "skills" / "demo3"
        write(src3 / "SKILL.md", "s\n")
        write(src3 / "skill.yaml", "name: demo3\nsecurity:\n  write_paths: ['outputs/']\n")
        key3 = hashlib.sha256(str(src3.resolve()).encode()).hexdigest()[:8]
        write(vault / "outputs" / "skill-evals" / "accepted" / f"{key3}.json", json.dumps({"protect": ["last_run.json"]}))
        write(dest_root / "demo3" / "SKILL.md", "old\n")
        write(dest_root / "demo3" / "last_run.json", "LIVE3")
        write(dest_root / "demo3" / "notes.txt", "keep me\n")
        assert propagate(src3, dest_root, False, vault, overwrite_dest=True, explicit={"notes.txt"}) == 0
        assert (dest_root / "demo3" / "last_run.json").read_text() == "LIVE3"
        assert (dest_root / "demo3" / "notes.txt").read_text() == "keep me\n"
        assert (dest_root / "demo3" / "SKILL.md").read_text() == "s\n"

        # a candidate that newly declares an absolute live path protects it, though the staging copy
        # lacks the file; a wildcard-looking literal name (state[1].json) is protected too
        live5 = vault / ".codex" / "skills" / "demo5"
        write(live5 / "SKILL.md", "a\n")
        write(live5 / "skill.yaml", "name: demo5\nsecurity:\n  write_paths: ['outputs/']\n")
        write(live5 / "cache.json", "LIVE-CACHE")
        write(live5 / "state[1].json", "LIVE-S1")
        write(live5 / "state1.json", "content, replaced by staging\n")
        st = stage(t / "run5", "k5", live5)
        (st / "cache.json").unlink()
        write(st / "state[1].json", "STALE")
        write(st / "skill.yaml", "name: demo5\nsecurity:\n  write_paths: ['outputs/', "
              f"'{live5.resolve()}/cache.json', '.codex/skills/demo5/state[1].json']\n")
        write(st / "state1.json", "new content\n")
        assert accept(t / "run5", "k5", live5, False, vault) == 0
        assert (live5 / "cache.json").read_text() == "LIVE-CACHE"
        assert (live5 / "state[1].json").read_text() == "LIVE-S1"
        assert (live5 / "state1.json").read_text() == "new content\n"

        # a protected nested state file created by the runtime after staging is not drift
        live6 = vault / ".codex" / "skills" / "demo6"
        write(live6 / "SKILL.md", "a\n")
        write(live6 / "skill.yaml", "name: demo6\nsecurity:\n  write_paths: ['.codex/skills/demo6/cache/state.json']\n")
        st = stage(t / "run6", "k6", live6)
        write(st / "SKILL.md", "b\n")
        write(live6 / "cache" / "state.json", "RUNTIME")
        assert accept(t / "run6", "k6", live6, False, vault) == 0
        assert (live6 / "cache" / "state.json").read_text() == "RUNTIME" and (live6 / "SKILL.md").read_text() == "b\n"

        # if the protection record cannot be written, nothing is copied
        accepted_dir = vault / "outputs" / "skill-evals" / "accepted"
        st = stage(t / "run7", "k7", live6)
        write(st / "SKILL.md", "c\n")
        saved = t / "accepted-saved"
        accepted_dir.rename(saved)
        accepted_dir.write_text("not a directory")
        try:
            assert accept(t / "run7", "k7", live6, False, vault) == 2
            assert (live6 / "SKILL.md").read_text() == "b\n"
        finally:
            accepted_dir.unlink()
            saved.rename(accepted_dir)

        # a permission change on a content directory after staging is drift
        write(live6 / "references" / "r.md", "r\n")
        st = stage(t / "run8", "k8", live6)
        os.chmod(live6 / "references", 0o700)
        assert accept(t / "run8", "k8", live6, False, vault) == 4
        os.chmod(live6 / "references", 0o755)

        # propagation records protection before copying: if recording fails nothing is copied
        prop_dir = vault / "outputs" / "skill-evals" / "propagated"
        write(src3 / "SKILL.md", "s2\n")
        saved = t / "propagated-saved"
        prop_dir.rename(saved)
        prop_dir.write_text("not a directory")
        try:
            assert propagate(src3, dest_root, False, vault) == 7  # unreadable record: refused
            assert propagate(src3, dest_root, False, vault, overwrite_dest=True) == 2  # cannot record
            assert (dest_root / "demo3" / "SKILL.md").read_text() == "s\n"
        finally:
            prop_dir.unlink()
            saved.rename(prop_dir)

        # full permission bits count: 0644 -> 0600 on a content file after staging is drift
        st = stage(t / "run9", "k9", live6)
        os.chmod(live6 / "SKILL.md", 0o600)
        assert accept(t / "run9", "k9", live6, False, vault) == 4
        os.chmod(live6 / "SKILL.md", 0o644)

        # a sync record from an older format is not trusted (fails safe), its protection is kept
        rec_path = prop_dir / (hashlib.sha256(str((dest_root / "demo3").resolve()).encode()).hexdigest()[:8] + ".json")
        old = json.loads(rec_path.read_text())
        rec_path.write_text(json.dumps({"dest": old["dest"], "protect": old["protect"] + ["legacy.db"],
                                        "hash": tree_hash(dest_root / "demo3", set(old["protect"]))}))
        write(dest_root / "demo3" / "legacy.db", "DB")
        write(src3 / "SKILL.md", "s3\n")
        assert propagate(src3, dest_root, False, vault) == 7
        assert propagate(src3, dest_root, False, vault, overwrite_dest=True) == 0
        assert (dest_root / "demo3" / "legacy.db").read_text() == "DB"

        # outward symlinks are found (absolute, escaping, dangling); protected ones are exempt
        lk = t / "links"
        write(lk / "a.md", "a\n")
        (lk / "ok.md").symlink_to("a.md")
        assert outward_links(lk) == []
        (lk / "abs.md").symlink_to("/etc/hosts")
        (lk / "esc.md").symlink_to("../outside.md")
        (lk / "venv").mkdir()
        (lk / "venv" / "python").symlink_to("/usr/bin/python3")
        assert outward_links(lk) == ["abs.md -> /etc/hosts", "esc.md -> ../outside.md", "venv/python -> /usr/bin/python3"]
        assert outward_links(lk, {"venv"}) == ["abs.md -> /etc/hosts", "esc.md -> ../outside.md"]
    print("self-test ok")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path)
    ap.add_argument("--key")
    ap.add_argument("--live", type=Path)
    ap.add_argument("--propagate", type=Path, metavar="SKILL_DIR")
    ap.add_argument("--dest-root", type=Path, default=Path("~/.claude/skills"))
    ap.add_argument("--state-paths", type=Path, metavar="SKILL_DIR")
    ap.add_argument("--check-links", type=Path, metavar="DIR",
                    help="list symlinks that are absolute or leave DIR (exit 1 if any)")
    ap.add_argument("--identity", type=Path, metavar="LIVE_DIR",
                    help="with --check-links on a staging copy: the live skill whose runtime state is exempt")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--protect", action="append", default=[], metavar="REL_PATH",
                    help="extra runtime-state path (relative to the skill dir) to never overwrite; repeatable")
    ap.add_argument("--overwrite-dest", action="store_true", help="propagate even if the destination has foreign changes (user-confirmed)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return 0
    if a.check_links:
        ident = a.identity.resolve() if a.identity else None
        protect = (state_paths(a.check_links, ident) | (effective_protect(ident, vault_root(), set(a.protect))
                                                      if ident else set(a.protect)))
        bad = outward_links(a.check_links, protect)
        print("\n".join(bad) if bad else "no outward symlinks")
        return 1 if bad else 0
    if a.state_paths:
        print("\n".join(sorted(effective_protect(a.state_paths.resolve(), vault_root(), set(a.protect)))))
        return 0
    if a.propagate:
        return propagate(a.propagate, a.dest_root, a.dry_run, vault_root(), a.overwrite_dest, set(a.protect))
    if a.run and a.key and a.live:
        return accept(a.run.resolve(), a.key, a.live, a.dry_run, vault_root(), set(a.protect))
    ap.error("give --run/--key/--live, --propagate, --state-paths or --self-test")
    return 2


if __name__ == "__main__":
    sys.exit(main())
