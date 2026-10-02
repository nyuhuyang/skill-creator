# vault-skill-creator

A `skill-creator` skill for an Obsidian vault used with Claude Code and Codex. It creates new skills and updates and tests existing ones, one skill at a time or a whole skills folder at once.

It is a thin layer over Anthropic's official `skill-creator`, which still handles evals, benchmarks and description tuning. On top of that it adds:

- **Authoring rules**: `skill-creator/references/best-practices.md` covers contents lists on long reference files, references one level deep, degrees of freedom, checklists with capped retries, and explicit install lines.
- **Update workflow**: `skill-creator/references/updating.md`. Edits go to staging copies. A dry-run report comes first, then one consolidated review where each skill is accepted or rejected. Tests run in three tiers:
  - T0: static checks.
  - T1: trigger tests, run only after confirming the installed copy of the skill is hidden.
  - T2: behavioral tests in a disposable mini-vault.
- **Scripts** in `skill-creator/scripts/`:
  - `lint_skill.py` checks skill structure and can insert contents lists.
  - `accept_skill.py` applies an accepted staged update and propagates it without clobbering runtime state or foreign edits.
  - `t1_check.py` validates a trigger-test run before it is scored.
  - `validate_skill_manifest.py` validates the `skill.yaml` manifest.

Requirements: Python ≥ 3.10 with PyYAML, and `rsync`. T1/T2 tests also need the `claude` CLI and a local copy of the official skill-creator.

Self-tests:

```bash
python3 skill-creator/scripts/lint_skill.py --self-test
python3 skill-creator/scripts/accept_skill.py --self-test
python3 skill-creator/scripts/t1_check.py --self-test
```

This repo mirrors `.codex/skills/skill-creator/` from the vault, which is the source of truth.
