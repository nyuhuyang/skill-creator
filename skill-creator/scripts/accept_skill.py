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
RECORD_VERSION = 3  # bump when tree_entries/tree_hash change; older sync hashes then count as foreign


def vault_root() -> Path:
    if os.environ.get("KB_ROOT"):
        return Path(os.environ["KB_ROOT"]).expanduser().resolve()
    here = Path(__file__).resolve()
    if here.parents[2].name == "skills" and here.parents[3].name == ".codex":
        return here.parents[4]
    return Path("~/Documents/AI_Workspace/obsidian/knowledge_base").expanduser()


def manifest_problem(skill_dir: Path) -> str | None:
    """Why a skill.yaml that exists can't be read for state declarations (None if fine or absent).
    An unreadable manifest must not be mistaken for one that declares no runtime state."""
    manifest = skill_dir / "skill.yaml"
    if not os.path.lexists(manifest):
        return None  # genuinely absent: no manifest-declared state (explicit/recorded still apply)
    if not manifest.is_file():
        return f"{manifest}: not a regular file (dangling link or directory)"
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        return f"{manifest}: {str(exc).splitlines()[0]}"
    if not isinstance(data, dict):
        return f"{manifest}: empty or not a mapping"
    sec = data.get("security")
    if not isinstance(sec, dict):
        return f"{manifest}: security is missing or not a mapping"
    paths = sec.get("write_paths")
    if not (isinstance(paths, list) and all(isinstance(x, str) for x in paths)):
        return f"{manifest}: security.write_paths is missing or not a list of strings"
    return None


def state_paths(skill_dir: Path, identity: Path | None = None) -> set[str]:
    return _declared(skill_dir, identity)[0]


def whole_dir_declared(skill_dir: Path, identity: Path | None = None) -> bool:
    """True if the manifest declares the entire skill dir writable: runtime files could be
    anywhere in it, so the protected set can't be inferred."""
    return _declared(skill_dir, identity)[1]


def _declared(skill_dir: Path, identity: Path | None = None) -> tuple[set[str], bool]:
    """Runtime-state paths a skill declares inside its own dir (security.write_paths entries
    under .codex/skills/<name>/), relative to the skill dir. Missing/invalid manifest -> empty."""
    if not os.path.lexists(skill_dir / "skill.yaml") or manifest_problem(skill_dir):
        return set(), False  # absent, or unusable (callers refuse unless --ignore-bad-manifest)
    data = yaml.safe_load((skill_dir / "skill.yaml").read_text(encoding="utf-8"))
    paths = data["security"]["write_paths"]
    # A declared path is runtime state of this skill when it lands inside the skill dir, written
    # either root-relative (".codex/skills/<name>/x", ".agents/skills/<name>/x" — matched on
    # "<parent dir name>/<name>/") or absolute / home-relative.
    # The vault convention ".codex/skills/<name>/" is always recognised, because mirrors (e.g.
    # ~/.claude/skills/<name>) carry manifests written for the vault location.
    # `identity` = the canonical live dir when reading a baseline/staging copy, so declarations
    # are interpreted for the real target, not for the copy's physical location.
    ident = identity or skill_dir
    markers = {f".codex/skills/{ident.name}", f"{ident.parent.name}/{ident.name}"}  # no trailing slash
    real_dir = str(ident.resolve())
    out: set[str] = set()
    whole = False
    for entry in paths:
        if not isinstance(entry, str):
            continue
        entry = os.path.expanduser(entry).removeprefix("./")  # no strip: quoted names keep spaces
        if entry.startswith("/"):
            # absolute: keep the logical path under the skill dir (so symlinks along it are still
            # seen by linked_state). The dir may be spelled resolved or not (/var vs /private/var).
            parts = [c for c in entry.split("/") if c not in ("", ".")]
            logical = "/" + "/".join(parts)
            rel = None
            for prefix in {os.path.abspath(ident), real_dir}:
                if logical == prefix:
                    rel = "."
                elif logical.startswith(prefix + "/"):
                    rel = logical[len(prefix) + 1:]
                if rel is not None:
                    break
            if rel is not None and ".." in rel.split("/"):
                whole = True  # ".." inside the skill: never guess
                continue
            if rel is None:
                real = os.path.relpath(os.path.realpath(entry), real_dir)
                if real == ".." or real.startswith("../"):
                    continue  # really outside the skill
                whole = True  # reaches the skill only through some symlink: unresolved scope
                continue
        else:
            clean = [c for c in entry.split("/") if c not in ("", ".")]
            if ".." in clean and any(("/" + "/".join(clean[:i])).endswith("/" + m)
                                     for m in markers for i in range(1, len(clean) + 1)):
                # the path walks through this skill before a "..": a symlink inside the skill can
                # land it back in the skill even if the name normalises outside. Never guess.
                whole = True
                continue
            # "/" + normalised path, so ".codex/skills/demo", "./.codex//skills/./demo/" and
            # "kb/.codex/skills/demo/x" all match
            norm = "/" + os.path.normpath(entry)
            rel = None
            for m in markers:
                if norm.endswith("/" + m):
                    rel = "."
                    break
                at = norm.find("/" + m + "/")
                if at >= 0:
                    rel = norm[at + len(m) + 2:]
                    break
            if rel is None:
                continue
            if ".." in entry.split("/"):
                # e.g. ".codex/skills/./demo/cache/../state.json": the name normalises into this skill,
                # but if cache is a symlink the OS reaches a different file. Never guess: any ".."
                # in a path that lands in this skill is unresolved scope (refused unless reviewed).
                whole = True
                continue
        rel = os.path.normpath(rel)
        if rel == ".":
            whole = True  # the whole skill dir: unresolved scope, never silently "no state"
        elif rel not in ("", "..") and not rel.startswith("../"):
            out.add(rel)
    return out, whole


def try_lock(vault: Path, name: str):
    """Exclusive non-blocking lock file under outputs/skill-evals/locks/; None if held elsewhere.
    The lock is released when the returned file is closed or the process exits."""
    locks = vault / "outputs" / "skill-evals" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    f = open(locks / f"{name}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def write_record(record: Path, data: dict) -> None:
    """Atomic replace through a uniquely named temp file in the same dir."""
    record.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=record.parent, prefix=record.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"v": RECORD_VERSION, **data}) + "\n")
    os.replace(tmp, record)


def path_key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]


def state_only_dirs(roots: list[Path], protect: set[str]) -> set[str]:
    """Ancestor dirs of protected paths that hold no unprotected file or link in any of `roots`.
    They are left alone for one run (not copied, not compared), so rsync never resets their
    permissions; they are not persisted, so later real content in them still syncs."""
    cands = ancestors(protect)
    def content(root: Path, d: str) -> bool:
        path = root / d
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            return True  # a type clash is real content, never skipped
        for dirpath, dirs, names in walk(path):  # symlinked dirs appear in `dirs`, unfollowed
            for n in names + dirs:
                rel = os.path.relpath(os.path.join(dirpath, n), root)
                if excluded(rel, protect):
                    continue
                is_dir = n in dirs and not os.path.islink(os.path.join(dirpath, n))
                if not is_dir or rel not in cands:  # files, links and unrelated dirs are content
                    return True
        return False
    return {d for d in cands if not any(content(r, d) for r in roots)}


def recorded_protect(skill_dir: Path, vault: Path) -> set[str]:
    """Protection recorded for this directory by earlier acceptances *and* propagations. Both
    record kinds are keyed by the same canonical-path key, so a dir that was once accepted into
    and once synced into keeps the union of both."""
    key = path_key(skill_dir)
    out: set[str] = set()
    for kind in ("accepted", "propagated"):
        record = vault / "outputs" / "skill-evals" / kind / f"{key}.json"
        if record.is_file():
            out |= set(json.loads(record.read_text()).get("protect", []))
    return out


def bad_explicit(explicit: set[str]) -> list[str]:
    """--protect paths that aren't clean paths relative to the skill dir."""
    return sorted(e for e in explicit if not e or e in (".", "./") or os.path.isabs(e) or ".." in e.split("/"))


