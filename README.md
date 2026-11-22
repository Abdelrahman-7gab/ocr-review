# ocr-review

A [Claude Code](https://claude.com/claude-code) skill that runs
[Open Code Review](https://github.com/alibaba/open-code-review)'s **native review
pipeline** (what `ocr review` does) on Claude Code subagents, so it uses your Claude
subscription and needs no API key or OCR model provider.

OCR's native mode calls a model provider directly. Its delegation mode uses your agent's
model but skips most of the pipeline. This skill keeps the whole pipeline and changes only
who answers the model calls:

- **OCR's CLI where OCR has a command:** `ocr delegate preview` picks the files and resolves
  the background (sanitised and wrapped as OCR does); `ocr delegate rule` resolves the
  review rules, including your `--rule` file and the project's `.opencodereview/rule.json`.
- **OCR's code, ported, where it has no command:** one `git diff --find-renames` over the
  whole change split per file as OCR's parser does (renames, quoted paths and odd bytes
  included), grouping thresholds, prompt assembly, the prompt-size gate, comment
  positioning, the confirmed-findings block, stop rules and the 30-finding cap.
- **OCR's prompts, verbatim, answered by Claude subagents:** grouping, plan, each review
  round (a fresh subagent per round), re-location of comments it cannot place, and the
  fact-checking review filter. Every subagent runs on one model, as OCR uses one model.

## Install

```bash
npm install -g @alibaba-group/open-code-review   # the ocr CLI
git clone https://github.com/Abdelrahman-7gab/ocr-review ~/.claude/skills/ocr-review
```

Requires git and Python 3.9 or later. Then ask Claude Code for "an OCR review of this
branch against main". [SKILL.md](SKILL.md) is the procedure Claude follows; `native.py`
is the script it runs between model steps.

A team rulebook goes in an OCR rule file, as in OCR:

```json
{"rules": [{"path": "**/*", "rule": "/abs/path/code-rules.md", "merge_system_rule": true}]}
```

## What differs from OCR's native mode

Necessary, because the model is a Claude Code subagent rather than an API call:

- **Tool calls.** The subagent gets OCR's tool definitions verbatim plus a short mapping
  onto Claude Code's tools (Read, Grep, Glob, git). It writes its `code_comment` calls
  and its `task_done` state into one JSON answer file instead of calling tools.
- **Turn budget.** There is no hard 100-call counter or forced final round; the subagent is
  given OCR's final-round message to apply when it nears about 100 calls.
- **Context compression.** OCR's compression prompt is not used; Claude Code compacts on
  its own.
- **Token counts.** No tokenizer: tokens are estimated as characters / 3.5, so OCR's
  160,000-token gates fire at about the same size, not exactly.
- **Retries.** A subagent that fails is run once more, standing in for OCR's HTTP retries.

Improvements over OCR (OCR's own code has these bugs):

- **One-pass placeholders.** OCR fills its prompt placeholders one after another, so a diff
  that happens to contain a placeholder name (`{{plan_guidance}}`, `{{comments}}`, `{diff}`…)
  gets another value pasted into it. Here every placeholder is filled in one pass, and a diff
  is always shown as it is. It only matters for diffs that contain those names, such as a
  review of this skill or of OCR.
- **Comment fields shown.** OCR's API hands the reviewer the `code_comment` schema; here the
  reviewer's tool list also spells out each comment field and its allowed values.

Choices, each off or presentation-only unless you ask:

- **Model.** `--model opus` (default) or `sonnet`, for every step of a run.
- **Language.** `--language` (default English), as OCR's language setting: the reply language
  added to the review and plan prompts.
- **Report order.** Findings are listed most severe first (OCR prints critical last), with
  the comments the filter removed listed below them.
- **`--list-excluded`** (off by default): also name excluded tests, docs, user-excluded and
  too-large files in each reviewer's "other changed files".
- **Extra check** (only when asked): a subagent re-checks critical and high findings against
  the code; rejected findings are listed apart and OCR's original severity is kept.

## Licence and attribution

Apache License 2.0 ([LICENSE](LICENSE)). The prompts and tool definitions are Open Code
Review's, copied unmodified; `native.py` is a Python port of its review code.
[NOTICE](NOTICE) has the details. This project is not affiliated with Alibaba Group or the
Open Code Review project.
