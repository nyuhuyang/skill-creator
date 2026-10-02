# Updating and testing skills (one or many)

## Contents
- Overview
- Run setup
- Phases
- Test tiers
- Accept and propagate
- Registry
- Report format

## Overview

There is one workflow for both cases: updating a single skill is a batch of one. Originals are never
edited before acceptance. All edits and tests happen on staging copies inside a run dir, so
rejecting a change means not accepting its staging copy. Paths: vault-relative paths resolve against
the vault root, and every command runs with cwd = vault root (see SKILL.md "Paths"). `PY` below means
`<vault root>/.venv/bin/python3` and `SC` means `<vault root>/.codex/skills/skill-creator`.

Dry-run is the default. Apply only after the user has approved the dry-run report.

## Run setup

1. Create the run dir and record the printed absolute path. Shell variables don't survive between
   tool calls, so write the path out literally wherever `$R` appears below:
   `mkdir -p outputs/skill-evals && R=$(mktemp -d "$PWD/outputs/skill-evals/batch-XXXXXX") && echo "$R"`
2. Targets: explicit skill dirs, or every skill under a root (`PY SC/scripts/lint_skill.py --root DIR`
   lists them and skips symlinks and `synced`). For each target:
   - Canonical path = `realpath`. Drop duplicates.
   - `key` = first 8 hex chars of sha256(canonical path), e.g.
     `python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8])" <path>`.
   - Write a line to `$R/targets.tsv`: key, name, canonical path, `risk_level`, runtime state
     (`PY SC/scripts/accept_skill.py --state-paths <dir>`).
3. Runtime state comes from `security.write_paths` entries that land inside the skill dir. Entries
   can be root-relative (`.codex/skills/<name>/…`, `<root>/<name>/…`) or absolute, and earlier
   acceptance records also count. If a skill declares state another way, or has no `skill.yaml`,
   pass `--protect <rel path>` (repeatable) to `accept_skill.py` for every file it writes at runtime.
4. Skills outside the vault root are outside this skill's write boundary. Get explicit user
   confirmation before applying to them. If such a skill has no `skill.yaml`, flag "runtime state
   unknown — check before accepting".

## Phases

1. **Inventory**: run `lint_skill.py` on every target and read each SKILL.md and skill.yaml. Build a
   worklist per skill:
   - mechanical fixes (contents lists via `--fix-toc`);
   - judgment items, each tied to a named rule in `best-practices.md` (no rule, no change);
   - missing evals;
   - T2 safety class;
   - which test tiers will run.
2. **Dry-run report** (default): write `$R/report.md` (see Report format). Change nothing else and
   stop. Ask the user which skills to apply, if any.