def effective_protect(skill_dir: Path, vault: Path, explicit: set[str] = frozenset()) -> set[str]:
    """Everything that must never be overwritten in this skill dir: declared state, protection
    recorded by earlier acceptances or propagations, and explicit --protect paths."""
    return state_paths(skill_dir) | recorded_protect(skill_dir, vault) | {os.path.normpath(e) for e in explicit}


def _walk_error(err: OSError) -> None:
    if isinstance(err, FileNotFoundError):
        return  # absent (or vanished) dir: nothing to inspect, not an error
    raise ValueError(f"cannot read directory {err.filename}: {err.strerror or err}")


def walk(root):
    """os.walk that never follows symlinked dirs and raises ValueError instead of silently
    skipping a directory it can't list (an unreadable dir would otherwise look 'equal')."""
    return os.walk(root, onerror=_walk_error)


def excluded(rel: str, protect: set[str]) -> bool:
    parts = rel.split("/")
    if any(p in ALWAYS_EXCLUDED for p in parts):
        return True
    return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in protect)


def ancestors(protect: set[str]) -> set[str]:
    """All parent dirs of protected paths (relative): possible scaffolding for runtime state."""
    out = set()
    for p in protect:
        parts = p.split("/")[:-1]
        out |= {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}
    return out


def tree_entries(root: Path, protect: set[str]) -> dict[str, tuple]:
    """lstat view of a tree: files (exec bits + content hash) and symlinks (target).
    Protected/excluded paths are skipped and not descended into. Directories are entries (with
    their permission bits) only when they hold unprotected content, so a dir that exists only to
    hold protected state never counts as a difference, while a permission change on a content dir
    does. The root's own mode is not compared (copies are created by mkdir)."""
    out: dict[str, tuple] = {}
    if not root.exists():
        return out
    scaffolding = ancestors(protect)  # recorded below only if they also hold other content
    for dirpath, dirs, names in walk(root):  # does not follow symlinked dirs
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
                if rel not in scaffolding:  # empty or not, an ordinary dir is compared with its mode
                    out[rel] = ("dir", st.st_mode & 0o7777)
            elif stat.S_ISREG(st.st_mode):
                out[rel] = ("file", st.st_mode & 0o7777, hashlib.sha256(p.read_bytes()).hexdigest())
            else:  # fifo, socket, device: reading could block or fail; never copied or compared
                raise ValueError(f"unsupported entry type (not file/dir/symlink): {p}")
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
    for dirpath, dirs, names in walk(root):
        for n in dirs + names:
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root)
            if os.path.islink(p) and not excluded(rel, protect):
                target = os.readlink(p)
                resolved = os.path.realpath(p)
                if os.path.isabs(target) or not (resolved == real_root or resolved.startswith(real_root + os.sep)):
                    bad.append(f"{rel} -> {target}")
        dirs[:] = [d for d in dirs if not excluded(os.path.relpath(os.path.join(dirpath, d), root), protect)]
    return sorted(bad)


def linked_state(dirs: list[Path], protect: set[str]) -> list[str]:
    """Protected paths whose real files could lie outside the protected set, in any of `dirs`:
    (a) the path itself, or a directory on the way to it, is a symlink; (b) a symlink *inside* a
    protected directory resolves to unprotected content of the skill dir (an alias like
    cache/alias -> ../data). rsync protects only the logical paths, so such skills are refused
    until the resolved files are protected explicitly. Links that leave the skill dir (e.g. a
    venv's python -> /opt/...) alias nothing in it and are fine."""
    hits = set()
    for d in dirs:
        real_d = os.path.realpath(d)
        for p in protect:
            parts = p.split("/")
            for i in range(1, len(parts) + 1):
                if os.path.islink(d / "/".join(parts[:i])):
                    hits.add(f"{d.name}: {p} (via {'/'.join(parts[:i])})")
                    break
            base = d / p
            if base.is_dir() and not base.is_symlink():
                for dirpath, dnames, fnames in walk(base):  # links in dnames are not followed
                    for n in dnames + fnames:
                        link = os.path.join(dirpath, n)
                        if not os.path.islink(link):
                            continue
                        target = os.path.relpath(os.path.realpath(link), real_d)
                        if target != ".." and not target.startswith("../") and not excluded(target, protect):
                            hits.add(f"{d.name}: {os.path.relpath(link, d)} -> unprotected {target}")
        # (c) hard links: a protected file sharing an inode with an unprotected one would have its
        # mode changed when rsync updates the unprotected name
        protected_inodes, shared = {}, []
        if d.is_dir():
            for dirpath, dnames, fnames in walk(d):
                for n in fnames:
                    f = os.path.join(dirpath, n)
                    st = os.lstat(f)
                    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                        rel = os.path.relpath(f, d)
                        (protected_inodes.setdefault((st.st_dev, st.st_ino), rel) if excluded(rel, protect)
                         else shared.append(((st.st_dev, st.st_ino), rel)))
            for ino, rel in shared:
                if ino in protected_inodes:
                    hits.add(f"{d.name}: {rel} is a hard link to protected {protected_inodes[ino]}")
    return sorted(hits)


def special_entries(dirs: list[Path], protect: set[str]) -> list[str]:
    """Unprotected entries that are not regular files, dirs or symlinks (fifos, sockets, devices)."""
    out = []
    for d in dirs:
        if not d.is_dir():
            continue
        for dirpath, dnames, fnames in walk(d):
            for n in dnames + fnames:
                f = os.path.join(dirpath, n)
                rel = os.path.relpath(f, d)
                mode = os.lstat(f).st_mode
                if not excluded(rel, protect) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                    out.append(f"{d.name}: {rel}")
            dnames[:] = [x for x in dnames if not excluded(os.path.relpath(os.path.join(dirpath, x), d), protect)]
    return sorted(out)


def unsupported(protect: set[str]) -> list[str]:
    """Protected paths whose names rsync filter rules can't match literally across backends
    (backslash semantics differ, newlines can't be expressed). Such skills are refused."""
    return sorted(p for p in protect if "\\" in p or "\n" in p)


def review(live: Path, staging: Path, protect: set[str]) -> list[str]:
    """Everything acceptance would change, protected state excluded: added/removed entries, type,
    mode and symlink-target changes, and a unified diff for changed text files."""
    import difflib
    run_protect = protect | state_only_dirs([live, staging], protect)
    a, b = tree_entries(live, run_protect), tree_entries(staging, run_protect)
    out = []

    def text_diff(rel: str, old_file: bool, new_file: bool) -> str:
        """Unified diff of a file's text; a side that isn't a regular file counts as empty."""
        try:
            la = (live / rel).read_text(encoding="utf-8").splitlines(keepends=True) if old_file else []
            lb = (staging / rel).read_text(encoding="utf-8").splitlines(keepends=True) if new_file else []
        except UnicodeDecodeError:
            return f"binary content: {rel}"
        return "".join(difflib.unified_diff(la, lb, f"live/{rel}", f"staging/{rel}")).rstrip("\n")

    for rel in sorted(a.keys() | b.keys()):
        old, new = a.get(rel), b.get(rel)
        if old == new:
            continue
        if old is None or new is None:
            entry = new or old
            out.append(f"{'added' if old is None else 'removed'}: {rel} ({entry[0]}"
                       + (f", mode {oct(entry[1])})" if entry[0] in ("file", "dir") else f" -> {entry[1]})"))
            if entry[0] == "file":  # show the whole text that appears or disappears
                out.append(text_diff(rel, old is not None, new is not None))
            continue
        if old[0] != new[0]:
            out.append(f"type changed: {rel} {old[0]} -> {new[0]}")
            if "file" in (old[0], new[0]):
                out.append(text_diff(rel, old[0] == "file", new[0] == "file"))
        elif old[0] == "link":
            out.append(f"symlink target: {rel} {old[1]} -> {new[1]}")
        elif old[0] == "dir":
            out.append(f"mode: {rel}/ {oct(old[1])} -> {oct(new[1])}")
        else:
            if old[1] != new[1]:
                out.append(f"mode: {rel} {oct(old[1])} -> {oct(new[1])}")
            if old[2] != new[2]:
                out.append(text_diff(rel, True, True))
    return out


