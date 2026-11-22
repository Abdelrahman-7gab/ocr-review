---
name: ocr-review
description: >
  Code review of a diff (working tree, branch range, commit or PR) that runs
  Alibaba Open Code Review's native pipeline on the user's Claude subscription,
  with no API key. A script does OCR's deterministic parts (file selection,
  rules, grouping thresholds, prompt rendering, comment positioning, round
  bookkeeping). Claude subagents do the model parts with OCR's own prompts:
  grouping, a review plan, multi-round reviewers, and the fact-checking review
  filter. An Opus check of critical/high findings follows. Use when the user asks
  for an OCR review, an "ocr-review", or a thorough or full-coverage review of a
  large change, or when another skill calls for it. Never
  run `ocr review` (native mode with a provider) or ask for an API key.
---

# OCR review (native pipeline, on Claude subagents)

You coordinate. Subagents do every model step, and the script does everything
else. Keep diffs and prompts out of your own context: pass file paths, and read
only the script's one-line outputs and the small JSON answers.

Requirements: `git` ≥ 2.41 and `ocr` ≥ 1.9 on PATH. Install OCR with
`npm install -g @alibaba-group/open-code-review`, and only with the user's
go-ahead. Never run `ocr review` or `ocr config`.

`N` below is `python3 ~/.claude/skills/ocr-review/native.py`. `$OUT` is a fresh
directory in your scratchpad.

Every subagent is an `Agent` call with `subagent_type: "general-purpose"` and
this brief, filled in:

> Read `<prompt file>` and do exactly the task it describes. `<system>` is your
> role. `<harness>` says how to use your tools and where to write the answer.
> Write only that answer file, and change nothing else.

Use `model` as given per step. Run at most 6 subagents at once.

## 1. Plan (script)

```bash
N plan --out $OUT --repo <repo> [--from <base> [--to <head>] | --commit <sha>] \
  [--background <file>] [--exclude <globs>] [--effort low|medium|high] [--risky '<regex>']
```

- **Targets.**
  - Branch or PR: `--from` the base (the merge-base is taken) and `--to` the head.
  - Working tree: no flags.
  - One commit: `--commit`.
  - Refs are resolved to SHAs.
- **`--background`:** becomes OCR's "Requirement Background" (ticket text,
  requirements, or a team rulebook such as a `code-rules.md`). In commit
  mode it defaults to the commit message.
- **`--effort`:** sets the review rounds per group: 1, 2 (the default) or 3.
  Use `low` for a quick pass.
- **`--risky`:** a path regex. A group with a matching file gets an Opus
  reviewer instead of Sonnet. The defaults cover auth, money, data,
  migrations, and sync/push/write/state. You can also promote a group to Opus
  yourself when its content is risky (edit its `model` in `$OUT/state.json`).

Tell the user the summary line. If it printed `NEXT grouping`, run one
subagent (`model: "sonnet"`) on `$OUT/grouping-prompt.md`. Tell it to write
only the JSON array to `$OUT/grouping.json`. Then run:

```bash
N groups --out $OUT --response $OUT/grouping.json   # or without --response when no grouping was asked
```

This prints the groups: their model, files, lines, and whether each needs a plan.

## 2. Plan pass (groups marked `plan yes`)

Run one subagent per such group (`model: "sonnet"`) on
`$OUT/group-<g>/plan-prompt.md`. It uses no tools except Write, and writes its
whole plan text to `$OUT/group-<g>/plan.txt`. A failed plan does not block the
review: continue without one.

## 3. Rounds (each group, independently; groups run in parallel)

For round `r` = 1, 2, …:

1. `N render --out $OUT --group g --round r`. Then run a subagent with the
   group's `model` on the printed prompt. Each round is a fresh subagent:
   never continue the previous one. It writes `round-r/comments.json` and
   replies `DONE <n> comments`. If it fails or writes nothing, run it once
   more. If it fails again, that round has state FAILED.
2. `N position --out $OUT --group g --round r`. This places each comment by its
   `existing_code` (OCR's tiers: the diff's new side, then without blank lines,
   then the old side, then the whole file, then a unique match in another
   reviewed file). For any `relocate-<i>.md` it prints, you may run a
   `model: "haiku"` subagent and collect its code block as
   `{"<i>": "<code>"}` in `round-r/relocated.json`. Then rerun with
   `--relocated`. Unplaced comments stay at line 0 and are kept.
3. `N filter --out $OUT --group g --round r`. Unless it says `skip`, run a
   subagent (`model: "sonnet"`) on the printed filter prompt. It writes
   `round-r/filter.json`.
4. `N apply --out $OUT --group g --round r`. It prints either `NEXT round
   r+1` or `STOP (<reason>)`.
   - It stops when the reviewer did not finish, when a round adds no new
     surviving finding, at 30 confirmed findings, or at the last round.
   - The filter fails open: a missing or unparseable answer removes nothing.

## 4. Check critical and high findings (ours, not OCR's)

Run `N report --out $OUT`, then read `$OUT/findings.json`. Hand every critical
or high finding to one `model: "opus"` subagent with this brief:

> For each finding, open the code in `<repo>` at `<to>` and decide whether the
> failure is real and reachable: CONFIRMED, ADJUSTED (say the new severity or
> line), or REJECTED (say why). Be adversarial. Write a JSON list
> `[{"index", "verdict", "severity", "reason"}]` to `$OUT/verdicts.json`.

Then:

- Drop the rejected findings.
- Read the code behind every critical finding that remains yourself.
- Add each verdict to the finding.

You are accountable for what goes out.

## 5. Report

`$OUT/report.md` is the skeleton. It has the coverage per group (where the
reviewer did not list a file it was given, the report says so) and the
excluded files with their reasons. Then come the findings, most severe first,
as `path:start-end`, and the comments the review filter removed.

Give the user:

- coverage;
- findings with verdicts;
- the models used per group;
- what nobody checked: files a reviewer skipped, unplaced comments, and
  excluded files such as tests and docs.

When the caller asked for a file, write it there. When a findings-report host
tool is available and the caller asked for it, use that instead.

Fix only when the user asked for "review and fix":

- apply confirmed critical/high fixes;
- describe the medium ones;
- leave the low ones.

## Notes

- **Prompts.** `prompts/` holds OCR's prompts verbatim (Apache-2.0,
  `prompts/LICENSE-open-code-review`, alibaba/open-code-review at e74f2c6). Do
  not edit them; the Claude-specific tool mapping lives in the `<harness>`
  block that `native.py` appends.
- **Differences from OCR.**
  - Token counts are estimated as chars/4.
  - There is no hard 100-call budget or grace round; the harness asks for it.
  - There is no context compression; Claude Code compacts on its own.
  - Excluded tests and docs are listed in each reviewer's "other changed
    files", so they get read as context.
  - Findings are reported most severe first.
  - An Opus check is added for critical/high findings.
- **Excluded files.** OCR excludes tests and docs from review by default
  (`default_path`, `unsupported_ext`).
- **Coverage is the point.** Never drop a group silently, and never stop
  after the first serious finding.
