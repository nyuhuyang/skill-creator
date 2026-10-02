# skill-creator

**A skill-creator built to Claude's [Agent Skills best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices).**

It creates new Agent Skills, and updates and tests existing ones, one skill at a time or a whole skills folder at once. It enforces those best practices instead of leaving them as advice. It runs in Claude Code and Codex, and was built for an Obsidian vault that keeps its skills in `.codex/skills/`.

## Why it exists

Anthropic's official `skill-creator` is strong at evals, benchmarks and description tuning. On a few structural points, though, it misses or contradicts the best-practices guide. For example, it puts the contents-list threshold at 300 lines instead of 100, and it suggests adding more reference hierarchy where the guide says to keep references one level deep. This skill closes those gaps and hands evals to the official skill-creator.

## What it enforces (from the best-practices guide)

| Best practice | How this skill applies it |
|---|---|
| SKILL.md body under 500 lines | `lint_skill.py` checks it |
| Contents list on reference files over 100 lines | `lint_skill.py` checks it; `--fix-toc` inserts one safely |
| References one level deep from SKILL.md | `lint_skill.py` flags any reference SKILL.md doesn't mention directly |
| Match degrees of freedom to fragility | Authoring rules: fragile steps become scripts with exact commands |
| Checklists and feedback loops | Authoring rules: copyable checklists, "return to step N" with capped retries |
| Don't assume tools are installed | Authoring rules: install lines sit next to each script |
| Test with every model you plan to use | Behavioral tests per model tier; intended models recorded in `metadata` |
| Evaluation-driven development | Stored `evals/` per skill: trigger tests (T1) and behavioral tests (T2) |
| Valid frontmatter (name, description limits) | `lint_skill.py` checks it; `--upload` applies the claude.ai/API spec |

The full rule set is in `skill-creator/references/best-practices.md`.

## Updating many skills safely

`skill-creator/references/updating.md` uses one workflow for a single skill or a whole folder:

1. All edits happen on staging copies. Originals stay untouched until each skill is accepted.
2. A dry-run report comes first.
3. One consolidated review follows, where each skill is accepted or rejected.
4. Each candidate is tested in tiers:
   - **T0**: static checks.
   - **T1**: trigger tests, run only after the installed copy is verified hidden.
   - **T2**: behavioral tests in a disposable mini-vault.

## Scripts

All scripts are in `skill-creator/scripts/`.

| Script | What it does |
|---|---|
| `lint_skill.py` | Checks skill structure against the rules above. `--root DIR` covers a whole folder; `--fix-toc` inserts contents lists. |
| `accept_skill.py` | Applies an accepted staged update, or propagates a skill to `~/.claude/skills`. It takes a lock, detects drift, and never overwrites runtime state or edits made elsewhere. |
| `t1_check.py` | Checks that a trigger-test run is valid (every call decided, no errors) before it is scored. |
| `validate_skill_manifest.py` | Validates the vault's `skill.yaml` manifest. |

Requirements: Python ≥ 3.10 with PyYAML, and `rsync`. T1 and T2 also need the `claude` CLI and a local copy of the official skill-creator.

```bash
python3 skill-creator/scripts/lint_skill.py --self-test
python3 skill-creator/scripts/accept_skill.py --self-test
python3 skill-creator/scripts/t1_check.py --self-test
```

The source of truth is `.codex/skills/skill-creator/` in the vault; this repo mirrors it.