3. **Apply** (approved skills only):
   - Copy each skill twice: an immutable baseline at `$R/baseline/<key>/<name>/` and an editable copy
     at `$R/staging/<key>/<name>/`. Use `cp -Rp <dir>/. <dest>/`, because `-p` keeps file modes
     (without it, umask changes them and acceptance reports false drift).
   - Right after copying, confirm both copies equal the live skill:
     `PY SC/scripts/accept_skill.py --same <canonical path> <copy>` must print `same`.
   - Before any edit, check the staging copy for symlinks that point outside it. An edit made through
     such a link would change the original.
     `PY SC/scripts/accept_skill.py --check-links <staging copy> --identity <canonical path>` must exit
     0; the skill's declared runtime state, such as a `.toolvenv`, is exempt. If it exits 1, don't
     edit that skill. Report the links, and suggest declaring them as runtime state if they belong
     to a venv.
   - Edit only the staging copies: `--fix-toc` (always with `--within <staging copy>`; it refuses to edit
     through a symlink, outside the copy, or any file that isn't a reference `.md`), the judgment
     items, and missing evals. Mark drafted
     evals "new — needs user review"; they don't count as proof until the user has seen them.
   - Run the test tiers against the staging copies.
   - Allow at most 2 fix attempts per skill. If it still fails, mark it FAILED and don't accept its
     staging copy.
   - In Claude Code you may run one subagent per skill. Each subagent touches only its own staging
     dir and returns a summary. The coordinator runs T1/T2 and touches any shared file itself, one
     at a time. In Codex, run the skills one after another.
4. **Consolidated review**: add the results table to `$R/report.md`, plus the diff command for each
   skill: `diff -ru <canonical path> $R/staging/<key>/<name>`. The user accepts or rejects each
   skill.
5. **Accept, then propagate**: see the next sections.

## Test tiers

Always run T0. Run T1 when `description` or `when_to_use` changed, or when the skill is new. Run T2
when instructions or scripts changed.

**T0 static**:
- `PY SC/scripts/lint_skill.py <staging copy>` reports 0 errors.
- `PY SC/scripts/validate_skill_manifest.py <staging copy>` passes when `skill.yaml` exists.
- The diff has been reviewed.

**T1 trigger** (`evals/trigger.json` = 8–12 realistic queries `[{query, should_trigger}]`, about half
of them `false` near-misses). Verified in the 2026-10-01 pilot:
1. Create a disposable dir: `T=$(mktemp -d) && mkdir -p "$T/.claude/commands" "$T/bin" && echo "$T"`.
   Write `$T/override.json` with
   `{"skillOverrides": {"<name>": "off"}, "disableSkillShellExecution": true}`.
   Then create a `claude` shim that injects it. Run the block without the list indentation, because
   the closing `EOF` must start its line:
   ```bash
   cat > "$T/bin/claude" <<EOF
   #!/bin/bash
   echo "\${*//\$'\n'/ }" >> "$T/calls.log"
   exec > >(tee -a "$T/stdout.log") 2>>"$T/stderr.log" < /dev/null
   exec "$(command -v claude)" --settings "$T/override.json" "\$@"
   EOF
   chmod +x "$T/bin/claude"
   ```
   The shim records, for every call:
   - its arguments in `calls.log` (one line per call, so the call can be matched to its query);
   - its stream-json stdout in `stdout.log`;
   - its stderr in `stderr.log`.

   The official runner throws all of these away. Stdin comes from `/dev/null`, so claude prints
   no "no stdin" warning. `exec` keeps the PID, so the runner can still stop the call.
   Use the shim because `skillOverrides` in a project settings file does **not** hide user-level
   skills, while `--settings` does. Never edit the vault's own settings.
2. Get the trigger text: `PY SC/scripts/lint_skill.py --trigger-text <staging copy>`. It must exit 0
   with non-empty output. Save it to `$R/tests/<key>/trigger-text.txt`.
3. Visibility probe. It calls the real `claude` with the override, **not** the shim: the shim
   points stdin at `/dev/null`, and the probe sends its prompt on stdin.
   - Run from `$T`:
     `printf '%s' 'Answer only YES or NO: is a skill named <name> listed among the skills available to you? Do not invoke anything.' | env -u CLAUDECODE claude -p --tools Skill --settings "$T/override.json"`.
   - Control: run the same command from a second fresh temp dir with
     `--settings '{"disableSkillShellExecution": true}'`.
   - Hiding is verified only if control = YES and probe = NO. Anything else makes T1
     **inconclusive**: report it and do not score.
   - `--tools Skill` keeps the skill list visible while removing every tool that could act. Shell
     injection is off in both runs.
   - Send the prompt on stdin: `--tools` takes several values and would swallow a prompt argument.
   - Don't wrap `claude` in `timeout`, which makes it fail silently.
4. Start every eval attempt, including each description retry, with empty shim logs:
   `rm -f "$T/stdout.log"; : > "$T/calls.log"; : > "$T/stderr.log"`.
   Run the eval from `$T`:
   `env -u CLAUDECODE PATH="$T/bin:$PATH" PYTHONPATH=<official skill-creator dir> python3 -m scripts.run_eval --eval-set <staging copy>/evals/trigger.json --skill-path <staging copy> --description "<trigger-text.txt contents>" --runs-per-query 3 --num-workers 1 --timeout 60 --verbose > $R/tests/<key>/trigger.json`.
   Use one worker: several workers would put identical stubs in the same project. Check that the
   `Evaluating:` line in stderr equals the saved trigger text.
   Validity check before scoring:
   `PY SC/scripts/t1_check.py "$T" "$R/tests/<key>/trigger.json"` must exit 0 and print `valid`.
   It requires that:
   - the number of calls equals the expected queries × runs;
   - every call reached a decision by the runner's own rules (another tool chosen, a Skill/Read
     block completed or naming the runner's stub, or a clean result);
   - for every query, the trigger count rebuilt from the logs equals the runner's count (the
     runner can drop the last chunk of a stream when the process exits);
   - no call ended in an error result;
   - stderr holds only known-benign warnings. Timeouts, crashes and error results make the run
   **inconclusive**: do not score it, and report the checker's output. Copy the three logs to
   `$R/tests/<key>/`.
