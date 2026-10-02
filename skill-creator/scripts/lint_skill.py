#!/usr/bin/env python3
"""Lint skill structure against references/best-practices.md.

Usage (cwd = vault root):
  .venv/bin/python3 .codex/skills/skill-creator/scripts/lint_skill.py .codex/skills/<name>
  .venv/bin/python3 .codex/skills/skill-creator/scripts/lint_skill.py --all [--upload]
  .venv/bin/python3 .codex/skills/skill-creator/scripts/lint_skill.py --self-test

Output: `path:line severity rule message`. Exit 1 if any error, else 0.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
import os
import stat
from pathlib import Path
from urllib.parse import unquote

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Run with the repo virtualenv: "
        ".venv/bin/python3 .codex/skills/skill-creator/scripts/lint_skill.py"
    ) from exc

CLAUDE_CODE_KEYS = {
    "name", "description", "when_to_use", "argument-hint", "arguments",
    "disable-model-invocation", "user-invocable", "allowed-tools", "disallowed-tools",
    "model", "effort", "context", "agent", "background", "hooks", "paths", "shell",
    "metadata", "license", "compatibility",
}
UPLOAD_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
CONTENTS_RE = re.compile(r"^#{1,3}\s*(contents|table of contents|目录|toc)\b", re.I)
# [x](<a b.md>) or [x](a.md) or [x](api(v2).md) — one level of balanced parens
LINK_TARGET_RE = re.compile(r"\]\(\s*(?:<([^>]+)>|((?:[^()\s]|\([^()\s]*\))+))")
CODE_SPAN_RE = re.compile(r"`([^`\n]+\.md)`")  # `references/setup guide.md`
MD_PATH_RE = re.compile(r"(?<![\w./-])[\w./-]*\w\.md\b")  # bare paths, whole tokens only
URL_RE = re.compile(r"\w+://\S+")
XML_TAG_RE = re.compile(r"<[^>]+>")
RESERVED_NAME_WORDS = ("anthropic", "claude")  # upload spec
RESERVED_SKILL_NAMES = {"synced", "anthropic-skills"}  # Claude Code
SKIP_DIRS = {"scripts", "assets", "evals"}  # run/used, not read as references
MAX_BODY_LINES = 500
TOC_THRESHOLD = 100
TOC_WINDOW = 30  # contents heading must appear within the first N lines


def split_frontmatter(text: str) -> tuple[dict | None, int, str | None]:
    """Return (frontmatter, body_start_index, error)."""
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None, 0, "missing YAML frontmatter"
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":  # column 0 only; indented --- is block-scalar content
            try:
                fm = yaml.safe_load("\n".join(lines[1:i])) or {}
            except (yaml.YAMLError, ValueError) as exc:  # ValueError: e.g. invalid date 2026-13-01
                return None, i + 1, f"invalid YAML: {exc}".splitlines()[0]
            if not isinstance(fm, dict):
                return None, i + 1, "frontmatter is not a mapping"
            return fm, i + 1, None
    return None, 0, "unterminated frontmatter"


VENDOR_DIRS = {"node_modules", "__pycache__"}  # excluded at any depth, like the official packager


def packaged(p: Path, skill_dir: Path) -> bool:
    """True if the official packager would ship this file."""
    parts = p.relative_to(skill_dir).parts
    return parts[0] != "evals" and not VENDOR_DIRS & set(parts)


def scan_md(skill_dir: Path) -> tuple[list[Path], list[str]]:
    """Every *.md entry under the skill (symlinked dirs not followed) and the directories that
    couldn't be listed. Hidden dirs, node_modules, __pycache__ and the top-level evals/ are
    pruned (never referenced or packaged), so errors there don't count."""
    found, errors = [], []

    def onerr(err: OSError) -> None:
        if not isinstance(err, FileNotFoundError):
            errors.append(f"{err.filename}: {err.strerror or err}")

    for dirpath, dnames, fnames in os.walk(skill_dir, onerror=onerr):
        found += [Path(dirpath) / n for n in fnames + dnames if n.endswith(".md")]
        top = Path(dirpath) == skill_dir
        dnames[:] = [d for d in dnames if not d.startswith(".") and d not in ("node_modules", "__pycache__")
                     and not (top and d == "evals") and not os.path.islink(os.path.join(dirpath, d))]
    return sorted(found), errors