def rsync(src: Path, dest: Path, protect: set[str], dry_run: bool) -> int:
    # --checksum: the default size+mtime quick check skips same-size edits made within one second
    # --no-owner/--no-group: never restore an old owner or group over one changed at the
    # destination (e.g. a file moved from a shared to a private group)
    cmd = ["rsync", "-a", "--no-owner", "--no-group", "--checksum", "--delete"]
    cmd += [f"--exclude={p}" for p in ALWAYS_EXCLUDED]
    # anchored; also shields them from --delete. Escape wildcard chars so "state[1].json" stays literal.
    cmd += ["--exclude=/" + re.sub(r"([*?\[\]])", r"\\\1", p) for p in sorted(protect)]
    if dry_run:
        cmd += ["--dry-run", "--itemize-changes"]
    cmd += [f"{src}/", f"{dest}/"]
    # rsync -a copies the source root's mode onto the destination root. The root's permissions
    # belong to the destination, and neither root may be widened at any moment: during the
    # transfer the source root takes the intersection of both modes (only ever narrower than
    # its own), so the destination root is also only narrowed; afterwards both get their own
    # modes back. A kill mid-transfer leaves roots narrower, never wider.
    if dry_run or not dest.is_dir():
        return subprocess.run(cmd).returncode
    dest_mode = stat.S_IMODE(os.lstat(dest).st_mode)
    src_mode = stat.S_IMODE(os.lstat(src).st_mode)
    os.chmod(src, src_mode & dest_mode)
    try:
        return subprocess.run(cmd).returncode
    finally:
        os.chmod(src, src_mode)
        if dest.is_dir() and stat.S_IMODE(os.lstat(dest).st_mode) != dest_mode:
            os.chmod(dest, dest_mode)


def single_child(d: Path) -> Path | None:
    kids = [k for k in d.iterdir() if k.is_dir()] if d.is_dir() else []
    return kids[0] if len(kids) == 1 else None


def accept(run: Path, key: str, live: Path, dry_run: bool, vault: Path,
           explicit: set[str] = frozenset(), ignore_bad_manifest: bool = False) -> int:
    live = live.resolve()
    baseline, staging = single_child(run / "baseline" / key), single_child(run / "staging" / key)
    if not (live.is_dir() and baseline and staging):
        print(f"error: need live dir and exactly one baseline/staging dir under {run}/*/{key}", file=sys.stderr)
        return 2
    if bad_explicit(explicit):
        print(f"error: --protect paths must be clean relative paths inside the skill: {bad_explicit(explicit)}",
              file=sys.stderr)
        return 2
    if key != path_key(live):
        print(f"error: key {key} does not match the live path (expected {path_key(live)})", file=sys.stderr)
        return 2
    if not (baseline.name == staging.name == live.name):
        print(f"error: name mismatch live={live.name} baseline={baseline.name} staging={staging.name}", file=sys.stderr)
        return 2
    try:
        lock = try_lock(vault, key)
    except OSError as exc:
        print(f"error: cannot create lock under outputs/skill-evals/locks: {exc}", file=sys.stderr)
        return 2
    if lock is None:
        print(f"busy: another process is accepting {key}", file=sys.stderr)
        return 3
    with lock:  # everything below reads and writes protection state under the lock
        try:
            return _accept_locked(key, live, baseline, staging, dry_run, vault, explicit, ignore_bad_manifest)
        except ValueError as exc:
            print(f"error: {exc}; can't inspect the trees safely. Fix permissions and retry.", file=sys.stderr)
            return 2


def _accept_locked(key: str, live: Path, baseline: Path, staging: Path, dry_run: bool, vault: Path,
                   explicit: set[str], ignore_bad_manifest: bool) -> int:
    bad = [m for m in map(manifest_problem, (live, baseline, staging)) if m]
    bad += [f"{d}/skill.yaml: a write path can't be resolved safely (the whole skill dir, or '..' inside it)"
            for d in (live, baseline, staging) if whole_dir_declared(d, live)]
    if bad and not ignore_bad_manifest:
        print(f"error: runtime state can't be inferred: {bad}. Fix the manifest(s), or pass every "
              f"runtime file with --protect and --ignore-bad-manifest after review. Nothing copied.",
              file=sys.stderr)
        return 2
    # Protection accepted earlier for this path persists even if a later update stops declaring it;
    # retiring a state path is a separate, explicit decision (delete the record entry by hand).
    record = vault / "outputs" / "skill-evals" / "accepted" / f"{key}.json"
    rec_protect = set(json.loads(record.read_text()).get("protect", [])) if record.is_file() else set()
    protect = (effective_protect(live, vault, explicit) | state_paths(baseline, live) | state_paths(staging, live)
               | rec_protect)
    if unsupported(protect):
        print(f"error: runtime-state names rsync can't protect literally: {unsupported(protect)}; "
              f"rename them or exclude this skill. Nothing copied.", file=sys.stderr)
        return 2
    specials = special_entries([live, baseline, staging], protect)
    if specials:
        print(f"error: unsupported entries (fifo/socket/device) can't be compared or copied: {specials}. "
              f"Remove them or protect them with --protect. Nothing copied.", file=sys.stderr)
        return 2
    via_links = linked_state([live, baseline, staging], protect)
    if via_links and not ignore_bad_manifest:
        print(f"error: runtime state reached through symlinks: {via_links}. Protect the files the links "
              f"resolve to with --protect and pass --ignore-bad-manifest after review. Nothing copied.",
              file=sys.stderr)
        return 2
    run_protect = protect | state_only_dirs([live, baseline, staging], protect)
    drift = tree_diff(live, baseline, run_protect)
    if drift:
        print(f"diverged: {live} changed since staging; re-stage and re-review. Paths: {drift[:10]}", file=sys.stderr)
        return 4
    print(f"protected runtime state: {sorted(protect) or 'none'}")
    if not dry_run:  # record first: a later update must never lose this protection
        try:
            write_record(record, {"live": str(live), "protect": sorted(protect)})
        except OSError as exc:
            print(f"error: cannot record protection at {record}: {exc}; nothing copied", file=sys.stderr)
            return 2
    if rsync(staging, live, run_protect, dry_run) != 0:
        return 5
    if dry_run:
        return 0
    mismatch = tree_diff(staging, live, run_protect)
    if mismatch:
        print(f"verify failed: {mismatch[:10]}", file=sys.stderr)
        return 5
    print(f"accepted: {live}")
    return 0


def propagate(src: Path, dest_root: Path, dry_run: bool, vault: Path, overwrite_dest: bool = False,
              explicit: set[str] = frozenset(), ignore_bad_manifest: bool = False) -> int:
    src = src.resolve()
    if not (src / "SKILL.md").is_file():
        print(f"error: {src} has no SKILL.md", file=sys.stderr)
        return 2
    if bad_explicit(explicit):
        print(f"error: --protect paths must be clean relative paths inside the skill: {bad_explicit(explicit)}",
              file=sys.stderr)
        return 2
    # absolute local path: a relative "host:dir" would otherwise be read by rsync as a remote target
    dest = Path(os.path.abspath(dest_root.expanduser())) / src.name
    real_dest = Path(os.path.realpath(dest))
    if real_dest == src or src in real_dest.parents or real_dest in src.parents:
        print(f"error: source {src} and destination {dest} overlap; nothing created or copied", file=sys.stderr)
        return 2
    if dest.is_symlink():
        print(f"refused: {dest} is a symlink (maintained elsewhere)", file=sys.stderr)
        return 6
    # One lock for the source (shared with acceptance, so the source can't change mid-sync) and one
    # for the destination, held from reading the record until the final record is written.
    try:
        src_lock = try_lock(vault, path_key(src))
        dest_lock = try_lock(vault, path_key(dest)) if src_lock else None  # same namespace as accept
    except OSError as exc:
        print(f"error: cannot create lock under outputs/skill-evals/locks: {exc}", file=sys.stderr)
        return 2
    if src_lock is None or dest_lock is None:
        if src_lock:
            src_lock.close()
        print(f"busy: another accept/propagate holds {src} or {dest}", file=sys.stderr)
        return 3
    with src_lock, dest_lock:
        try:
            return _propagate_locked(src, dest, dry_run, vault, overwrite_dest, explicit, ignore_bad_manifest)
        except ValueError as exc:
            print(f"error: {exc}; can't inspect the trees safely. Fix permissions and retry.", file=sys.stderr)
            return 2


