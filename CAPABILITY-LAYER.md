# Capability layer

Everything above `claude-lumo` itself: the skills chain, the memory archive, an
unattended CI fixer, a weekly retro automation, the life-layer agent suites, and the
first-month review checklist that binds them together. All of it is local to this
machine; only `watch_ci.py` lives in this repo.

## Skills chain

38 skills from the [mattpocock/skills](https://github.com/mattpocock/skills)
distribution, installed globally to `~/.claude/skills` with:

```
npx skills@latest add mattpocock/skills -a claude-code -g
```

They form one workflow chain:

| Skill | Purpose | Invocation |
|---|---|---|
| `grilling` | stress-test a plan before committing to it | model-visible |
| `to-spec` | turn a conversation into a spec on the issue tracker | user only (`/to-spec`) |
| `to-tickets` | turn a spec into tracer-bullet tickets | user only (`/to-tickets`) |
| `implement` | work a spec or its tickets | user only (`/implement`) |
| `code-review` | review changes since a fixed point | model-visible |
| `pr` | write the PR body | model-visible |
| `retro` | retrospective after the work | user only (`/retro`) |

`to-spec`, `to-tickets`, `implement`, `grill-with-docs` and `retro` carry an
upstream `disable-model-invocation: true` frontmatter flag, so the model cannot
trigger them on its own — only an explicit slash command does. `code-review` did
not clash with the builtin during headless verification, but the name collision
is known upstream (mattpocock issue #483).

## Memory archive

`~/life/archive` is a git repo. Layout:

- `MEMORY.md` — distilled index, newest first.
- `journal/YYYY-MM-DD.md` — one file per day; new entries are prepended at the
  top.

Two local skills manage it, living under `~/.claude/skills/local/` (not in the
public repos):

- **`recall`** — retrieval only. Greps the archive and returns matching lines,
  capped at roughly 1500 tokens per query.
- **`log-session`** — prepends a dated entry and commits it to the archive repo.

Hard privacy boundary for both: raw health, financial and relationship data
stays on disk. Only distilled, query-relevant facts cross into the session
context, never bulk pasted file contents.

## Headless CI fixer

`scripts/watch_ci.py` in this repo. Polls GitHub Actions per
`scripts/watch-ci.toml`. On a new failed run it creates an Orca worktree and runs
headless `claude-lumo -p` to diagnose (`gh run view --log-failed`), fix, and
open a PR with a merge-danger section — or park the attempt with the
`parked-needs-human` label.

Autonomy contract: it never blocks on a question; anything unresolved is parked
instead.

Hard throttle, enforced in the script: at most 3 fix attempts per night window
(18:00–06:00) and 3 per calendar day. State lives in
`<CLAUDE_LUMO_STATE_DIR>/ci-watcher-state.json` (default `~/.claude-lumo`).
Runs log to `~/life/archive/journal/ci-watcher-YYYY-MM-DD.log`; the morning
report (`--report`) writes `~/life/archive/ci-reports/YYYY-MM-DD.md`.

Safe test, touches nothing:

```
python scripts/watch_ci.py --once --dry-run
```

## Weekly retro automation

Orca automation `weekly-retro`: Sundays 19:00 America/New_York, model pinned to
lumo-max via extra-agent-args. It reads the week's journal entries and git
activity, then drafts one-line-per-friction retro suggestions plus check and
lint ideas to `~/life/archive/retro-suggestions/YYYY-WW.md`.

It never applies its own suggestions — propose-only, enforced in its prompt.

Manage it with:

```
orca automations list
orca automations show <id>
orca automations edit <id> --disabled   # disable
orca automations run <id> --json        # test
orca automations remove <id>            # delete
```

## First-week review checklist

Weekly for the first month:

- **Failure notifications fired.** `~/life/archive/ci-reports/` exists and has
  an entry for every night CI actually failed.
- **Outbound actions reviewed before relaxing rules.** Inspect the PRs and
  issues the fixer opened (merge-danger sections present, parked items labeled)
  before loosening the throttle or the autonomy contract.
- **Verify one claimed action exists.** Pick one item from the latest ci-report
  or retro suggestion and confirm the referenced PR, issue or file is real.
- **Retro stayed propose-only.** `git status` across the repos it watches; only
  proposal files under `retro-suggestions/` should appear.
- **Throttle respected.** The ci-watcher journal shows no more than 3 attempts
  per night window.
- **Journal growth sanity.** Entries stay distilled. If raw personal detail
  starts appearing, tighten the privacy text in the two local skills.

## Life layer

Three pull-based agent suites extend the machinery beyond code. Nothing here is
scheduled: each runs only when invoked, by design.

Design rule for every life-layer agent (non-negotiable): it must either DO something
(close a loop) or SAY exactly one thing at the moment it is actionable. No dashboards,
no summary feeds, nothing the user has to remember to check.

Privacy boundary: raw health, financial and relationship data lives in
`~/life/archive` and never crosses into model context wholesale. Skills read targeted
ranges (grep, specific dates) and surface distilled answers, per the recall skill's
boundary.

| Suite | Skills | Data |
|---|---|---|
| Medical timeline | log-symptom, correlate, brief | `~/life/archive/medical/` |
| Single-action router | next | today's journal + MEMORY.md |
| Prediction journal | predict, calibrate | `~/life/archive/predictions/log.md` |

- **Medical** (`/log-symptom`): captures a symptom entry with same-day weather fetched
  from Open-Meteo at logging time (schema in `medical/SCHEMA.md`; entries prepend
  monthly under `medical/entries/`). `/correlate <range>` joins symptoms against stored
  weather, reports one paragraph with explicit small-sample caveats. `/brief` generates
  a pre-appointment page from the timeline. correlate is UNVALIDATED until
  `medical/VALIDATION.md` records a comparison against manual findings (gate currently
  not-yet-applicable; see that file).
- **Router** (`/next <energy>`): returns exactly one action sized to the reported energy
  level, one sentence of justification, no alternatives. Exists to collapse a paralyzing
  list to one item.
- **Predictions** (`/predict <claim> <conf>% by <date>`, `/calibrate <range>`): falsifiable
  claims with confidence and horizon; calibrate reports hit-rate versus stated confidence
  per bucket and flags DUE predictions that quietly never resolved. Scores honesty over
  time.

The six skills live in `~/.claude/skills/local/` (claude-config repo, commits 533a6cd and
bd5c8a8); schemas and data live in the `~/life/archive` repo (commits d4e8c91, 8ea4a2f,
1b87e1c). Nothing is scheduled — pull-based by design.