def reference_files(skill_dir: Path) -> list[Path]:
    return sorted(
        p for p in scan_md(skill_dir)[0]
        if p.name != "SKILL.md"
        and packaged(p, skill_dir)
        and p.relative_to(skill_dir).parts[0] not in SKIP_DIRS
        and not any(part.startswith(".") for part in p.relative_to(skill_dir).parts)
    )


def direct_mentions(skill_md: str, skill_dir: Path) -> set[Path]:
    """Files SKILL.md names directly by a path that resolves from the skill dir
    (markdown link target, inline code or bare path). No basename guessing."""
    root = skill_dir.resolve()
    tokens = [unquote(a or b) for a, b in LINK_TARGET_RE.findall(skill_md)]  # setup%20guide.md
    tokens += CODE_SPAN_RE.findall(skill_md)
    # bare paths: first blank out URLs, code spans and link targets already handled above,
    # so `references/setup guide.md` doesn't also yield a stray `guide.md`
    rest = LINK_TARGET_RE.sub("](", CODE_SPAN_RE.sub(" ", URL_RE.sub(" ", skill_md)))
    tokens += MD_PATH_RE.findall(rest)
    vault_prefix = f".codex/skills/{skill_dir.name}/"
    found: set[Path] = set()
    for token in tokens:
        token = token.split("#", 1)[0].strip()
        if "://" in token:
            continue
        if token.startswith(vault_prefix):  # vault-rooted local path
            token = token[len(vault_prefix):]
        cand = (root / token).resolve()
        if cand.is_relative_to(root) and cand.is_file():
            found.add(cand)
    return found


def read_regular(path: Path) -> tuple[list[str], str | None]:
    """Lines of a regular text file, or (no lines, reason). A FIFO, socket, device or directory
    is never opened (opening a FIFO would block)."""
    try:
        mode = os.stat(path).st_mode  # follows a symlink to its target
    except OSError as exc:
        return [], f"cannot stat: {exc.strerror or exc}"
    if not stat.S_ISREG(mode):
        return [], "not a regular file (fifo/socket/device/directory); not opened"
    try:
        return path.read_text(encoding="utf-8").splitlines(), None
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"cannot read as UTF-8 text: {exc}"