5. It passes when at least 80% of queries land on the correct side of a 0.5 trigger rate. Otherwise,
   revise the description in staging (at most 2 rounds) and report the result. Delete `$T` and the
   control dir afterwards.

The official copy lives at `ls -d ~/.claude/skills/synced/*/skill-creator | head -1`. If it is
missing, or `claude` is not on PATH, report T1 as unavailable.

**T2 behavioral** (`evals/evals.json`, official schema: prompt, expected_output, expectations):
- *Safe* class, where T2 runs by default, requires all of the following:
  - no executable code anywhere in the skill, `evals/` included: no `scripts/` dir, no file with an
    exec bit, no `.py`, `.sh`, `.js`, `.ts`, `.mjs`, `.cjs`, `.rb` or `.pl` file;
  - no eval prompt or fixture that asks the run to execute a command or contact a service;
  - empty `dependencies.scripts`, and no absolute symlink and no symlink resolving outside the
    skill dir (an absolute link would point back to the original once copied into `$W`);
  - empty `dependencies.external_services`;
  - every `write_paths` and `outputs` path is relative and does not escape (no leading `/` or `~`,
    no `..` after normalization);
  - no file the run can read names network/API calls, publishing, sending, or writes outside the
    run dir. That covers SKILL.md and every reference, asset, eval and fixture in the candidate and
    in the dependency closure. A reference you could not inspect counts as unsafe;
  - every skill in the **dependency closure** is itself safe. The closure is `dependencies.skills`,
    followed transitively. Seed the visited set with the target itself, so a cycle (A→B→A) or a
    self-dependency never puts the target in its own closure.
- Classify again on the **final staging candidate** and its dependencies right before running T2.
  A staged change that adds code, for example a new validator script, turns a safe skill unsafe.
- Everything else is *unsafe*. For example, skill-registry is unsafe: its scripts write
  `../../.codex/` and rsync to `~/.claude/skills`. Unsafe skills need the user's explicit go-ahead
  and disposable resources. Otherwise report "T2 skipped: <reason>".
- Run each case in a disposable mini-vault: `W=$(mktemp -d)`.
  - Copy the staging copy to `$W/.codex/skills/<name>/`.
  - Copy every skill in the dependency closure from its current original, and record each copy's
    name and SKILL.md sha256. Never copy anything (dependencies, fixtures) over
    `$W/.codex/skills/<name>/`.
  - After setup, the disposable candidate must still match the reviewed staging copy exactly:
    `diff -r $R/staging/<key>/<name> "$W/.codex/skills/<name>"` must print nothing. Otherwise T2
    would grade some other version.
  - Copy in any fixtures.
  - Before launching, check that no symlink under `$W` is absolute or resolves outside `$W`, dangling
    ones included: `PY SC/scripts/accept_skill.py --check-links "$W"` must exit 0.
    Otherwise T2 is invalid.
  - Run with cwd = `$W` and `KB_ROOT=$W`. Use the T1 shim pattern with `skillOverrides` set to
    `"off"` for the target **and** every skill in the dependency closure, so that only the copies in
    `$W` can be used.
  - The prompt names `$W/.codex/skills/<name>/SKILL.md` by absolute path. Record that file's sha256.
    Accept the grading only if the transcript shows that path was read, and shows no use of
    `~/.claude/skills/<x>` or `<vault root>/.codex/skills/<x>` for the target or any skill in the closure.
    Any such use means the run exercised an installed copy instead of the candidate: report T2 as
    invalid.
  - Outputs stay in `$W`. Copy the results to `$R/tests/<key>/`.
