# Skill authoring best practices (overlay)

Rules the official skill-creator misses or contradicts. Where they conflict with it, this file wins.
Source: platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices and
code.claude.com/docs/en/skills (checked 2026-10-01). Prune a rule once upstream matches it.

## Contents
- Layout: contents lists, one-level references
- Degrees of freedom
- Checklists and feedback loops
- Dependencies
- Models
- Frontmatter keys
- Eval workspace

## Layout: contents lists, one-level references

- Any reference `.md` (not SKILL.md) over 100 lines starts with a `## Contents` list (official says
  300 — too late). Why: agents often preview long files with partial reads; the list shows full scope.
- SKILL.md mentions every supporting `.md` directly (link or inline path). No file reachable only
  through another reference — overrides the official "add another layer of hierarchy" advice.
  Why: files found indirectly tend to be read partially. Cross-links between files SKILL.md already
  mentions are fine.
- SKILL.md body ≤500 lines; split by domain (`references/finance.md`, `references/sales.md`) so
  irrelevant parts never load. Put the most important instructions near the top.

## Degrees of freedom

Decide per step by asking "what breaks if the agent does this differently?"
- Nothing much → high freedom: state the goal in prose.
- Preferred shape, some variation fine → medium: template or script with parameters.
- Fragile, irreversible, money/data deletion, strict order → low: a script plus the exact command,
  "do not change flags". Low freedom means a script, not more prose.
One skill can mix levels (e.g. drafting = high, creating the invoice = low).

## Checklists and feedback loops

- Only when order matters: give a copyable checklist the agent ticks off in its reply.
- Verification steps name where to go back: "if X fails, return to step N". Cap retries
  (default 2), then stop and report what failed — never loop silently.
- Quality-critical output: validate → fix → repeat until it passes. The validator can be a script or
  a document (style guide, brand voice). When a draft fails for a reason the document lacks, propose
  a new rule at the end of the run; add it only after the user approves.

## Dependencies

Never assume a package or CLI is installed. Put the install line next to each script's usage, e.g.
"Requires `pip install pypdf`". State whether a script is to be run (default) or read.

## Models

- Write for the weakest model the skill must support; drop CAPS/"MUST" scaffolding stronger models
  don't need. Explain why instead.
- If the skill must work on more than one model tier, test behavior (not just triggering) on each:
  same eval prompts + assertions, run with the skill per model (Claude Code: subagent `model`
  override or `claude -p --model <id>`), graded with the official `agents/grader.md`. Official
  `run_eval.py` only measures description triggering.
- Record intended models as a string: `metadata: {intended-models: "haiku, sonnet"}` (the spec
  requires string metadata values). Do not use frontmatter `model:` for
  this — in Claude Code it switches the model at runtime, and upload validators reject it.

## Frontmatter keys

- Claude Code-only skill: any Claude Code key is valid (`when_to_use`, `argument-hint`,
  `arguments`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `disallowed-tools`,
  `model`, `effort`, `context`, `agent`, `background`, `hooks`, `paths`, `shell`). `description` +
  `when_to_use` ≤1,536 chars. Never name a skill (directory or `name`) `synced` or
  `anthropic-skills` in any case — Claude Code reserves them and will not load it.
- Skill for claude.ai upload / Skills API: only `name`, `description`, `license`, `compatibility`,
  `metadata`, `allowed-tools`; description ≤1,024 chars. Check with `lint_skill.py --upload`.
- Do not use official `quick_validate.py` on Claude Code skills; it applies the upload rules.

## Eval workspace

Put eval runs in `outputs/skill-evals/<skill-name>/` (vault root), never next to the skill under
`.codex/skills/` — the registry would treat it as a broken skill and sync it into `~/.claude/skills`.