def lint(skill_dir: Path, upload: bool = False) -> list[tuple[str, int, str, str, str]]:
    findings: list[tuple[str, int, str, str, str]] = []

    def add(path: Path, line: int, sev: str, rule: str, msg: str) -> None:
        findings.append((str(path), line, sev, rule, msg))

    skill_md_path = skill_dir / "SKILL.md"
    if not os.path.lexists(skill_md_path):
        add(skill_dir, 0, "error", "skill-md", "SKILL.md missing")
        return findings
    md_lines, why = read_regular(skill_md_path)
    if why:
        add(skill_md_path, 1, "error", "unreadable", why)
        return findings
    text = "\n".join(md_lines) + "\n"
    fm, body_start, err = split_frontmatter(text)
    if err:
        add(skill_md_path, 1, "error", "frontmatter", err)
        fm = {}

    # Claude Code reserves these (any case) for claude.ai-synced skills; local ones never load
    for label, value in (("directory", skill_dir.name), ("name", fm.get("name"))):
        if isinstance(value, str) and value.strip().lower() in RESERVED_SKILL_NAMES:
            add(skill_md_path, 1, "error", "reserved-name", f"{label} {value!r} is reserved by Claude Code")

    bad_keys = [k for k in fm if not isinstance(k, str)]
    if bad_keys:
        add(skill_md_path, 1, "error", "frontmatter-keys", f"non-string keys: {bad_keys!r}")
        fm = {k: v for k, v in fm.items() if isinstance(k, str)}

    name = fm.get("name")
    # Claude Code alone would default name to the dir, but vault skills also serve Codex and uploads,
    # which both require it.
    if not err and not (isinstance(name, str) and name.strip()):
        add(skill_md_path, 1, "error", "name", "non-empty name required (Codex and upload spec)")
    elif name is not None and (not isinstance(name, str) or not NAME_RE.fullmatch(name) or len(name) > 64):
        add(skill_md_path, 1, "error", "name", f"name {name!r} must be kebab-case, <=64 chars")
    elif upload and isinstance(name, str) and any(w in name for w in RESERVED_NAME_WORDS):
        add(skill_md_path, 1, "error", "name", f"name {name!r} contains a reserved word")
    elif upload and name != skill_dir.name:
        add(skill_md_path, 1, "error", "name", f"upload spec requires name {name!r} == directory {skill_dir.name!r}")
    if upload:  # claude.ai / Skills API accept exactly one SKILL.md per package
        for extra in sorted(p for p in scan_md(skill_dir)[0]
                            if p.name == "SKILL.md" and p != skill_md_path and packaged(p, skill_dir)):
            add(extra, 1, "error", "nested-skill-md", "upload spec allows only the root SKILL.md")
    desc = fm.get("description")
    if not err and (not isinstance(desc, str) or not desc.strip()):
        add(skill_md_path, 1, "error", "description", "description missing or empty")
    desc = desc if isinstance(desc, str) else ""
    if upload and XML_TAG_RE.search(desc):
        add(skill_md_path, 1, "error", "description", "upload spec forbids XML tags in description")
    allowed = UPLOAD_KEYS if upload else CLAUDE_CODE_KEYS
    unknown = sorted(set(fm) - allowed)
    if unknown:
        mode = "upload spec" if upload else "Claude Code"
        add(skill_md_path, 1, "error" if upload else "warning", "frontmatter-keys",
            f"keys not valid for {mode}: {', '.join(unknown)}")
    if upload:  # optional upload fields: validate type whenever the key is present
        meta = fm.get("metadata")
        if "metadata" in fm and not (
            isinstance(meta, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items())
        ):
            add(skill_md_path, 1, "error", "metadata", "metadata must map strings to strings")
        comp = fm.get("compatibility")
        if "compatibility" in fm and not (isinstance(comp, str) and 1 <= len(comp) <= 500):
            add(skill_md_path, 1, "error", "compatibility", "compatibility must be a 1-500 char string")
        if "license" in fm and not isinstance(fm["license"], str):
            add(skill_md_path, 1, "error", "license", "license must be a string")
        if "allowed-tools" in fm and not isinstance(fm["allowed-tools"], str):  # lists are Claude Code-only
            add(skill_md_path, 1, "error", "allowed-tools", "upload spec requires a space-separated string")
    if upload and len(desc) > 1024:
        add(skill_md_path, 1, "error", "description-length", f"description {len(desc)} > 1024 chars")
    wtu = fm.get("when_to_use") if isinstance(fm.get("when_to_use"), str) else ""
    if not upload and len(desc) + len(wtu) > 1536:
        add(skill_md_path, 1, "error", "description-length",
            f"description + when_to_use {len(desc) + len(wtu)} > 1536 chars")

    body_lines = len(text.splitlines()) - body_start
    if body_lines > MAX_BODY_LINES:
        add(skill_md_path, body_start + 1, "error", "body-length",
            f"SKILL.md body {body_lines} lines > {MAX_BODY_LINES}")

    for err in scan_md(skill_dir)[1]:  # an unlistable dir would hide references: never "clean"
        add(skill_dir, 0, "error", "unreadable-dir", err)
    refs = reference_files(skill_dir)
    mentioned = direct_mentions(text, skill_dir)
    for ref in refs:
        lines, why = read_regular(ref)
        if why:
            add(ref, 1, "error", "unreadable", why)
            continue
        if len(lines) > TOC_THRESHOLD and not has_contents(lines, 0, TOC_WINDOW):
            add(ref, 1, "error", "toc",
                f"{len(lines)} lines with no contents heading in first {TOC_WINDOW} lines")
        if ref.resolve() not in mentioned:
            add(ref, 1, "error", "indirect-reference",
                "not mentioned directly in SKILL.md (link it one level deep or delete it)")
    return findings


def skill_dirs(skills_root: Path) -> list[Path]:
    """Skill dirs directly under a skills root; skips symlinks (maintained elsewhere) and
    reserved names such as `synced`."""
    return sorted(
        d for d in skills_root.iterdir()
        if d.is_dir() and not d.is_symlink() and d.name.lower() not in RESERVED_SKILL_NAMES
        and (d / "SKILL.md").is_file()
    )