- In Claude Code, use `/anthropic-skills:skill-creator` subagents and its `agents/grader.md` with
  these dirs. In Codex, use `claude -p` from `$W` if it is available; otherwise skip and report.

## Accept and propagate

Acceptance is a fragile operation, so a script does it:

- Accept: `PY SC/scripts/accept_skill.py --run $R --key <key> --live <canonical path> [--dry-run]`.
  - It takes a non-blocking lock under `outputs/skill-evals/locks/`, named by the canonical-path
    key. Acceptance and propagation share that one namespace, so they can't change the same
    directory at the same time. The lock is released when the process exits. `--key` must equal
    the canonical-path key of `--live`, or the run is refused.
  - It refuses if the live copy changed since staging (exit 4 = re-stage and re-review).
  - It refuses (exit 2, nothing copied) if any involved `skill.yaml` can't be read, or declares the
    whole skill dir writable (`.codex/skills/<name>/`), or declares state through a symlink (for example
    `cache/state.json` where `cache -> data`), because the runtime state can't be inferred then.
    For the symlink case, protect the resolved files (`--protect data/state.json`). Proceed only after the user reviews a full `--protect` list,
    passing `--ignore-bad-manifest`.
  - It protects runtime state: the union of the state paths declared by the live, baseline and
    staging manifests, plus every path protected for that directory by an earlier acceptance or
    propagation (records in `outputs/skill-evals/accepted/` and `propagated/`, both keyed by the
    canonical path). Retiring a protected state path is a separate decision the
    user makes explicitly, by editing that record.
  - Drift checks, verification and records compare entry type, exec bits, symlink targets and
    content, not just file bytes.
  - It rsyncs staging → live with `--checksum --delete`, then verifies.
  - Exit 3 means busy (another process holds the lock). Exit 5 means verification failed.
- Propagate to Claude Code (vault skills only; this is outside the write boundary, so get one user
  confirmation for the whole set):
  `PY SC/scripts/accept_skill.py --propagate <vault skill dir> --dry-run`, show the preview, then
  run it again without `--dry-run`.
  - It refuses a symlinked destination (exit 6).
  - It holds the source's lock (shared with acceptance) and a destination lock for the whole sync
    (exit 3 = busy). The same unreadable-manifest rule applies.
  - It protects the union of the source state paths, the destination state paths, and every path
    protected in earlier syncs. A sync record kept in `outputs/skill-evals/propagated/` remembers them.
  - It refuses with exit 7 when the destination differs from the source and holds changes this script
    didn't make: it changed since the last sync, or it was never synced and differs. This happens when
    an installed copy was edited directly, for example `disable-model-invocation: true` added in
    `~/.claude/skills`, or a newer version installed there. First merge those changes into the vault
    copy (as a staged update, if they are wanted). Pass `--overwrite-dest` only when the user confirms
    the destination's changes can be discarded.
- Never use a whole-dir `rsync` without these protections, and never use a full `build_registry.py`
  sync: both overwrite runtime state or other skills.

## Registry

`build_registry.py` writes `AI_Workspace/.codex/registry.json` and `.md`. Both are outside the vault
and agentic-os reads them; `--no-sync` only skips the rsync. After acceptance, if any accepted skill
changed `skill.yaml` or the set of skills changed, ask the user, then run once from the vault root:
`PY .codex/skills/skill-registry/scripts/build_registry.py --no-sync`.
First confirm the installed builder supports the flag:
`PY .codex/skills/skill-registry/scripts/build_registry.py --help | grep -q -- --no-sync`.
If it doesn't, stop. Never run the builder without `--no-sync`: by default it syncs every skill
into `~/.claude/skills`.

## Report format

`$R/report.md`:
- A header with the run dir, mode (dry-run/apply), targets and date.
- One section per skill, containing:
  - the lint findings;
  - the proposed changes, each with its rule;
  - runtime state;
  - T2 safety class and the planned tiers;
  - missing evals;
  - risks.
- In apply mode, add a results table: skill | T0 | T1 | T2 | status (ok / FAILED / inconclusive /
  skipped) | diff command.
