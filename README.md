# ocr-review

A [Claude Code](https://claude.com/claude-code) skill that runs
[Open Code Review](https://github.com/alibaba/open-code-review)'s **native review
pipeline** on Claude Code subagents, so it uses your Claude subscription and needs no API
key or OCR model provider.

OCR's native mode (`ocr review`) calls a model provider directly. Its delegation mode
uses your agent's model but skips most of the pipeline. This skill keeps the pipeline:

1. **Select and rule**: `ocr delegate preview` and `ocr delegate rule` (no model) pick the
   files and the review checklist for each one.
2. **Group**: OCR's thresholds; at 4 or more files, OCR's grouping prompt.
3. **Plan**: OCR's planning prompt, for larger groups, in round 1 only.
4. **Review in rounds**: OCR's main review prompt, verbatim, in a fresh subagent each
   round (1, 2 or 3 by effort). Later rounds are told what is already confirmed, and the
   loop stops early when a round adds nothing.
5. **Position**: every finding is placed by the code it quotes, using OCR's matching
   tiers (the diff's new side, without blank lines, the old side, the whole file, then a
   unique match in another file), with OCR's relocation prompt as a fallback.
6. **Filter**: OCR's fact-checking prompt. It removes only findings that the diff proves
   wrong, it protects five kinds of finding, and it keeps everything when it fails.
7. **Check**: an Opus subagent re-checks critical and high findings against the code
   (an addition, not in OCR).

`native.py` does every step that needs no model. Subagents do the rest, Sonnet by default
and Opus for groups whose paths look risky (auth, money, data, migrations, sync).

## Install

```bash
npm install -g @alibaba-group/open-code-review   # the ocr CLI, >= 1.9
git clone https://github.com/Abdelrahman-7gab/ocr-review ~/.claude/skills/ocr-review
```

Requires `git` 2.41 or later and Python 3.9 or later. Then ask Claude Code for "an OCR
review of this branch against main". [SKILL.md](SKILL.md) is the procedure Claude follows.

## How it compared

All three were run on one 12-file TypeScript change (+602 lines):

| | Claude Code `/code-review` | delegation-style skill | this skill (medium effort) |
|---|---|---|---|
| wall time | ~2 min | ~6 min | ~15 min |
| findings | 9, unverified, 1 false positive | 7, verified | 20 (1 high, 9 medium, 10 low) |
| the one crash bug | missed | found | found |
| a data-loss bug | missed | found | found, in round 2 |

A third of the findings came from round 2. Use `--effort low` for a single round.

## What differs from OCR's native mode

- **Token counting.** Tokens are estimated as characters / 4, so the size gates are
  approximate.
- **Budget and compression.** Subagents have no hard 100-call budget and no "final
  round". The harness block asks for the budget instead, and Claude Code compacts
  context in its own way.
- **Tests and docs.** Files that OCR excludes by default (tests, docs) are listed in
  each reviewer's "other changed files", so they get read as context.
- **Report order.** Findings are reported most severe first (OCR prints critical last).
- **Extra check.** An Opus check of critical and high findings is added.

## Licence and attribution

Apache License 2.0 ([LICENSE](LICENSE)). The prompts in `prompts/` are Open Code Review's,
copied unmodified. `native.py` reimplements its pipeline. [NOTICE](NOTICE) has the
details. This project is not affiliated with Alibaba Group or the Open Code Review project.