H2_RE = re.compile(r"^##\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def fence_mask(lines: list[str]) -> list[bool]:
    """True for lines inside (or delimiting) a fenced code block. A fence closes only with the
    same character and at least the opening length, as in CommonMark."""
    mask, opener = [], None
    for l in lines:
        m = FENCE_RE.match(l)
        if opener is None:
            if m:
                opener = m.group(1)
            mask.append(opener is not None)
        else:
            mask.append(True)
            if m and m.group(1)[0] == opener[0] and len(m.group(1)) >= len(opener) \
                    and not l.strip()[len(m.group(1)):].strip():
                opener = None
    return mask


def has_contents(lines: list[str], start: int = 0, end: int | None = None) -> bool:
    mask = fence_mask(lines)
    return any(CONTENTS_RE.match(lines[i]) and not mask[i] for i in range(start, min(end or len(lines), len(lines))))


def through_symlink(path: Path, within: Path) -> str | None:
    """Why editing `path` could touch something outside `within`, or None if it is safe.
    Checks the path exactly as it will be opened: no ".." components (they would be collapsed by
    normalisation but followed by the OS through symlinks), `within` itself not a symlink, and no
    symlink among the components from `within` down to the file."""
    if ".." in Path(path).parts or ".." in Path(within).parts:
        return "path contains '..'"
    root = Path(within) if Path(within).is_absolute() else Path.cwd() / within
    p = Path(path) if Path(path).is_absolute() else Path.cwd() / path
    if root.is_symlink():
        return f"{within} is a symlink"
    if p.parts[:len(root.parts)] != root.parts or p == root:
        return f"outside {within}"
    cur = root
    for part in p.parts[len(root.parts):]:
        cur = cur / part
        if cur.is_symlink():
            return f"{cur} is a symlink"
    if not os.path.realpath(p).startswith(os.path.realpath(root) + os.sep):
        return f"resolves outside {within}"
    return None


def fix_toc(path: Path, within: Path | None = None) -> str:
    """Insert a `## Contents` list into a long reference file if it lacks one in the first
    TOC_WINDOW lines. Idempotent; refuses (returns a 'manual' message) rather than guess.
    Never edits through a symlink or outside `within` (default: the current directory)."""
    why = through_symlink(path, within or Path.cwd())
    if why:
        return f"refused: {why}"
    rel = Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(within or Path.cwd())))
    if (path.suffix.lower() != ".md" or path.name.upper() == "SKILL.MD"
            or (len(rel.parts) > 1 and rel.parts[0] in SKIP_DIRS)
            or any(part.startswith(".") for part in rel.parts)
            or {"node_modules", "__pycache__"} & set(rel.parts)):
        return "refused: not a reference .md file (SKILL.md, scripts/, assets/, evals/ and non-.md are never edited)"
    _, why = read_regular(path)
    if why:
        return f"refused: {why}"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(lines) <= TOC_THRESHOLD:
        return "skip: not over 100 lines"
    if has_contents(lines, 0, TOC_WINDOW):
        return "ok: has contents list"
    if has_contents(lines, TOC_WINDOW):
        return "late contents list: move it to the top manually"
    body = 0
    if lines[0].rstrip() == "---":  # never insert before or inside frontmatter
        ends = [i for i in range(1, len(lines)) if lines[i].rstrip() == "---"]
        if not ends:
            return "manual fix needed: unterminated frontmatter"
        body = ends[0] + 1
    mask = fence_mask(lines)
    insert = body
    for i in range(body, min(body + 5, len(lines))):
        if lines[i].startswith("# ") and not mask[i]:
            insert = i + 1
            break
    headings = [m.group(1) for i in range(body, len(lines))
                if not mask[i] and (m := H2_RE.match(lines[i]))]
    if not headings:
        return "manual fix needed: no ## headings to list"
    if insert + 2 > TOC_WINDOW:  # 1-based line of the inserted heading
        return "manual fix needed: contents would land beyond the first 30 lines"
    if insert and not lines[insert - 1].endswith("\n"):
        lines[insert - 1] += "\n"
    block = ["\n", "## Contents\n"] + [f"- {h}\n" for h in headings] + ["\n"]
    path.write_text("".join(lines[:insert] + block + lines[insert:]), encoding="utf-8")
    return f"inserted contents list ({len(headings)} entries) at line {insert + 2}"


def trigger_text(skill_dir: Path) -> str:
    """What Claude Code lists for triggering: description, plus when_to_use if present."""
    md_lines, why = read_regular(skill_dir / "SKILL.md")
    if why:
        return ""
    fm, _, err = split_frontmatter("\n".join(md_lines) + "\n")
    if err or not fm:
        return ""
    parts = [fm.get("description"), fm.get("when_to_use")]
    return " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


