---
name: skill-creator
description: Create, update and test skills in this vault, one skill or all of them at once. Use whenever the user wants a new `.codex/skills/<name>/` skill; wants to update, fix, improve, audit or batch-update existing skills against current skill best practices (e.g. "update all skills", "批量更新 skill", "audit my skills"); wants a skill fixed and tested before the change is accepted; or needs a canonical `skill.yaml` manifest.
---

# Skill Creator

Create or update repository-local skills for this vault.

This skill is not the global Codex skill scaffold. It is the vault-specific workflow that turns a local skill into an operable unit with a standard manifest and validation path.

## Paths

Vault root = `git rev-parse --show-toplevel` when cwd is inside the vault; otherwise `$KB_ROOT` if set; otherwise `~/Documents/AI_Workspace/obsidian/knowledge_base`. Skill dir = `<vault root>/.codex/skills/skill-creator/`. Bundled `references/` and `assets/` paths below resolve against the skill dir; `.codex/`, `.venv/` and `outputs/` paths resolve against the vault root. Run validator and lint commands with cwd = vault root. (Some callers inject only this text with a different cwd, so resolve before reading.)

## Required output

Every local skill needs `SKILL.md` and `skill.yaml`. It may also bundle `scripts/`, `references/`, `assets/` and `evals/` (test cases; not packaged).

## Canonical manifest

Every local skill manifest lives at `.codex/skills/<skill-name>/skill.yaml`.

Use the schema defined in `references/skill-manifest-schema.md`.
Start from `assets/skill.yaml.template`.

## Workflow

1. Normalize the skill name to lowercase hyphen-case.
2. Read the existing repo context that the skill must respect.
3. Read `references/best-practices.md`, then create or update `SKILL.md` with a concise trigger description and workflow that follows it (contents lists, one-level references, degrees of freedom, checklists with capped retries, install lines).
4. Create or update `skill.yaml` with the canonical metadata fields:
   - `purpose`
   - `trigger`
   - `inputs`
   - `outputs`
   - `dependencies`
   - `failure_modes`
   - `evaluation`
   - `schedule_eligibility`
5. Add scripts or references only when the task needs deterministic execution or nontrivial repo-specific guidance.
6. Validate with:

```bash
.venv/bin/python3 .codex/skills/skill-creator/scripts/validate_skill_manifest.py .codex/skills/<skill-name>   # or --all
```

7. Lint structure (add `--upload` if the skill will be uploaded to claude.ai or the Skills API):

```bash
.venv/bin/python3 .codex/skills/skill-creator/scripts/lint_skill.py .codex/skills/<skill-name>
```

## Evals and the official skill-creator

- Claude Code: for evals, benchmarks, A/B comparison or description optimization, use `/anthropic-skills:skill-creator`.
- Codex: if `claude` is on PATH and `ls -d ~/.claude/skills/synced/*/skill-creator | head -1` finds the official copy, read that copy's SKILL.md eval section and run cases with `claude -p`. Otherwise skip benchmarking and report "evals not run — official skill-creator unavailable in this harness".
- Put eval workspaces in `outputs/skill-evals/<skill-name>/`, never next to the skill under `.codex/skills/`. For description optimization, the official `run_eval.py` writes and then deletes command stubs under the nearest ancestor `.claude/`, which is `~/.claude/` if the vault has none. From the vault root, run `mkdir -p .claude/commands outputs/skill-evals/<skill-name>`, then confirm that `PYTHONPATH=<official skill-creator dir> python3 -c "import scripts.run_eval as m; print(m.find_project_root())"` prints the vault root. Stop if it prints anything else. Then run `PYTHONPATH=<official skill-creator dir> python3 -m scripts.run_loop ... --results-dir outputs/skill-evals/<skill-name>/ --report outputs/skill-evals/<skill-name>/report.html`.
- Where the official skill-creator's structural advice conflicts with `references/best-practices.md` (contents-list threshold, extra reference hierarchy), the reference wins. Do not use its `quick_validate.py` on Claude Code skills; use `lint_skill.py`.

## Updating and testing skills (one or many)

Most runs are updates. Read `references/updating.md` before updating any existing skill, whether one skill or a whole root. Summary:

- Edits happen on staging copies in a run dir `outputs/skill-evals/batch-XXXXXX/`. Originals stay untouched until the user accepts each skill.
- A dry-run report comes first. Apply only after the user approves it; then one consolidated review with per-skill accept or reject.
- Test tiers: T0 static (always). T1 trigger tests when the description or `when_to_use` changes; these run in a disposable project with the installed copy verified hidden. T2 behavioral tests in a disposable mini-vault, by default only for safe skills.
- Accept and propagate only with `scripts/accept_skill.py`, which locks, detects drift, and preserves runtime state such as `last_run.json`. Never use a whole-dir `rsync` or a full `build_registry.py` sync.
- Keep each skill's contract (triggers, outputs, `skill.yaml` interface) unless the user asks to change it. Every change needs a named rule from `references/best-practices.md`.

## Manifest rules

- Keep values operational, not aspirational.
- `purpose` should describe the durable job of the skill in one or two sentences.
- `trigger` should capture both user-language cues and the underlying intent.
- `inputs` should distinguish required from optional inputs.
- `outputs` should state where durable artifacts are written.
- `dependencies` should list local skills, scripts, tools, env requirements, and external services only when they are actually needed.
- `failure_modes` should describe likely breakpoints, how they are detected, and the minimum safe response.
- `evaluation` should define what a good run looks like and how it is checked.
- `schedule_eligibility` should explicitly say whether recurring automation is allowed, under what execution mode, and why.

## Repo-specific conventions

- Prefer `.venv/bin/python3` when the repo has a virtualenv.
- Keep workflow logic concise in `SKILL.md`; move detailed schema notes into references.
- Do not create extra README-style files for a skill.
- If the skill produces nontrivial knowledge artifacts, route them into `wiki/` or `outputs/` as appropriate.
- If converting an old local skill, preserve its existing behavior first; standardize metadata without opportunistic redesign.

## Minimal completion bar

A new or updated local skill is not complete until:

- `SKILL.md` exists and is triggerable
- `skill.yaml` passes validation
- `lint_skill.py` reports 0 errors
- any required scripts or references are present
- the user-visible workflow matches the manifest