def _propagate_locked(src: Path, dest: Path, dry_run: bool, vault: Path, overwrite_dest: bool,
                      explicit: set[str], ignore_bad_manifest: bool) -> int:
    bad = [m for m in map(manifest_problem, (src, dest)) if m]
    bad += [f"{d}/skill.yaml: a write path can't be resolved safely (the whole skill dir, or '..' inside it)"
            for d, ident in ((src, src), (dest, dest), (src, dest), (dest, src))
            if os.path.lexists(d) and whole_dir_declared(d, ident)]
    if bad and not ignore_bad_manifest:
        print(f"error: runtime state can't be inferred: {bad}. Fix the manifest(s), or pass every "
              f"runtime file with --protect and --ignore-bad-manifest after review. Nothing copied.",
              file=sys.stderr)
        return 2
    # The destination is a mirror. A record of our last sync (in the vault) holds the protected set
    # and the destination hash we left behind, so (a) state paths stay protected even after an update
    # stops declaring them, and (b) we refuse when the destination changed outside this script.
    record = vault / "outputs" / "skill-evals" / "propagated" / (path_key(dest) + ".json")
    rec = json.loads(record.read_text()) if record.is_file() else {}
    rec_protect = set(rec.get("protect", []))
    # each manifest is read for both locations: the source may declare state by the destination's
    # absolute path (and vice versa)
    protect = (effective_protect(src, vault, explicit) | effective_protect(dest, vault) | rec_protect
               | state_paths(src, dest) | (state_paths(dest, src) if dest.exists() else set()))
    print(f"protected runtime state: {sorted(protect) or 'none'}")
    if unsupported(protect):
        print(f"error: runtime-state names rsync can't protect literally: {unsupported(protect)}; "
              f"nothing copied.", file=sys.stderr)
        return 2
    specials = special_entries([src, dest], protect)
    if specials:
        print(f"error: unsupported entries (fifo/socket/device) can't be compared or copied: {specials}. "
              f"Remove them or protect them with --protect. Nothing copied.", file=sys.stderr)
        return 2
    via_links = linked_state([src, dest], protect)
    if via_links and not ignore_bad_manifest:
        print(f"error: runtime state reached through symlinks: {via_links}. Protect the files the links "
              f"resolve to with --protect and pass --ignore-bad-manifest after review. Nothing copied.",
              file=sys.stderr)
        return 2
    run_protect = protect | state_only_dirs([src, dest], protect)
    if dest.exists() and tree_diff(src, dest, run_protect):
        foreign = (not rec or rec.get("v") != RECORD_VERSION
                   or rec.get("hash") != tree_hash(dest, rec_protect))
        if foreign and not overwrite_dest:
            print(f"refused: {dest} has changes not made by a previous propagation "
                  f"({'modified since last sync' if rec else 'never synced by this script'}). "
                  f"Differing paths: {tree_diff(src, dest, run_protect)[:10]}. Reconcile them into the vault "
                  f"copy first, or rerun with --overwrite-dest after the user confirms.", file=sys.stderr)
            return 7
    if dry_run and not dest.exists():
        print(f"would create {dest} with {sum(e[0] == 'file' for e in tree_entries(src, protect).values())} file(s)")
        return 0

    def save(hash_: str | None) -> None:
        write_record(record, {"dest": str(dest), "protect": sorted(protect), "hash": hash_})

    if not dry_run:
        try:  # protection first, keeping the previous sync hash until this copy is verified
            save(rec.get("hash") if rec.get("v") == RECORD_VERSION else None)
        except OSError as exc:
            print(f"error: cannot record protection at {record}: {exc}; nothing copied", file=sys.stderr)
            return 2
        if not dest.exists():  # a new mirror is never broader than the source root
            dest.mkdir(parents=True)
            os.chmod(dest, stat.S_IMODE(os.lstat(src).st_mode))
    if rsync(src, dest, run_protect, dry_run) != 0:
        return 5
    if dry_run:
        return 0
    mismatch = tree_diff(src, dest, run_protect)
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
        k1 = path_key(live)
        for kind in ("baseline", "staging"):
            dst = run / kind / k1 / "demo"
            subprocess.run(["cp", "-Rp", f"{live}/.", str(dst) + "/"] if dst.mkdir(parents=True) is None else [])
        write(run / "staging" / k1 / "demo" / "SKILL.md", "new\n")
        write(run / "staging" / k1 / "demo" / "last_run.json", "STALE")

        assert state_paths(live) == {"last_run.json"}
        assert not (vault / "outputs").exists()
        # accept: content updated, runtime state preserved, locks/ parent created
        assert accept(run, k1, live, False, vault) == 0
        assert (live / "SKILL.md").read_text() == "new\n"
        assert (live / "last_run.json").read_text() == "LIVE"
        assert (vault / "outputs" / "skill-evals" / "locks").is_dir()

        # diverged: live changed after baseline -> exit 4, nothing written
        write(live / "SKILL.md", "edited elsewhere\n")
        assert accept(run, k1, live, False, vault) == 4
        assert (live / "SKILL.md").read_text() == "edited elsewhere\n"

        # busy: another process holds the lock -> exit 3
        write(live / "SKILL.md", "old\n")  # back to baseline content
        lockfile = vault / "outputs" / "skill-evals" / "locks" / f"{k1}.lock"
        holder = subprocess.Popen([sys.executable, "-c",
            "import fcntl,sys,time;f=open(sys.argv[1],'w');fcntl.flock(f,fcntl.LOCK_EX);print('held',flush=True);time.sleep(30)",
            str(lockfile)], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "held"
            assert accept(run, k1, live, False, vault) == 3
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
                subprocess.run(["cp", "-Rp", f"{skill}/.", f"{dst}/"], check=True)
            return run_dir / "staging" / key / skill.name

        # exec-bit change on the live copy after staging is drift
        live2 = vault / ".codex" / "skills" / "demo2"
        write(live2 / "SKILL.md", "v1\n")
        write(live2 / "run.sh", "echo hi\n")
        write(live2 / "skill.yaml", manifest("last_run.json").replace("demo", "demo2"))
        write(live2 / "last_run.json", "STATE")
        st = stage(t / "run2", path_key(live2), live2)
        os.chmod(live2 / "run.sh", 0o755)
        assert accept(t / "run2", path_key(live2), live2, False, vault) == 4
        os.chmod(live2 / "run.sh", 0o644)
        assert accept(t / "run2", path_key(live2), live2, False, vault) == 0  # records protect {last_run.json}

        # later updates that stop declaring the state path still preserve it
        st = stage(t / "run3", path_key(live2), live2)
        write(st / "skill.yaml", "name: demo2\nsecurity:\n  write_paths: ['outputs/']\n")
        write(st / "last_run.json", "STAGED-STALE")
        assert accept(t / "run3", path_key(live2), live2, False, vault) == 0
        assert (live2 / "last_run.json").read_text() == "STATE"
        st = stage(t / "run4", path_key(live2), live2)  # no manifest declares it any more
        (st / "last_run.json").unlink()
        write(st / "SKILL.md", "v2\n")
        assert accept(t / "run4", path_key(live2), live2, False, vault) == 0
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
        st = stage(t / "run5", path_key(live5), live5)
        (st / "cache.json").unlink()
        write(st / "state[1].json", "STALE")
        write(st / "skill.yaml", "name: demo5\nsecurity:\n  write_paths: ['outputs/', "
              f"'{live5.resolve()}/cache.json', '.codex/skills/demo5/state[1].json']\n")
        write(st / "state1.json", "new content\n")
        assert accept(t / "run5", path_key(live5), live5, False, vault) == 0
        assert (live5 / "cache.json").read_text() == "LIVE-CACHE"
        assert (live5 / "state[1].json").read_text() == "LIVE-S1"
        assert (live5 / "state1.json").read_text() == "new content\n"

        # a protected nested state file created by the runtime after staging is not drift
        live6 = vault / ".codex" / "skills" / "demo6"
        write(live6 / "SKILL.md", "a\n")
        write(live6 / "skill.yaml", "name: demo6\nsecurity:\n  write_paths: ['.codex/skills/demo6/cache/state.json']\n")
        st = stage(t / "run6", path_key(live6), live6)
        write(st / "SKILL.md", "b\n")
        write(live6 / "cache" / "state.json", "RUNTIME")
        assert accept(t / "run6", path_key(live6), live6, False, vault) == 0
        assert (live6 / "cache" / "state.json").read_text() == "RUNTIME" and (live6 / "SKILL.md").read_text() == "b\n"

        # if the protection record cannot be written, nothing is copied
        accepted_dir = vault / "outputs" / "skill-evals" / "accepted"
        st = stage(t / "run7", path_key(live6), live6)
        write(st / "SKILL.md", "c\n")
        saved = t / "accepted-saved"
        accepted_dir.rename(saved)
        accepted_dir.write_text("not a directory")
        try:
            assert accept(t / "run7", path_key(live6), live6, False, vault) == 2
            assert (live6 / "SKILL.md").read_text() == "b\n"
        finally:
            accepted_dir.unlink()
            saved.rename(accepted_dir)

        # a permission change on a content directory after staging is drift
        write(live6 / "references" / "r.md", "r\n")
        st = stage(t / "run8", path_key(live6), live6)
        os.chmod(live6 / "references", 0o700)
        assert accept(t / "run8", path_key(live6), live6, False, vault) == 4
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
        st = stage(t / "run9", path_key(live6), live6)
        os.chmod(live6 / "SKILL.md", 0o600)
        assert accept(t / "run9", path_key(live6), live6, False, vault) == 4
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
        (lk / "sub").mkdir()
        (lk / "sub" / "root").symlink_to("..")  # resolves to the root itself: inside
        assert outward_links(lk) == []
        (lk / "sub" / "root").unlink()
        (lk / "abs.md").symlink_to("/etc/hosts")
        (lk / "esc.md").symlink_to("../outside.md")
        (lk / "venv").mkdir()
        (lk / "venv" / "python").symlink_to("/usr/bin/python3")
        assert outward_links(lk) == ["abs.md -> /etc/hosts", "esc.md -> ../outside.md", "venv/python -> /usr/bin/python3"]
        assert outward_links(lk, {"venv"}) == ["abs.md -> /etc/hosts", "esc.md -> ../outside.md"]

        # a runtime-state name with a backslash can't be protected literally: refuse, copy nothing
        live10 = vault / ".codex" / "skills" / "demo10"
        write(live10 / "SKILL.md", "a\n")
        state_name = "state" + chr(92) + "run.json"  # one literal backslash
        write(live10 / "skill.yaml", "name: demo10\nsecurity:\n  write_paths: ['.codex/skills/demo10/"
              + state_name + "']\n")
        write(live10 / state_name, "LIVE")
        assert state_paths(live10) == {state_name}, state_paths(live10)
        st = stage(t / "run10", path_key(live10), live10)
        write(st / "SKILL.md", "b\n")
        assert accept(t / "run10", path_key(live10), live10, False, vault) == 2
        assert (live10 / "SKILL.md").read_text() == "a\n" and (live10 / state_name).read_text() == "LIVE"
        assert propagate(live10, dest_root, False, vault, overwrite_dest=True) == 2

        # permissions of a dir that only holds protected state are left alone (not reset by rsync)
        live11 = vault / ".codex" / "skills" / "demo11"
        write(live11 / "SKILL.md", "a\n")
        write(live11 / "skill.yaml", "name: demo11\nsecurity:\n  write_paths: ['.codex/skills/demo11/cache/state.json']\n")
        write(live11 / "cache" / "state.json", "S")
        st = stage(t / "run11", path_key(live11), live11)
        write(st / "SKILL.md", "b\n")
        os.chmod(live11 / "cache", 0o700)
        assert accept(t / "run11", path_key(live11), live11, False, vault) == 0
        assert stat.S_IMODE(os.stat(live11 / "cache").st_mode) == 0o700
        assert (live11 / "SKILL.md").read_text() == "b\n"

        # an unreadable destination manifest means unknown runtime state: refuse unless reviewed
        write(dest_root / "demo11" / "skill.yaml", "a: [unclosed\n")
        write(dest_root / "demo11" / "keep.db", "DB")
        assert propagate(live11, dest_root, False, vault, overwrite_dest=True) == 2
        assert (dest_root / "demo11" / "keep.db").read_text() == "DB"
        assert propagate(live11, dest_root, False, vault, overwrite_dest=True, explicit={"keep.db"},
                         ignore_bad_manifest=True) == 0
        assert (dest_root / "demo11" / "keep.db").read_text() == "DB"

        # a concurrent propagation to the same destination holds its lock: busy (exit 3)
        lockfile = vault / "outputs" / "skill-evals" / "locks" / (path_key(dest_root / "demo11") + ".lock")
        holder = subprocess.Popen([sys.executable, "-c",
            "import fcntl,sys,time;f=open(sys.argv[1],'w');fcntl.flock(f,fcntl.LOCK_EX);print('held',flush=True);time.sleep(30)",
            str(lockfile)], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "held"
            assert propagate(live11, dest_root, False, vault) == 3
        finally:
            holder.kill()
            holder.wait()

        # malformed state declarations count as unknown state, like an unparsable manifest
        for bad_yaml in ("", "name: demo11\nsecurity: 'oops'\n",
                         "name: demo11\nsecurity:\n  write_paths: '.codex/skills/demo11/keep.db'\n"):
            write(dest_root / "demo11" / "skill.yaml", bad_yaml)
            assert manifest_problem(dest_root / "demo11"), repr(bad_yaml)
            assert propagate(live11, dest_root, False, vault, overwrite_dest=True) == 2
            assert (dest_root / "demo11" / "keep.db").read_text() == "DB"

        # an unprotected symlinked dir next to protected state makes the parent real content
        sod = t / "sod"
        write(sod / "cache" / "state.json", "S")
        (sod / "assets").mkdir()
        assert state_only_dirs([sod], {"cache/state.json"}) == {"cache"}
        (sod / "cache" / "assets").symlink_to("../assets")
        assert state_only_dirs([sod], {"cache/state.json"}) == set()

        # the destination root keeps its own permissions (rsync -a would copy the source root's)
        st = stage(t / "run12", path_key(live11), live11)
        write(st / "SKILL.md", "c\n")
        os.chmod(live11, 0o700)
        assert accept(t / "run12", path_key(live11), live11, False, vault) == 0
        assert stat.S_IMODE(os.stat(live11).st_mode) == 0o700
        os.chmod(live11, 0o755)

        # an existing manifest must declare security.write_paths; odd manifest entries are errors
        d13 = dest_root / "demo11"
        for setup in ("missing-security", "dangling", "directory"):
            m = d13 / "skill.yaml"
            if m.is_dir() and not m.is_symlink():
                m.rmdir()
            elif os.path.lexists(m):
                m.unlink()
            if setup == "missing-security":
                write(m, "name: demo11\n")
            elif setup == "dangling":
                m.symlink_to("nowhere.yaml")
            else:
                m.mkdir()
            assert manifest_problem(d13), setup
            assert propagate(live11, dest_root, False, vault, overwrite_dest=True) == 2, setup
            assert (d13 / "keep.db").read_text() == "DB"
        (d13 / "skill.yaml").rmdir()

        # the reviewed override works even when the manifest can't be parsed into state
        write(d13 / "skill.yaml", "name: demo11\nsecurity: 'oops'\n")
        assert state_paths(d13) == set()
        assert propagate(live11, dest_root, False, vault, overwrite_dest=True, explicit={"keep.db"},
                         ignore_bad_manifest=True) == 0
        assert (d13 / "keep.db").read_text() == "DB"

        # empty ordinary dirs count: their mode is compared, and they are content for state_only_dirs
        write(live11 / "assets" / "private" / ".keep", "")
        (live11 / "assets" / "private" / ".keep").unlink()
        st = stage(t / "run13", path_key(live11), live11)
        os.chmod(live11 / "assets" / "private", 0o700)
        assert accept(t / "run13", path_key(live11), live11, False, vault) == 4
        os.chmod(live11 / "assets" / "private", 0o755)
        (sod / "cache" / "assets").unlink()
        assert state_only_dirs([sod], {"cache/state.json"}) == {"cache"}
        (sod / "cache" / "new-empty-dir").mkdir()
        assert state_only_dirs([sod], {"cache/state.json"}) == set()

        # protection recorded by an acceptance into the destination dir survives a later sync there
        inst = dest_root / "demo14"
        write(inst / "SKILL.md", "v1\n")
        write(inst / "skill.yaml", "name: demo14\nsecurity:\n  write_paths: ['.codex/skills/demo14/last_run.json']\n")
        write(inst / "last_run.json", "INSTALLED-STATE")
        st = stage(t / "run14", path_key(inst), inst)
        write(st / "skill.yaml", "name: demo14\nsecurity:\n  write_paths: ['outputs/']\n")  # stops declaring it
        (st / "last_run.json").unlink()
        assert accept(t / "run14", path_key(inst), inst, False, vault) == 0  # accepted into ~/.claude-like root
        src14 = vault / ".codex" / "skills" / "demo14"
        write(src14 / "SKILL.md", "v2\n")
        write(src14 / "skill.yaml", "name: demo14\nsecurity:\n  write_paths: ['outputs/']\n")
        assert propagate(src14, dest_root, False, vault, overwrite_dest=True) == 0
        assert (inst / "last_run.json").read_text() == "INSTALLED-STATE"

        # acceptance and propagation share one lock namespace; a mismatched key is refused
        holder = subprocess.Popen([sys.executable, "-c",
            "import fcntl,sys,time;f=open(sys.argv[1],'w');fcntl.flock(f,fcntl.LOCK_EX);print('held',flush=True);time.sleep(30)",
            str(vault / "outputs" / "skill-evals" / "locks" / (path_key(inst) + ".lock"))], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "held"
            assert propagate(src14, dest_root, False, vault) == 3  # an accept into inst holds this lock
        finally:
            holder.kill()
            holder.wait()
        st = stage(t / "run15", path_key(inst), inst)
        assert accept(t / "run15", path_key(inst), inst, False, vault) == 0
        stage(t / "run16", "deadbeef", inst)
        assert accept(t / "run16", "deadbeef", inst, False, vault) == 2

        # "..state.json" is a file inside the skill, not a traversal; quoted spaces are kept
        live17 = vault / ".codex" / "skills" / "demo17"
        write(live17 / "SKILL.md", "a\n")
        write(live17 / "skill.yaml", "name: demo17\nsecurity:\n  write_paths: ['.codex/skills/demo17/..state.json', "
              "'.codex/skills/demo17/ padded.json']\n")
        write(live17 / "..state.json", "S1")
        write(live17 / " padded.json", "S2")
        assert state_paths(live17) == {"..state.json", " padded.json"}, state_paths(live17)
        st = stage(t / "run17", path_key(live17), live17)
        (st / "..state.json").unlink()
        (st / " padded.json").unlink()
        write(st / "SKILL.md", "b\n")
        assert accept(t / "run17", path_key(live17), live17, False, vault) == 0
        assert (live17 / "..state.json").read_text() == "S1" and (live17 / " padded.json").read_text() == "S2"

        # copies made with cp -Rp keep modes: a 0664 file is not false drift under umask 022
        old_umask = os.umask(0o022)
        try:
            write(live17 / "group.md", "g\n")
            os.chmod(live17 / "group.md", 0o664)
            st = stage(t / "run18", path_key(live17), live17)
            assert stat.S_IMODE(os.stat(st / "group.md").st_mode) == 0o664
            assert accept(t / "run18", path_key(live17), live17, False, vault) == 0
        finally:
            os.umask(old_umask)

        # declaring the whole skill dir writable is unresolved scope: refuse unless reviewed
        live19 = vault / ".codex" / "skills" / "demo19"
        write(live19 / "SKILL.md", "a\n")
        write(live19 / "skill.yaml", "name: demo19\nsecurity:\n  write_paths: ['.codex/skills/demo19/']\n")
        write(live19 / "state.db", "A")
        assert whole_dir_declared(live19) and state_paths(live19) == set()
        st = stage(t / "run19", path_key(live19), live19)
        write(st / "state.db", "B")
        assert accept(t / "run19", path_key(live19), live19, False, vault) == 2
        assert (live19 / "state.db").read_text() == "A"
        assert accept(t / "run19", path_key(live19), live19, False, vault, explicit={"state.db"},
                      ignore_bad_manifest=True) == 0
        assert (live19 / "state.db").read_text() == "A"
        assert propagate(live19, dest_root, False, vault) == 2

        # every spelling of the whole skill dir is unresolved scope
        for spelling in (".codex/skills/demo19", "./.codex/skills/demo19/", "kb/.codex/skills/demo19",
                         ".codex/skills/demo19/sub/..", str(live19)):
            write(live19 / "skill.yaml", f"name: demo19\nsecurity:\n  write_paths: ['{spelling}']\n")
            assert whole_dir_declared(live19) and state_paths(live19) == set(), spelling
        write(live19 / "skill.yaml", "name: demo19\nsecurity:\n  write_paths: ['.codex/skills/demo19x/a', "
              "'../../.codex/registry.json', '.codex/skills/other/../x', 'kb/.codex/skills/demo19/cache/x.json']\n")
        assert not whole_dir_declared(live19) and state_paths(live19) == {"cache/x.json"}, state_paths(live19)

        # the destination root is never widened; the source root's mode is restored afterwards
        a20, b20 = t / "a20", t / "b20"
        write(a20 / "f.txt", "new\n")
        write(b20 / "f.txt", "old\n")
        os.chmod(a20, 0o755)
        os.chmod(b20, 0o700)
        assert rsync(a20, b20, set(), False) == 0
        assert stat.S_IMODE(os.stat(b20).st_mode) == 0o700 and stat.S_IMODE(os.stat(a20).st_mode) == 0o755
        assert (b20 / "f.txt").read_text() == "new\n"

        # neither root is ever wider than its own mode while rsync runs; owner/group not copied
        seen = []
        real_run = subprocess.run
        def watching_run(cmd, *a, **k):
            seen.append((stat.S_IMODE(os.stat(a20).st_mode), stat.S_IMODE(os.stat(b20).st_mode), list(cmd)))
            return real_run(cmd, *a, **k)
        for src_m, dest_m in ((0o700, 0o755), (0o755, 0o700), (0o750, 0o705)):
            os.chmod(a20, src_m)
            os.chmod(b20, dest_m)
            subprocess.run = watching_run
            try:
                assert rsync(a20, b20, set(), False) == 0
            finally:
                subprocess.run = real_run
            during_src, during_dest, cmd = seen[-1]
            assert during_src & ~src_m == 0 and during_dest & ~dest_m == 0, (oct(during_src), oct(during_dest))
            assert stat.S_IMODE(os.stat(a20).st_mode) == src_m and stat.S_IMODE(os.stat(b20).st_mode) == dest_m
            assert "--no-owner" in cmd and "--no-group" in cmd

        # ".." after an internal dir symlink: the OS reaches a different file; refuse, never guess
        live24 = vault / ".codex" / "skills" / "demo24"
        write(live24 / "SKILL.md", "a\n")
        write(live24 / "skill.yaml", "name: demo24\nsecurity:\n  write_paths: ['.codex/skills/demo24/cache/../state.json']\n")
        write(live24 / "data" / "nested" / "keep.txt", "k")
        write(live24 / "data" / "state.json", "REAL-STATE")
        (live24 / "cache").symlink_to("data/nested")
        assert whole_dir_declared(live24) and state_paths(live24) == set()
        for spelling in (".codex/skills/./demo24/cache/../state.json", ".codex/skills//demo24/cache/../state.json",
                         ".codex/x/../skills/demo24/state.json", "./.codex/skills/demo24/cache/../state.json"):
            write(live24 / "skill.yaml", f"name: demo24\nsecurity:\n  write_paths: ['{spelling}']\n")
            assert whole_dir_declared(live24) and state_paths(live24) == set(), spelling
            write(dest_root / "demo24" / "data" / "state.json", "INSTALLED")
            assert propagate(live24, dest_root, False, vault, overwrite_dest=True) == 2, spelling
            assert (dest_root / "demo24" / "data" / "state.json").read_text() == "INSTALLED"
        write(live24 / "skill.yaml", "name: demo24\nsecurity:\n  write_paths: ['.codex/skills/./demo24/cache/../state.json']\n")
        st = stage(t / "run24", path_key(live24), live24)
        (st / "data" / "state.json").unlink()
        assert accept(t / "run24", path_key(live24), live24, False, vault) == 2
        assert (live24 / "data" / "state.json").read_text() == "REAL-STATE"
        for bad in ("cache/../state.json", "/abs/state.json", ".", ""):
            assert accept(t / "run24", path_key(live24), live24, False, vault, explicit={bad},
                          ignore_bad_manifest=True) == 2, bad
            assert propagate(live24, dest_root, False, vault, explicit={bad}) == 2, bad
        assert accept(t / "run24", path_key(live24), live24, False, vault, explicit={"data/state.json"},
                      ignore_bad_manifest=True) == 0
        assert (live24 / "data" / "state.json").read_text() == "REAL-STATE"

        # ".." that lexically leaves the skill but physically lands back in it through a symlink
        live25 = vault / ".codex" / "skills" / "demo25"
        write(live25 / "SKILL.md", "a\n")
        write(live25 / "skill.yaml", "name: demo25\nsecurity:\n  write_paths: ['.codex/skills/demo25/cache/../../state.json']\n")
        write(live25 / "data" / "nested" / "k.txt", "k")
        write(live25 / "state.json", "REAL")
        (live25 / "cache").symlink_to("data/nested")
        assert whole_dir_declared(live25) and state_paths(live25) == set()
        st = stage(t / "run25", path_key(live25), live25)
        (st / "state.json").unlink()
        assert accept(t / "run25", path_key(live25), live25, False, vault) == 2
        assert (live25 / "state.json").read_text() == "REAL"

        # a protected file hard-linked to an unprotected one: refuse until the alias is protected
        live26 = vault / ".codex" / "skills" / "demo26"
        write(live26 / "SKILL.md", "a\n")
        write(live26 / "skill.yaml", "name: demo26\nsecurity:\n  write_paths: ['.codex/skills/demo26/cache/state.json']\n")
        write(live26 / "cache" / "state.json", "SECRET")
        os.chmod(live26 / "cache" / "state.json", 0o600)
        (live26 / "assets").mkdir()
        os.link(live26 / "cache" / "state.json", live26 / "assets" / "sample.json")
        assert linked_state([live26], {"cache/state.json"}) == ["demo26: assets/sample.json is a hard link to protected cache/state.json"]
        st = stage(t / "run26", path_key(live26), live26)
        os.chmod(st / "assets" / "sample.json", 0o644)
        assert accept(t / "run26", path_key(live26), live26, False, vault) == 2
        assert stat.S_IMODE(os.stat(live26 / "cache" / "state.json").st_mode) == 0o600
        assert linked_state([live26], {"cache/state.json", "assets/sample.json"}) == []

        # the source manifest may name the destination's absolute path: still protected there
        src27 = vault / ".codex" / "skills" / "demo27"
        write(src27 / "SKILL.md", "v2\n")
        write(src27 / "skill.yaml", "name: demo27\nsecurity:\n  write_paths: ['"
              + str(dest_root / "demo27") + "/last_run.json']\n")
        write(dest_root / "demo27" / "SKILL.md", "v1\n")
        write(dest_root / "demo27" / "skill.yaml", "name: demo27\nsecurity:\n  write_paths: ['outputs/']\n")
        write(dest_root / "demo27" / "last_run.json", "INSTALLED")
        assert state_paths(src27) == set() and state_paths(src27, dest_root / "demo27") == {"last_run.json"}
        assert propagate(src27, dest_root, False, vault, overwrite_dest=True) == 0
        assert (dest_root / "demo27" / "last_run.json").read_text() == "INSTALLED"

        # an absolute declaration keeps its logical path, so a symlinked state alias is refused
        live28 = vault / ".codex" / "skills" / "demo28"
        write(live28 / "SKILL.md", "a\n")
        write(live28 / "data" / "state.json", "S")
        (live28 / "state.json").symlink_to("data/state.json")
        write(live28 / "skill.yaml", "name: demo28\nsecurity:\n  write_paths: ['" + str(live28) + "/state.json']\n")
        assert state_paths(live28) == {"state.json"}
        assert linked_state([live28], state_paths(live28))
        st = stage(t / "run28", path_key(live28), live28)
        (st / "state.json").unlink()
        assert accept(t / "run28", path_key(live28), live28, False, vault) == 2
        assert (live28 / "state.json").is_symlink()
        write(live28 / "skill.yaml", "name: demo28\nsecurity:\n  write_paths: ['" + str(live28) + "/x/../state.json']\n")
        assert whole_dir_declared(live28)

        # a relative dest-root containing ":" stays local (rsync would read "host:path" as remote)
        src30 = vault / ".codex" / "skills" / "demo30"
        write(src30 / "SKILL.md", "colon-safe\n")
        write(src30 / "skill.yaml", "name: demo30\nsecurity:\n  write_paths: ['outputs/']\n")
        here = os.getcwd()
        os.chdir(t)
        try:
            assert propagate(src30, Path("backup:skills"), False, vault) == 0
        finally:
            os.chdir(here)
        assert (t / "backup:skills" / "demo30" / "SKILL.md").read_text() == "colon-safe\n"

        # overlapping source/destination trees are refused before anything is created
        assert propagate(src27, src27 / "test-install", False, vault) == 2
        assert not (src27 / "test-install").exists()
        assert propagate(src27, src27.parent, False, vault) == 2

        # review shows mode and symlink-target changes that a text diff would hide; state excluded
        rv_live, rv_st = t / "rv" / "live", t / "rv" / "staging"
        for d_ in (rv_live, rv_st):
            write(d_ / "a.txt", "same\n")
            write(d_ / "b.txt", "x\n")
            write(d_ / "c.txt", "x\n")
            write(d_ / "state.json", "S" if d_ == rv_live else "STALE")
        os.chmod(rv_live / "a.txt", 0o600)
        os.chmod(rv_st / "a.txt", 0o644)
        (rv_live / "ln").symlink_to("b.txt")
        (rv_st / "ln").symlink_to("c.txt")
        write(rv_st / "a.txt", "same\nnew line\n")
        changes = "\n".join(review(rv_live, rv_st, {"state.json"}))
        assert "mode: a.txt 0o600 -> 0o644" in changes and "symlink target: ln b.txt -> c.txt" in changes
        assert "+new line" in changes and "state.json" not in changes
        write(rv_st / "evals" / "trigger.json", '[{"query": "q1", "should_trigger": true}]\n')
        (rv_live / "b.txt").unlink()
        (rv_st / "b.txt").unlink()
        write(rv_live / "gone.py", "print('old')\n")
        changes = "\n".join(review(rv_live, rv_st, {"state.json"}))
        assert "added: evals/trigger.json (file" in changes and '+[{"query": "q1"' in changes
        assert "removed: gone.py (file" in changes and "-print('old')" in changes

        # a named pipe would block a read: refused before anything is recorded or copied
        live29 = vault / ".codex" / "skills" / "demo29"
        write(live29 / "SKILL.md", "a\n")
        write(live29 / "skill.yaml", "name: demo29\nsecurity:\n  write_paths: ['outputs/']\n")
        st = stage(t / "run29", path_key(live29), live29)
        write(st / "SKILL.md", "b\n")
        os.mkfifo(live29 / "events.fifo")
        assert special_entries([live29], set()) == ["demo29: events.fifo"]
        assert accept(t / "run29", path_key(live29), live29, False, vault) == 2
        assert (live29 / "SKILL.md").read_text() == "a\n"
        assert not (vault / "outputs" / "skill-evals" / "accepted" / f"{path_key(live29)}.json").exists()
        assert propagate(live29, dest_root, False, vault) == 2 and not (dest_root / "demo29").exists()
        assert special_entries([live29], {"events.fifo"}) == []

        # an unreadable directory is an error, never silently "equal"
        live31 = vault / ".codex" / "skills" / "demo31"
        write(live31 / "SKILL.md", "a\n")
        write(live31 / "skill.yaml", "name: demo31\nsecurity:\n  write_paths: ['outputs/']\n")
        write(live31 / "references" / "guide.md", "live\n")
        st = stage(t / "run31", path_key(live31), live31)
        write(st / "references" / "guide.md", "staged change\n")
        os.chmod(live31 / "references", 0o111)
        os.chmod(st / "references", 0o111)
        try:
            for fn in (lambda: tree_entries(live31, set()), lambda: review(live31, st, set()),
                       lambda: outward_links(live31)):
                try:
                    fn()
                    raise AssertionError("unreadable dir was silently skipped")
                except ValueError:
                    pass
            assert accept(t / "run31", path_key(live31), live31, False, vault) == 2
            assert not (vault / "outputs" / "skill-evals" / "accepted" / f"{path_key(live31)}.json").exists()
            assert propagate(live31, dest_root, False, vault) == 2 and not (dest_root / "demo31").exists()
        finally:
            os.chmod(live31 / "references", 0o755)
            os.chmod(st / "references", 0o755)
        assert (live31 / "references" / "guide.md").read_text() == "live\n"
        assert tree_entries(t / "does-not-exist", set()) == {}

        # a newly created destination takes the source root's (restrictive) mode
        src22 = vault / ".codex" / "skills" / "demo22"
        write(src22 / "SKILL.md", "private\n")
        write(src22 / "skill.yaml", "name: demo22\nsecurity:\n  write_paths: ['outputs/']\n")
        os.chmod(src22, 0o700)
        assert propagate(src22, dest_root, False, vault) == 0
        assert stat.S_IMODE(os.stat(dest_root / "demo22").st_mode) == 0o700
        os.chmod(src22, 0o755)

        # declared state reached through an internal symlink: refuse unless the target is protected
        live21 = vault / ".codex" / "skills" / "demo21"
        write(live21 / "SKILL.md", "a\n")
        write(live21 / "skill.yaml", "name: demo21\nsecurity:\n  write_paths: ['.codex/skills/demo21/cache/state.json']\n")
        write(live21 / "data" / "state.json", "LIVE-STATE")
        (live21 / "cache").symlink_to("data")
        st = stage(t / "run21", path_key(live21), live21)
        write(st / "data" / "state.json", "STAGED")
        write(st / "SKILL.md", "b\n")
        assert linked_state([live21], {"cache/state.json"})
        assert accept(t / "run21", path_key(live21), live21, False, vault) == 2
        assert (live21 / "data" / "state.json").read_text() == "LIVE-STATE"
        assert accept(t / "run21", path_key(live21), live21, False, vault, explicit={"data/state.json"},
                      ignore_bad_manifest=True) == 0
        assert (live21 / "data" / "state.json").read_text() == "LIVE-STATE" and (live21 / "SKILL.md").read_text() == "b\n"
        assert propagate(live21, dest_root, False, vault) == 2

        # a symlink inside a protected dir that aliases unprotected in-skill content is refused;
        # links leaving the skill dir (like a venv's python) and links within protected content are fine
        live23 = vault / ".codex" / "skills" / "demo23"
        write(live23 / "SKILL.md", "a\n")
        write(live23 / "skill.yaml", "name: demo23\nsecurity:\n  write_paths: ['.codex/skills/demo23/cache/']\n")
        write(live23 / "data" / "state.json", "LIVE")
        (live23 / "cache").mkdir()
        (live23 / "cache" / "py").symlink_to("/usr/bin/python3")
        write(live23 / "cache" / "real.json", "R")
        (live23 / "cache" / "inner").symlink_to("real.json")
        assert linked_state([live23], {"cache"}) == []
        (live23 / "cache" / "alias").symlink_to("../data")
        assert linked_state([live23], {"cache"}) == ["demo23: cache/alias -> unprotected data"]
        st = stage(t / "run23", path_key(live23), live23)
        write(st / "data" / "state.json", "STAGED")
        assert accept(t / "run23", path_key(live23), live23, False, vault) == 2
        assert (live23 / "data" / "state.json").read_text() == "LIVE"
        assert accept(t / "run23", path_key(live23), live23, False, vault, explicit={"data"},
                      ignore_bad_manifest=True) == 0
        assert (live23 / "data" / "state.json").read_text() == "LIVE"
    print("self-test ok")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path)
    ap.add_argument("--key")
    ap.add_argument("--live", type=Path)
    ap.add_argument("--propagate", type=Path, metavar="SKILL_DIR")
    ap.add_argument("--dest-root", type=Path, default=Path("~/.claude/skills"))
    ap.add_argument("--state-paths", type=Path, metavar="SKILL_DIR")
    ap.add_argument("--review", nargs=2, type=Path, metavar=("LIVE", "STAGING"),
                    help="print every change acceptance would apply (content, modes, links), protected state excluded")
    ap.add_argument("--same", nargs=2, type=Path, metavar=("LIVE", "COPY"),
                    help="exit 0 if COPY equals LIVE (types, modes, links, content), protected state ignored")
    ap.add_argument("--check-links", type=Path, metavar="DIR",
                    help="list symlinks that are absolute or leave DIR (exit 1 if any)")
    ap.add_argument("--identity", type=Path, metavar="LIVE_DIR",
                    help="with --check-links on a staging copy: the live skill whose runtime state is exempt")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--protect", action="append", default=[], metavar="REL_PATH",
                    help="extra runtime-state path (relative to the skill dir) to never overwrite; repeatable")
    ap.add_argument("--ignore-bad-manifest", action="store_true",
                    help="proceed although a skill.yaml can't be read; only after the user reviewed --protect")
    ap.add_argument("--overwrite-dest", action="store_true", help="propagate even if the destination has foreign changes (user-confirmed)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return 0
    if a.review:
        live_dir, staging_dir = a.review
        protect = effective_protect(live_dir.resolve(), vault_root(), set(a.protect)) | state_paths(staging_dir, live_dir.resolve())
        try:
            changes = review(live_dir, staging_dir, protect)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print("\n".join(changes) if changes else "no changes")
        return 0
    if a.same:
        live_dir, copy_dir = a.same
        protect = effective_protect(live_dir.resolve(), vault_root(), set(a.protect))
        try:
            diff = tree_diff(live_dir, copy_dir, protect | state_only_dirs([live_dir, copy_dir], protect))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print("\n".join(diff) if diff else "same")
        return 1 if diff else 0
    if a.check_links:
        ident = a.identity.resolve() if a.identity else None
        protect = (state_paths(a.check_links, ident) | (effective_protect(ident, vault_root(), set(a.protect))
                                                      if ident else set(a.protect)))
        try:
            bad = outward_links(a.check_links, protect)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print("\n".join(bad) if bad else "no outward symlinks")
        return 1 if bad else 0
    if a.state_paths:
        print("\n".join(sorted(effective_protect(a.state_paths.resolve(), vault_root(), set(a.protect)))))
        return 0
    if a.propagate:
        return propagate(a.propagate, a.dest_root, a.dry_run, vault_root(), a.overwrite_dest, set(a.protect),
                         a.ignore_bad_manifest)
    if a.run and a.key and a.live:
        return accept(a.run.resolve(), a.key, a.live, a.dry_run, vault_root(), set(a.protect),
                      a.ignore_bad_manifest)
    ap.error("give --run/--key/--live, --propagate, --state-paths or --self-test")
    return 2


if __name__ == "__main__":
    sys.exit(main())