def self_test() -> None:
    def write(p: Path, s: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(s, encoding="utf-8")

    def rules(d: Path, upload: bool = False) -> list[tuple[str, str]]:
        return [(Path(f[0]).name, f[3]) for f in lint(d, upload) if f[2] == "error"]

    long_body = "\n".join(f"line {i}" for i in range(101))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        s = root / ".codex" / "skills" / "demo"
        write(s / "SKILL.md",
              "---\nname: demo\ndescription: Demo skill.\nwhen_to_use: When testing.\n---\n"
              "Read `references/a.md` and [b](references/b.md).\n")
        write(s / "references" / "a.md", "# A\nSee [b](b.md) and [c](c.md).\n")  # cross-link a->b ok
        write(s / "references" / "b.md", "# B\n## Contents\n- x\n" + long_body)
        write(s / "references" / "c.md", "# C\n" + long_body)  # indirect + no contents
        write(root / ".codex" / "skills" / "demo-workspace" / "notes.md", "no SKILL.md here\n")

        got = rules(s)
        assert ("c.md", "toc") in got, got
        assert ("c.md", "indirect-reference") in got, got
        assert not any(n in {"a.md", "b.md", "SKILL.md"} for n, _ in got), got
        assert [d.name for d in skill_dirs(root / ".codex" / "skills")] == ["demo"]

        write(s / "SKILL.md",
              "---\nname: demo\ndescription: Demo skill.\nmodel: haiku\n---\n"
              "Read `references/a.md`, `references/b.md`, `references/c.md`.\n")
        assert ("SKILL.md", "frontmatter-keys") in rules(s, upload=True)
        assert ("SKILL.md", "frontmatter-keys") not in rules(s)

        # angle-bracket link with spaces counts; bare basename that doesn't resolve does not
        write(s / "SKILL.md",
              "---\nname: demo\ndescription: Demo skill.\n---\n"
              "See [setup](<references/setup guide.md>). Also guide.md.\n")
        for f in (s / "references").iterdir():
            f.unlink()
        write(s / "references" / "setup guide.md", "# Setup\n")
        write(s / "references" / "guide.md", "# Guide\n")
        got = rules(s)
        assert ("setup guide.md", "indirect-reference") not in got, got
        assert ("guide.md", "indirect-reference") in got, got

        # upload: name required, no XML in description; non-string key reported, not a crash
        write(s / "SKILL.md", "---\ndescription: Uses <tool> tags.\n42: x\n---\nbody\n")
        got = rules(s, upload=True)
        assert ("SKILL.md", "name") in got, got
        assert ("SKILL.md", "description") in got, got
        assert ("SKILL.md", "frontmatter-keys") in got, got
        assert ("SKILL.md", "name") in rules(s)  # required for Codex too
        write(s / "SKILL.md", "---\nname:\ndescription: d\n---\nx\n")
        assert ("SKILL.md", "name") in rules(s)  # explicit null

        # malformed frontmatter under --upload: reported, no crash
        write(s / "SKILL.md", "no frontmatter here\n")
        assert ("SKILL.md", "frontmatter") in rules(s, upload=True)

        # spaced inline-code path counts; URL containing the skill path does not
        write(s / "SKILL.md",
              "---\nname: demo\ndescription: Demo skill.\n---\n"
              "Read `references/setup guide.md`.\n"
              "Docs: https://example.com/.codex/skills/demo/references/guide.md\n")
        got = rules(s)
        assert ("setup guide.md", "indirect-reference") not in got, got
        assert ("guide.md", "indirect-reference") in got, got

        # invalid timestamp: reported per skill, --all keeps going to the next skill
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\nmetadata: {created: 2026-13-01}\n---\nx\n")
        z = root / ".codex" / "skills" / "zeta"
        write(z / "SKILL.md", "---\nname: zeta\ndescription: d\n---\nx\n")
        assert ("SKILL.md", "frontmatter") in rules(s)
        assert [f for d in skill_dirs(root / ".codex" / "skills") for f in lint(d)] is not None  # no exception
        assert lint(z) == []

        # metadata values must be strings for upload; recommended scalar form passes
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\nmetadata: {intended-models: [haiku]}\n---\nx\n")
        assert ("SKILL.md", "metadata") in rules(s, upload=True)
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\nmetadata: {intended-models: \"haiku, sonnet\"}\n---\nx\n")
        assert ("SKILL.md", "metadata") not in rules(s, upload=True)

        # other optional upload fields are type-checked when present
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\nmetadata:\ncompatibility: [python3]\n---\nx\n")
        got = rules(s, upload=True)
        assert ("SKILL.md", "metadata") in got and ("SKILL.md", "compatibility") in got, got
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\ncompatibility: " + "x" * 501 + "\n---\nx\n")
        assert ("SKILL.md", "compatibility") in rules(s, upload=True)
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\ncompatibility: Requires python3\n---\nx\n")
        assert [r for r in rules(s, upload=True) if r[0] == "SKILL.md"] == []

        # allowed-tools: list ok for Claude Code, string required for upload
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\nallowed-tools: [Read, Grep]\n---\nx\n")
        assert ("SKILL.md", "allowed-tools") in rules(s, upload=True)
        assert ("SKILL.md", "allowed-tools") not in rules(s)

        # reserved Claude Code names, as directory or frontmatter name, any case
        write(s / "SKILL.md", "---\nname: Anthropic-Skills\ndescription: d\n---\nx\n")
        assert ("SKILL.md", "reserved-name") in rules(s)
        r = root / ".codex" / "skills" / "synced"
        write(r / "SKILL.md", "---\nname: ok-name\ndescription: d\n---\nx\n")
        assert ("SKILL.md", "reserved-name") in rules(r)

        # indented --- inside a block scalar does not end frontmatter
        write(s / "SKILL.md", "---\nname: demo\ndescription: |\n  text\n  ---\nmodel: haiku\n---\nx\n")
        assert ("SKILL.md", "frontmatter-keys") in rules(s, upload=True)

        # percent-encoded link target resolves to the real file
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\n---\n[s](references/setup%20guide.md)\n")
        assert ("setup guide.md", "indirect-reference") not in rules(s)

        # block-scalar names carry a trailing newline: not kebab-case; reserved check still fires
        write(s / "SKILL.md", "---\nname: |\n  demo\ndescription: d\n---\nx\n")
        assert ("SKILL.md", "name") in rules(s)
        write(s / "SKILL.md", "---\nname: |\n  synced\ndescription: d\n---\nx\n")
        assert ("SKILL.md", "reserved-name") in rules(s)

        # spaced code path must not also "mention" an unrelated root file sharing its suffix
        write(s / "guide.md", "# Root guide\n")
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\n---\nRead `references/setup guide.md`.\n")
        got = [(Path(f[0]).relative_to(s).as_posix(), f[3]) for f in lint(s) if f[2] == "error"]
        assert ("guide.md", "indirect-reference") in got, got
        assert ("references/setup guide.md", "indirect-reference") not in got, got

        # upload: name must equal dir; nested SKILL.md rejected (Claude Code mode allows both)
        write(s / "SKILL.md", "---\nname: other\ndescription: d\n---\nx\n")
        assert ("SKILL.md", "name") in rules(s, upload=True)
        assert ("SKILL.md", "name") not in rules(s)
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\n---\nx\n")
        write(s / "references" / "nested" / "SKILL.md", "---\nname: n\ndescription: d\n---\nx\n")
        assert ("SKILL.md", "nested-skill-md") in rules(s, upload=True)
        assert ("SKILL.md", "nested-skill-md") not in rules(s)
        (s / "references" / "nested" / "SKILL.md").unlink()
        write(s / "evals" / "fixtures" / "SKILL.md", "---\nname: f\ndescription: d\n---\nx\n")
        assert ("SKILL.md", "nested-skill-md") not in rules(s, upload=True)  # evals not packaged

        # balanced parentheses in a link target
        write(s / "references" / "api(v2).md", "# API\n")
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\n---\n[API](references/api(v2).md)\n")
        assert ("api(v2).md", "indirect-reference") not in rules(s), rules(s)

        # vendored dependency docs are ignored; authored references still checked
        write(s / "scripts" / "node_modules" / "pkg" / "README.md", "# pkg\n" + long_body)
        write(s / "node_modules" / "pkg" / "README.md", "# pkg\n" + long_body)
        got = rules(s)
        assert not any(n == "README.md" for n, _ in got), got
        assert ("guide.md", "indirect-reference") in got, got

        # --root skips symlinked dirs and the reserved `synced` dir
        sroot = root / "other-root"
        write(sroot / "real" / "SKILL.md", "---\nname: real\ndescription: d\n---\nx\n")
        write(sroot / "synced" / "x" / "SKILL.md", "x\n")
        write(sroot / "synced" / "SKILL.md", "---\nname: synced\ndescription: d\n---\nx\n")
        (sroot / "linked").symlink_to(sroot / "real")
        assert [d.name for d in skill_dirs(sroot)] == ["real"]

        # --trigger-text: description + when_to_use
        write(sroot / "real" / "SKILL.md", "---\nname: real\ndescription: Does X.\nwhen_to_use: Say X.\n---\nx\n")
        assert trigger_text(sroot / "real") == "Does X. Say X."

        # --fix-toc: inserts after the H1, lint passes after, second run is a no-op
        sections = "".join(f"## Part {i}\n" + "text\n" * 20 for i in range(1, 6))
        ref = s / "references" / "long.md"
        write(ref, "# Long\n" + sections)
        assert fix_toc(ref, s).startswith("inserted contents list (5 entries)")
        text = ref.read_text()
        assert text.splitlines()[2] == "## Contents" and "- Part 5\n" in text
        assert fix_toc(ref, s) == "ok: has contents list"
        assert ref.read_text() == text
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\n---\nRead `references/long.md`.\n")
        assert ("long.md", "toc") not in rules(s)

        # late contents list -> no edit; H1 beyond the first 5 body lines -> insert at body start
        write(ref, "# Long\n" + sections + "## Contents\n- x\n")
        before = ref.read_text()
        assert fix_toc(ref, s).startswith("late contents list") and ref.read_text() == before
        write(ref, "intro\n" * 6 + "# Title\n" + sections)
        fix_toc(ref, s)
        assert ref.read_text().splitlines()[1] == "## Contents"

        # frontmatter preserved; contents inserted after it, or refused if beyond the window
        write(ref, "---\ntitle: t\n---\n# Long\n" + sections)
        fix_toc(ref, s)
        lines = ref.read_text().splitlines()
        assert lines[0] == "---" and lines[2] == "---" and lines[5] == "## Contents", lines[:7]
        write(ref, "---\n" + "k: v\n" * 30 + "---\n# Long\n" + sections)
        before = ref.read_text()
        assert fix_toc(ref, s).startswith("manual fix needed") and ref.read_text() == before

        # `## ` inside a code fence is not a heading
        write(ref, "# Long\n```\n## not a heading\n```\n" + sections)
        fix_toc(ref, s)
        assert "- not a heading" not in ref.read_text()

        # a fenced example at the top: its "# Example" is not the title, and a fenced
        # "## Contents" does not satisfy the rule; a longer closing fence is still a close
        write(ref, "````md\n# Example\n## Contents\n```\nstill inside\n````\n# Real\n" + sections)
        assert ("long.md", "toc") in rules(s)
        fix_toc(ref, s)
        lines = ref.read_text().splitlines()
        # "# Real" is beyond the first 5 lines, so contents go to the top, not inside the fence
        assert lines[1] == "## Contents" and lines[8] == "````md", lines[:10]
        assert ("long.md", "toc") not in rules(s)
        write(ref, "```\n# Example\n```\n# Real\n" + sections)  # real H1 within 5 lines
        fix_toc(ref, s)
        lines = ref.read_text().splitlines()
        assert lines[3] == "# Real" and lines[5] == "## Contents", lines[:7]

        # never edits through a symlink or outside the staging root
        original = root / "original.md"
        write(original, "# Orig\n" + sections)
        linked = s / "references" / "linked.md"
        linked.symlink_to(original)
        assert fix_toc(linked, s).startswith("refused") and "## Contents" not in original.read_text()
        (s / "refdir").symlink_to(root / "other-root")
        write(root / "other-root" / "x.md", "# X\n" + sections)
        assert fix_toc(s / "refdir" / "x.md", s).startswith("refused")
        assert fix_toc(original, s).startswith("refused: outside")

        # only reference .md files are eligible
        write(s / "scripts" / "tool.py", "# x\n" + sections)
        before = (s / "scripts" / "tool.py").read_text()
        assert fix_toc(s / "scripts" / "tool.py", s).startswith("refused: not a reference")
        assert (s / "scripts" / "tool.py").read_text() == before
        write(s / "SKILL.md", "---\nname: demo\ndescription: d\n---\n# Demo\n" + sections)
        assert fix_toc(s / "SKILL.md", s).startswith("refused: not a reference")
        write(s / "notes.txt", "# N\n" + sections)
        assert fix_toc(s / "notes.txt", s).startswith("refused: not a reference")

        # non-regular or unreadable .md entries are reported, never opened, and don't stop the run
        fifo = s / "references" / "events.md"
        os.mkfifo(fifo)
        (s / "references" / "folder.md").mkdir()
        (s / "references" / "binary.md").write_bytes(b"\xff\xfe\x00bad")
        got = [(Path(f[0]).name, f[3]) for f in lint(s) if f[2] == "error"]
        for name in ("events.md", "folder.md", "binary.md"):
            assert (name, "unreadable") in got, (name, got)
        assert fix_toc(fifo, s).startswith("refused: not a regular file")
        fifo.unlink()
        (s / "references" / "folder.md").rmdir()
        (s / "references" / "binary.md").unlink()

        # an unlistable references/ dir is an error, not a silently clean result
        write(s / "references" / "hidden-guide.md", "x\n")
        os.chmod(s / "references", 0o111)
        try:
            assert ("demo", "unreadable-dir") in [(Path(f[0]).name, f[3]) for f in lint(s)]
        finally:
            os.chmod(s / "references", 0o755)
        (s / "references" / "hidden-guide.md").unlink()

        # an undecodable SKILL.md is a finding, and trigger-text reports it instead of crashing
        bad = root / "other-root" / "badmd"
        bad.mkdir(parents=True)
        (bad / "SKILL.md").write_bytes(b"---\nname: badmd\n\xff\xfe\n---\n")
        assert [f[3] for f in lint(bad)] == ["unreadable"]
        assert trigger_text(bad) == ""

        # ".." through a symlinked runtime dir can't reach the original; a symlinked root is refused
        orig = root / "orig-skill"
        write(orig / "references" / "long.md", "# L\n" + sections)
        (orig / ".toolvenv").mkdir()
        (s / ".toolvenv").symlink_to(orig / ".toolvenv")
        sneaky = s / ".toolvenv" / ".." / "references" / "long.md"
        before = (orig / "references" / "long.md").read_text()
        assert fix_toc(sneaky, s).startswith("refused"), fix_toc(sneaky, s)
        assert (orig / "references" / "long.md").read_text() == before
        (root / "linked-root").symlink_to(s)
        assert fix_toc(root / "linked-root" / "references" / "long.md", root / "linked-root").startswith("refused")
    print("self-test ok")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("skill_dirs", nargs="*", type=Path)
    ap.add_argument("--all", action="store_true", help="same as --root .codex/skills (cwd = vault root)")
    ap.add_argument("--root", type=Path, help="lint every DIR/*/ with a SKILL.md (skips symlinks, synced)")
    ap.add_argument("--list-root", type=Path, metavar="DIR",
                    help="print the canonical path of every skill under DIR (the batch targets), then exit")
    ap.add_argument("--upload", action="store_true", help="enforce claude.ai/API upload frontmatter spec")
    ap.add_argument("--fix-toc", nargs="+", type=Path, metavar="FILE", help="insert contents lists, then exit")
    ap.add_argument("--within", type=Path, help="with --fix-toc: only edit files inside this dir (default: cwd)")
    ap.add_argument("--trigger-text", type=Path, metavar="SKILL_DIR", help="print description + when_to_use")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.list_root:
        for d in skill_dirs(args.list_root):
            print(os.path.realpath(d))
        return 0
    if args.fix_toc:
        results = [(f, fix_toc(f, args.within)) for f in args.fix_toc]
        for f, msg in results:
            print(f"{f}: {msg}")
        return 1 if any(m.startswith("refused") for _, m in results) else 0
    if args.trigger_text:
        text = trigger_text(args.trigger_text)
        if not text:
            _, why = read_regular(args.trigger_text / "SKILL.md")
            print(f"error: {why or 'no description'} in {args.trigger_text}/SKILL.md", file=sys.stderr)
            return 1
        print(text)
        return 0
    root = Path.cwd() / ".codex" / "skills" if args.all else args.root
    targets = skill_dirs(root) if root else args.skill_dirs
    if not targets:
        ap.error("give skill dirs, --root DIR or --all")
    errors = 0
    for d in targets:
        try:
            results = lint(d, args.upload)
        except Exception as exc:  # one broken skill must not hide the rest of the batch
            results = [(str(d), 0, "error", "lint-crash", f"{type(exc).__name__}: {exc}")]
        for path, line, sev, rule, msg in results:
            errors += sev == "error"
            print(f"{path}:{line} {sev} {rule} {msg}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
