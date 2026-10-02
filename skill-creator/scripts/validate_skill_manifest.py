#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Run this validator with the repo virtualenv: "
        ".venv/bin/python3 .codex/skills/skill-creator/scripts/validate_skill_manifest.py"
    ) from exc


ROOT = Path.cwd()
SKILLS_DIR = ROOT / ".codex" / "skills"
REQUIRED_TOP_LEVEL = [
    "name",
    "purpose",
    "trigger",
    "inputs",
    "outputs",
    "dependencies",
    "failure_modes",
    "evaluation",
    "schedule_eligibility",
]
EXECUTION_MODES = {"local_only", "remote_ok", "hybrid"}
RISK_LEVELS = {"read", "write", "destructive", "external"}
VALID_AGENTS = {"claude", "codex"}


def expect(condition: bool, errors: list[str], message: str) -> None:
    if not condition:
        errors.append(message)


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest must be a YAML mapping")
    return data


def validate_skill_dir(skill_dir: Path) -> list[str]:
    errors: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    manifest = skill_dir / "skill.yaml"

    expect(skill_md.exists(), errors, f"{skill_dir}: missing SKILL.md")
    expect(manifest.exists(), errors, f"{skill_dir}: missing skill.yaml")
    if errors:
        return errors

    try:
        data = load_yaml(manifest)
    except Exception as exc:  # pragma: no cover
        return [str(exc)]

    for field in REQUIRED_TOP_LEVEL:
        expect(field in data, errors, f"{manifest}: missing top-level field `{field}`")

    if errors:
        return errors

    expect(data["name"] == skill_dir.name, errors, f"{manifest}: `name` must equal `{skill_dir.name}`")
    expect(isinstance(data["purpose"], str) and data["purpose"].strip(), errors, f"{manifest}: `purpose` must be a non-empty string")

    trigger = data["trigger"]
    expect(isinstance(trigger, dict), errors, f"{manifest}: `trigger` must be a mapping")
    if isinstance(trigger, dict):
        for key in ("user_phrases", "intent_signals"):
            expect(key in trigger, errors, f"{manifest}: `trigger.{key}` is required")
            expect(isinstance(trigger.get(key), list), errors, f"{manifest}: `trigger.{key}` must be a list")

    inputs = data["inputs"]
    expect(isinstance(inputs, dict), errors, f"{manifest}: `inputs` must be a mapping")
    if isinstance(inputs, dict):
        for key in ("required", "optional"):
            expect(key in inputs, errors, f"{manifest}: `inputs.{key}` is required")
            expect(isinstance(inputs.get(key), list), errors, f"{manifest}: `inputs.{key}` must be a list")

    outputs = data["outputs"]
    expect(isinstance(outputs, dict), errors, f"{manifest}: `outputs` must be a mapping")
    if isinstance(outputs, dict):
        expect(isinstance(outputs.get("primary"), list), errors, f"{manifest}: `outputs.primary` must be a list")
        expect(isinstance(outputs.get("side_effects"), list), errors, f"{manifest}: `outputs.side_effects` must be a list")

    dependencies = data["dependencies"]
    expect(isinstance(dependencies, dict), errors, f"{manifest}: `dependencies` must be a mapping")
    if isinstance(dependencies, dict):
        for key in ("skills", "scripts", "tools", "env", "external_services"):
            expect(isinstance(dependencies.get(key), list), errors, f"{manifest}: `dependencies.{key}` must be a list")

    failure_modes = data["failure_modes"]
    expect(isinstance(failure_modes, list), errors, f"{manifest}: `failure_modes` must be a list")
    if isinstance(failure_modes, list):
        for index, item in enumerate(failure_modes):
            expect(isinstance(item, dict), errors, f"{manifest}: `failure_modes[{index}]` must be a mapping")
            if isinstance(item, dict):
                for key in ("mode", "detection", "response"):
                    expect(isinstance(item.get(key), str) and item.get(key, "").strip(), errors, f"{manifest}: `failure_modes[{index}].{key}` must be a non-empty string")

    evaluation = data["evaluation"]
    expect(isinstance(evaluation, dict), errors, f"{manifest}: `evaluation` must be a mapping")
    if isinstance(evaluation, dict):
        for key in ("success_signals", "checks", "artifacts"):
            expect(isinstance(evaluation.get(key), list), errors, f"{manifest}: `evaluation.{key}` must be a list")

    schedule = data["schedule_eligibility"]
    expect(isinstance(schedule, dict), errors, f"{manifest}: `schedule_eligibility` must be a mapping")
    if isinstance(schedule, dict):
        expect(isinstance(schedule.get("eligible"), bool), errors, f"{manifest}: `schedule_eligibility.eligible` must be a boolean")
        expect(schedule.get("execution_mode") in EXECUTION_MODES, errors, f"{manifest}: `schedule_eligibility.execution_mode` must be one of {sorted(EXECUTION_MODES)}")
        expect(isinstance(schedule.get("rationale"), str) and schedule.get("rationale", "").strip(), errors, f"{manifest}: `schedule_eligibility.rationale` must be a non-empty string")
        expect(isinstance(schedule.get("cadence_examples"), list), errors, f"{manifest}: `schedule_eligibility.cadence_examples` must be a list")

    # security block (required)
    security = data.get("security")
    expect(isinstance(security, dict), errors, f"{manifest}: `security` must be a mapping")
    if isinstance(security, dict):
        expect(security.get("risk_level") in RISK_LEVELS, errors, f"{manifest}: `security.risk_level` must be one of {sorted(RISK_LEVELS)}")
        expect(isinstance(security.get("confirmation_required"), bool), errors, f"{manifest}: `security.confirmation_required` must be a boolean")
        expect(isinstance(security.get("reason"), str) and security.get("reason", "").strip(), errors, f"{manifest}: `security.reason` must be a non-empty string")
        # write_paths: required; must be a list (empty list OK for read-only skills)
        expect("write_paths" in security, errors, f"{manifest}: `security.write_paths` is required (use [] for read-only skills)")
        expect(isinstance(security.get("write_paths"), list), errors, f"{manifest}: `security.write_paths` must be a list")
        # credentials: required for external-risk skills; must be a list (empty OK if no auth needed)
        if security.get("risk_level") == "external":
            expect("credentials" in security, errors, f"{manifest}: `security.credentials` is required for external-risk skills (use [] if no auth needed)")
            expect(isinstance(security.get("credentials"), list), errors, f"{manifest}: `security.credentials` must be a list")
            if isinstance(security.get("credentials"), list):
                for i, cred in enumerate(security["credentials"]):
                    expect(isinstance(cred, dict), errors, f"{manifest}: `security.credentials[{i}]` must be a mapping")
                    if isinstance(cred, dict):
                        for key in ("name", "source", "location"):
                            expect(isinstance(cred.get(key), str) and cred.get(key, "").strip(), errors, f"{manifest}: `security.credentials[{i}].{key}` must be a non-empty string")

    # agents block (optional; default [claude] if absent)
    agents = data.get("agents")
    if agents is not None:
        expect(isinstance(agents, list) and len(agents) > 0, errors, f"{manifest}: `agents` must be a non-empty list")
        if isinstance(agents, list):
            for a in agents:
                expect(a in VALID_AGENTS, errors, f"{manifest}: `agents` value '{a}' must be one of {sorted(VALID_AGENTS)}")

    return errors


def iter_skill_dirs() -> list[Path]:
    return sorted(path for path in SKILLS_DIR.iterdir() if path.is_dir())


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate local skill manifests.")
    parser.add_argument("skill_path", nargs="?", help="Path to a skill directory")
    parser.add_argument("--all", action="store_true", help="Validate all skills under .codex/skills")
    args = parser.parse_args()

    if args.all:
        targets = iter_skill_dirs()
    elif args.skill_path:
        targets = [Path(args.skill_path)]
    else:
        parser.error("provide a skill path or use --all")

    all_errors: list[str] = []
    for target in targets:
        all_errors.extend(validate_skill_dir(target))

    if all_errors:
        print("Skill manifest validation failed:", file=sys.stderr)
        for error in all_errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Validated {len(targets)} skill manifest(s) successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
