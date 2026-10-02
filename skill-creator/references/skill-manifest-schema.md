# Skill Manifest Schema

Canonical path: `.codex/skills/<skill-name>/skill.yaml`

This schema standardizes local skill metadata for the knowledge base.

## Required top-level fields

```yaml
name: string
purpose: string
trigger:
  user_phrases: [string]
  intent_signals: [string]
inputs:
  required:
    - name: string
      description: string
  optional:
    - name: string
      description: string
outputs:
  primary:
    - path: string
      description: string
  side_effects: [string]
dependencies:
  skills: [string]
  scripts: [string]
  tools: [string]
  env: [string]
  external_services: [string]
failure_modes:
  - mode: string
    detection: string
    response: string
evaluation:
  success_signals: [string]
  checks: [string]
  artifacts: [string]
schedule_eligibility:
  eligible: boolean
  execution_mode: local_only | remote_ok | hybrid
  rationale: string
  cadence_examples: [string]
security:
  risk_level: read | write | destructive | external
  confirmation_required: boolean
  reason: string
  write_paths: [string]          # paths this skill may write to; [] for read-only
  credentials:                   # external-risk skills only; [] if no auth needed
    - name: string
      source: string             # e.g. cli_auth, env_var, file
      location: string           # where the credential lives (never in vault or git)
      required: boolean
```

## Field intent

- `name`: Canonical skill folder name.
- `purpose`: Durable responsibility of the skill in this repo.
- `trigger.user_phrases`: Typical user wording that should activate the skill.
- `trigger.intent_signals`: Short descriptions of intents the router should map to the skill even when wording differs.
- `inputs.required`: Inputs without which the skill cannot run correctly.
- `inputs.optional`: Inputs that refine or constrain the run.
- `outputs.primary`: Durable artifacts or primary destinations the skill is expected to produce.
- `outputs.side_effects`: Secondary changes such as updated logs, state files, or derived wiki pages.
- `dependencies.skills`: Other local skills this skill depends on conceptually or operationally.
- `dependencies.scripts`: Repo-local scripts this skill expects to run or reuse.
- `dependencies.tools`: CLIs, interpreters, or libraries that must be available.
- `dependencies.env`: Environment assumptions such as `.venv/bin/python3` or required auth state.
- `dependencies.external_services`: Remote services or APIs used by the skill.
- `failure_modes`: Expected failure classes with detection and minimum safe handling.
- `evaluation.success_signals`: What indicates the run worked.
- `evaluation.checks`: Concrete verification steps.
- `evaluation.artifacts`: Files, logs, or state to inspect when verifying.
- `schedule_eligibility.eligible`: Whether recurring automation is allowed.
- `schedule_eligibility.execution_mode`: `local_only`, `remote_ok`, or `hybrid`.
- `schedule_eligibility.rationale`: Why the schedule decision is correct.
- `schedule_eligibility.cadence_examples`: If eligible, examples like `daily`, `weekly`, or `on file drop`.
- `security.risk_level`: `read` (no writes, no external calls), `write` (modifies local files), `destructive` (can permanently remove or overwrite content), `external` (makes API calls to remote services; may also write).
- `security.confirmation_required`: Whether the runner and web UI must ask for explicit confirmation before executing.
- `security.reason`: One-line explanation of why this risk level was assigned.
- `security.write_paths`: List of vault-relative paths this skill is permitted to write to. Use `[]` for read-only skills. This is the declared permission boundary; see `.codex/SAFETY.md §6`.
- `security.credentials`: Required for `external` risk_level skills. List of credential entries; use `[]` if the skill makes external calls without auth (e.g., public scraping). Each entry has `name`, `source`, `location`, `required`. See `.codex/SAFETY.md §7`.

## Authoring rules

- Prefer short operational strings over prose blocks.
- Use empty lists instead of invented values.
- Keep `failure_modes` to concrete, likely cases.
- If `eligible: false`, still provide `execution_mode` and `rationale`.
- If a skill writes to `wiki/`, mention the target location in `outputs.primary`.
