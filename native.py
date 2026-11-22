#!/usr/bin/env python3
"""Deterministic half of ocr-review: Open Code Review's native pipeline, minus the model calls.

The coordinator runs each step, hands the rendered prompt files to Claude subagents
(on the user's subscription), saves their answers where this script says, and runs
the next step. Nothing here calls a model. Prompts are OCR's own (prompts/, Apache-2.0,
alibaba/open-code-review at e74f2c6); thresholds and algorithms follow OCR's Go code
(see the comments naming each function).

  native.py plan     --out DIR [--repo P] [--from REF [--to REF] | --commit SHA]
                     [--background FILE] [--exclude GLOBS] [--risky REGEX] [--effort low|medium|high]
  native.py groups   --out DIR [--response FILE]   apply the grouping answer (or none: one group per file)
  native.py render   --out DIR --group N --round R  write the reviewer prompt for one round
  native.py position --out DIR --group N --round R [--relocated FILE]
  native.py filter   --out DIR --group N --round R  write the review-filter prompt (or say skip)
  native.py apply    --out DIR --group N --round R [--response FILE]  apply the filter, say next round or stop
  native.py report   --out DIR                       findings.json + report.md
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS = os.path.join(HERE, "prompts")

# OCR constants (task_template.json, grouping.go, util.go, agent.go).
GROUPING_MIN_FILES = 4
GROUPING_BUNDLE_LINE_THRESHOLD = 200
MAX_FILES_PER_GROUP = 10
TOKEN_LIMIT = 160000            # 0.8 * MAX_TOKENS
PLAN_MODE_LINE_THRESHOLD = 50
PLAN_MODE_GROUP_LINE_THRESHOLD = 100
CONFIRMED_CAP = 30
CONFIRMED_MAX_CODE = 200
CONFIRMED_MAX_CONTENT = 300
ROUNDS = {"low": 1, "medium": 2, "high": 3}
CATEGORIES = {"bug", "security", "performance", "maintainability", "test", "style",
              "documentation", "other"}
SEVERITIES = ["critical", "high", "medium", "low"]

# Ours, not OCR's: bundles touching these paths get an Opus reviewer.
DEFAULT_RISKY = (
    r"(auth|login|session|token|password|secret|crypt|permission|acl|role|"
    r"pay|billing|money|price|invoice|wallet|refund|subscription|"
    r"migrat|schema|repositor|/db/|database|sql|"
    r"secur|sanitiz|csrf|cors|oauth|jwt|webhook|"
    r"sync|push|write|upload|import|export|delete|purge|backup|restore|queue|"
    r"cache|persist|storage|state)"
)


# ---------- helpers ----------

def git(repo, *args, check=True):
    res = subprocess.run(["git", "--no-pager", *args], cwd=repo, capture_output=True, text=True)
    if check and res.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def run_json(cmd, cwd):
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{res.stderr.strip()}")
    return json.loads(res.stdout)


def prompt(name):
    with open(os.path.join(PROMPTS, name + ".md")) as fh:
        return fh.read()


def tokens(text):
    return len(text) // 4        # OCR counts cl100k tokens; chars/4 is close enough for the gates


def load(out):
    with open(os.path.join(out, "state.json")) as fh:
        return json.load(fh)


def save(out, state):
    with open(os.path.join(out, "state.json"), "w") as fh:
        json.dump(state, fh, indent=1)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    with open(path) as fh:
        text = fh.read().strip()
    text = re.sub(r"^```[a-zA-Z]*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)   # first balanced-looking value (parseToolArgs)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    return default


def status_word(status):
    return {"added": "ADDED", "deleted": "DELETED", "renamed": "RENAMED"}.get(status, "MODIFIED")


def entry(f):
    """formatDiffEntry: `<STATUS>   <path> (+i/-d)`."""
    return f"{status_word(f['status'])}   {f['path']} (+{f['insertions']}/-{f['deletions']})"


def gdir(out, g):
    return os.path.join(out, f"group-{g}")


def rdir(out, g, r):
    return os.path.join(gdir(out, g), f"round-{r}")


# ---------- diffs (agent.go: loadDiffs) ----------

DIFF_FLAGS = ["--no-ext-diff", "--no-textconv", "--find-renames", "--src-prefix=a/",
              "--dst-prefix=b/", "--no-color", "-U3"]


def file_diff(state, path, status):
    repo, base, to = state["repo"], state["base"], state["to"]
    if state["mode"] == "workspace":
        text = git(repo, "diff", *DIFF_FLAGS, base, "--", path)
        if not text.strip() and status == "added" and os.path.exists(os.path.join(repo, path)):
            res = subprocess.run(["git", "--no-pager", "diff", "--no-index", *DIFF_FLAGS,
                                  "/dev/null", path], cwd=repo, capture_output=True, text=True)
            text = res.stdout
    else:
        text = git(repo, "diff", *DIFF_FLAGS, base, to, "--", path)
    # OCR keeps the header lines but drops `index ...` and trims the trailing newline.
    return "\n".join(l for l in text.splitlines() if not l.startswith("index ")).rstrip("\n")


def new_content(state, path):
    if state["mode"] == "workspace":
        p = os.path.join(state["repo"], path)
        return open(p, errors="replace").read() if os.path.exists(p) else ""
    return git(state["repo"], "show", f"{state['to']}:{path}", check=False)


# ---------- plan ----------

def cmd_plan(a):
    repo = os.path.abspath(os.path.expanduser(a.repo))
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    flags = []
    if a.commit:
        flags = ["--commit", a.commit]
    elif a.from_ref:
        flags = ["--from", a.from_ref] + (["--to", a.to_ref] if a.to_ref else [])
    common = ["--repo", repo, "--format", "json"] + flags
    if a.exclude:
        common += ["--exclude", a.exclude]
    preview = run_json(["ocr", "delegate", "preview"] + common, repo)
    files = preview.get("reviewable_files") or []

    mode = preview["mode"]
    head = git(repo, "rev-parse", "HEAD").strip()
    if mode == "range":
        base, to = preview["merge_base"], git(repo, "rev-parse", preview.get("to") or "HEAD").strip()
    elif mode == "commit":
        to = git(repo, "rev-parse", a.commit).strip()
        base = git(repo, "rev-parse", f"{to}^").strip()
    else:
        base, to = head, "WORKTREE"
    dirty = bool(git(repo, "status", "--porcelain", "--untracked-files=no").strip())
    reads_tree = mode == "workspace" or (to == head and not dirty)

    background = ""
    if a.background:
        background = open(os.path.expanduser(a.background)).read().strip()
    elif mode == "commit":
        background = git(repo, "log", "-1", "--format=%B", to).strip()   # OCR's commit-message default

    state = {
        "repo": repo, "mode": mode, "base": base, "to": to, "reads_tree": reads_tree,
        "effort": a.effort, "rounds": ROUNDS[a.effort], "background": background,
        "started": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "excluded": preview.get("excluded_files") or [], "files": [], "groups": None,
        "risky": a.risky,
    }
    if not files:
        save(out, state)
        print("Nothing to review.")
        return

    paths = [f["path"] for f in files]
    rules = run_json(["ocr", "delegate", "rule"] + common + paths, repo)
    rule_text, rule_of = {}, {}
    for g in rules.get("groups", []):
        rule_text[str(g["group_id"])] = g["rule"]
        for p in g["files"]:
            rule_of[p] = str(g["group_id"])
    state["rules"] = rule_text

    risky = re.compile(a.risky, re.I)
    os.makedirs(os.path.join(out, "diffs"), exist_ok=True)
    for i, f in enumerate(files):
        d = file_diff(state, f["path"], f["status"])
        if tokens(d) > TOKEN_LIMIT:     # selection gate 8: ExcludeTooLarge
            state["excluded"].append({**f, "exclude_reason": "too_large"})
            continue
        dp = os.path.join(out, "diffs", f"{i}.diff")
        write(dp, d)
        state["files"].append({
            "path": f["path"], "status": f["status"], "insertions": f.get("insertions", 0),
            "deletions": f.get("deletions", 0), "rule": rule_of.get(f["path"]), "diff": dp,
            "risky": bool(risky.search(f["path"])),
        })

    n = len(state["files"])
    save(out, state)
    print(f"{mode} review of {repo}: {n} files (+{sum(f['insertions'] for f in state['files'])}"
          f"/-{sum(f['deletions'] for f in state['files'])}), {len(state['excluded'])} excluded, "
          f"effort {a.effort} = {state['rounds']} round(s); base {base[:10]}, to {to[:10]}")
    # GroupingPlan
    if n >= GROUPING_MIN_FILES:
        listing = "\n".join(f"[{i}] {entry(f)}" for i, f in enumerate(state["files"]))
        text = (f"<system>\n{prompt('grouping_task_system')}\n</system>\n\n"
                f"{prompt('grouping_task_user').replace('{{file_list}}', listing)}")
        write(os.path.join(out, "grouping-prompt.md"), text)
        print(f"NEXT grouping: run a subagent on {out}/grouping-prompt.md, save its JSON array to "
              f"{out}/grouping.json, then `native.py groups --out {out} --response {out}/grouping.json`")
    else:
        print(f"NEXT: native.py groups --out {out}")


# ---------- groups (grouping.go) ----------

def cmd_groups(a):
    out = os.path.abspath(a.out)
    state = load(out)
    files = state["files"]
    n = len(files)
    churn = sum(f["insertions"] + f["deletions"] for f in files)
    groups = []
    if n <= 1:
        groups = [{"label": f["path"], "files": [i]} for i, f in enumerate(files)]
    elif n >= GROUPING_MIN_FILES:
        resp = read_json(a.response)
        used = set()
        if isinstance(resp, list):
            for g in resp:
                if not isinstance(g, dict):
                    continue
                idx = []
                for i in g.get("files") or []:
                    if isinstance(i, int) and 0 <= i < n and i not in used:
                        used.add(i)
                        idx.append(i)
                if idx:
                    groups.append({"label": str(g.get("label") or ""), "files": idx})
        for i, f in enumerate(files):           # unassigned (or a failed call) -> own group
            if i not in used:
                groups.append({"label": f["path"], "files": [i]})
    elif churn < GROUPING_BUNDLE_LINE_THRESHOLD:
        groups = [{"label": "small change set", "files": list(range(n))}]
    else:
        groups = [{"label": f["path"], "files": [i]} for i, f in enumerate(files)]

    # enforceMaxFilesPerGroup, then enforceGroupTokenBudget
    sized = []
    for g in groups:
        for s in range(0, len(g["files"]), MAX_FILES_PER_GROUP):
            sized.append({"label": g["label"], "files": g["files"][s:s + MAX_FILES_PER_GROUP]})
    final = []
    for g in sized:
        size = sum(tokens(open(files[i]["diff"]).read()) for i in g["files"])
        if size > TOKEN_LIMIT and len(g["files"]) > 1:
            final += [{"label": f"{g['label']} (split: {files[i]['path']})", "files": [i]}
                      for i in g["files"]]
        else:
            final.append(g)

    state["groups"] = []
    for gi, g in enumerate(final, 1):
        members = [files[i] for i in g["files"]]
        churns = [f["insertions"] + f["deletions"] for f in members]
        total = sum(churns)
        risky = any(f["risky"] for f in members)
        state["groups"].append({
            "id": gi, "label": g["label"], "files": g["files"],
            "key": members[0]["path"] if len(members) == 1 else ",".join(sorted(f["path"] for f in members)),
            "lines": total, "model": "opus" if risky else "sonnet", "risky": risky,
            # PlanRequired
            "plan": max(churns) >= PLAN_MODE_LINE_THRESHOLD
                    or (len(members) >= 2 and total >= PLAN_MODE_GROUP_LINE_THRESHOLD),
            "confirmed": [], "dropped": [], "done": False, "stopped": None,
        })
    save(out, state)
    for g in state["groups"]:
        write_inputs(out, state, g)
        if g["plan"]:
            write_plan_prompt(out, state, g)
    for g in state["groups"]:
        print(f"  group {g['id']}: {g['model']}, {len(g['files'])} files, {g['lines']} lines, "
              f"plan {'yes' if g['plan'] else 'no'}{' (risky)' if g['risky'] else ''}: {g['label']}")
        for i in g["files"]:
            print(f"      {files[i]['path']}")
    print(f"NEXT: per group, plan (if yes) then `native.py render --out {out} --group N --round 1`")


def group_rule(state, g):
    """resolveGroupSystemRule: one distinct rule -> bare text, else <rules for=...> blocks."""
    by_text = {}
    for i in sorted(g["files"], key=lambda i: state["files"][i]["path"]):
        f = state["files"][i]
        text = state["rules"].get(f["rule"] or "", "")
        by_text.setdefault(text, []).append(f["path"])
    if len(by_text) == 1:
        return next(iter(by_text))
    return "\n".join(f'<rules for="{", ".join(p)}">\n{t}\n</rules>' for t, p in by_text.items())


def write_inputs(out, state, g):
    files = state["files"]
    diffs = "\n\n".join(
        f'<file path="{files[i]["path"]}">\n{open(files[i]["diff"]).read()}\n</file>' for i in g["files"])
    others = [entry(f) for i, f in enumerate(files) if i not in g["files"]]
    # Ours: name the excluded tests and docs too, so a reviewer knows they exist and can read them.
    others += [f"{entry(f)} [not under review: {f.get('exclude_reason')}]" for f in state["excluded"]]
    write(os.path.join(gdir(out, g["id"]), "diffs.txt"), diffs)
    write(os.path.join(gdir(out, g["id"]), "others.txt"), "\n".join(others))
    write(os.path.join(gdir(out, g["id"]), "rule.md"), group_rule(state, g))


def fill(text, out, state, g, plan="", confirmed=""):
    gd = gdir(out, g["id"])
    if not plan:
        text = re.sub(r"(?m)^### [^\n]*Review Plan[^\n]*\n\{\{plan_guidance\}\}\n\n?", "", text)
    if not confirmed:
        text = re.sub(r"(?m)^### [^\n]*Confirmed Findings[^\n]*\n\{\{confirmed_comments\}\}\n\n?", "", text)
    for k, v in [("current_system_date_time", state["started"]),
                 ("system_rule", open(os.path.join(gd, "rule.md")).read()),
                 ("change_files", open(os.path.join(gd, "others.txt")).read()),
                 ("diffs", open(os.path.join(gd, "diffs.txt")).read()),
                 ("requirement_background", state["background"]),
                 ("plan_guidance", plan), ("confirmed_comments", confirmed)]:
        text = text.replace("{{" + k + "}}", v)
    return text


def plan_tools():
    """formatToolDefs over PLAN_TASK.tool_references."""
    defs = {t["name"]: t["definition"] for t in json.load(open(os.path.join(PROMPTS, "tools.json")))}
    lines = ["### Available Tools (reference only — do not call)"]
    for name in ["code_search", "file_read", "file_read_diff", "file_find"]:
        d = defs[name]
        lines.append(f"- **{name}**: {d['description']}")
        props = d.get("parameters", {}).get("properties", {})
        if props:
            req = set(d["parameters"].get("required", []))
            lines.append("  Parameters:")
            for p, spec in props.items():
                lines.append(f"  - {p}: {spec.get('description', '')}{' (required)' if p in req else ''}")
    return "\n".join(lines) + "\n"


def write_plan_prompt(out, state, g):
    system = prompt("plan_task_system").replace("{{plan_tools}}", plan_tools()) + "\n\nAlways respond in English."
    user = fill(prompt("plan_task_user"), out, state, g)
    write(os.path.join(gdir(out, g["id"]), "plan-prompt.md"),
          f"<system>\n{system}\n</system>\n\n{user}")


# ---------- render (buildMainTaskMessages + the Claude adapter) ----------

def adapter(state, g, out_file):
    repo, base, to = state["repo"], state["base"], state["to"]
    new_read = ("Read the file in the working tree" if state["reads_tree"]
                else f"run `git --no-pager show {to}:<path>` (the working tree is NOT the reviewed version)")
    diff_to = "" if state["mode"] == "workspace" else f" {to}"
    return f"""<harness>
You are running inside Claude Code in place of Open Code Review's tool loop. The task above is
unchanged; only the tools are spelled differently. Repository: {repo} (run commands from there).
- file_read (side new): {new_read}. Side old: `git --no-pager show {base}:<path>`.
- code_search: Grep over the repository (or `git grep -n` at {to if to != 'WORKTREE' else 'the working tree'}).
- file_find: Glob.
- file_read_diff: `git --no-pager diff {base}{diff_to} -- <path>` (works for files not under review too).
- code_comment: every finding becomes one object in "comments" of the JSON you write at the end, with
  exactly these fields: {{"content", "existing_code", "suggestion_code" (optional), "category", "severity",
  "path", "thinking" (optional: your reasoning)}}. existing_code must be one or several consecutive lines
  copied exactly from the added (+) lines of the diff, without the leading `+`.
- task_done: write this file with the Write tool and stop:
  {out_file}
  content: {{"state": "DONE" or "FAILED", "files_reviewed": [every path you gave its own pass], "comments": [...]}}
Budget: at most about 100 tool calls; when you near it, stop investigating and write what you have.
Do not edit, create or delete any other file. Your final message: one line, "DONE <n> comments".
</harness>"""


def confirmed_block(confirmed):
    """buildConfirmedCommentsBlock."""
    if not confirmed:
        return ""

    def flat(s, n):
        s = re.sub(r"\r\n|\n|\r", " ", s or "").strip()
        return s if len(s) <= n else s[:n] + "..."
    lines = ["The following issues were already identified and confirmed in a prior review pass. Do not "
             "repeat them. Continue reviewing all files in <review_files> and report any other real "
             "issues you find.", "", "<confirmed_findings>"]
    for i, c in enumerate(confirmed, 1):
        lines.append(f"{i}. {c['path']}")
        if c.get("existing_code"):
            lines.append(f"   code: {flat(c['existing_code'], CONFIRMED_MAX_CODE)}")
        lines.append(f"   issue: {flat(c['content'], CONFIRMED_MAX_CONTENT)}")
    lines.append("</confirmed_findings>")
    return "\n".join(lines)


def find_group(state, gid):
    for g in state["groups"]:
        if g["id"] == gid:
            return g
    sys.exit(f"no group {gid}")


def cmd_render(a):
    out = os.path.abspath(a.out)
    state = load(out)
    g = find_group(state, a.group)
    plan = ""
    pf = os.path.join(gdir(out, g["id"]), "plan.txt")
    if a.round == 1 and g["plan"] and os.path.exists(pf):
        plan = open(pf).read().strip()      # roundPlan: round 1 only
    system = prompt("main_task_system") + "\n\nAlways respond in English."
    user = fill(prompt("main_task_user"), out, state, g, plan=plan, confirmed=confirmed_block(g["confirmed"]))
    out_file = os.path.join(rdir(out, g["id"], a.round), "comments.json")
    path = os.path.join(rdir(out, g["id"], a.round), "prompt.md")
    write(path, f"<system>\n{system}\n</system>\n\n{user}\n\n{adapter(state, g, out_file)}\n")
    print(f"prompt: {path}\noutput: {out_file}\nmodel: {g['model']}")


# ---------- position (internal/diff/resolver.go) ----------

def norm(s):
    s = s.strip()
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("-"):
        s = s[1:]
    return s.strip()


def target_of(code):
    return [n for n in (norm(l) for l in (code or "").split("\n")) if n]


def hunks(diff_text):
    out, cur, old, new = [], None, 0, 0
    for line in diff_text.split("\n"):
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            cur = {"new": [], "old": []}
            out.append(cur)
            old, new = int(m.group(1)), int(m.group(2))
            continue
        if cur is None or line.startswith("\\"):
            continue
        if line.startswith("+"):
            cur["new"].append((new, line[1:]))
            new += 1
        elif line.startswith("-"):
            cur["old"].append((old, line[1:]))
            old += 1
        else:
            body = line[1:] if line.startswith(" ") else line
            cur["new"].append((new, body))
            cur["old"].append((old, body))
            new += 1
            old += 1
    return out


def match(seq, target):
    """matchConsecutive over (line_no, text) pairs; first match wins."""
    if not target or len(target) > len(seq):
        return None
    normed = [norm(t) for _, t in seq]
    for s in range(len(seq) - len(target) + 1):
        if normed[s:s + len(target)] == target:
            return seq[s][0], seq[s + len(target) - 1][0]
    return None


def resolve(state, f, target, cache):
    """Tier 1 for one file: hunk new side, new side without blanks, old side, whole new file."""
    hs = hunks(open(f["diff"]).read())
    for h in hs:
        r = match(h["new"], target)
        if r:
            return r
    for h in hs:
        r = match([(n, t) for n, t in h["new"] if t.strip()], target)
        if r:
            return r
    for h in hs:
        r = match(h["old"], target)
        if r:
            return r              # OCR reports old-file numbers here as if new (a quirk kept)
    if f["path"] not in cache:
        cache[f["path"]] = new_content(state, f["path"])
    whole = [(i, l.rstrip("\r")) for i, l in enumerate(cache[f["path"]].split("\n"), 1) if l.strip()]
    return match(whole, target)


def clean_path(p):
    p = (p or "").replace("\\", "/")
    p = os.path.normpath(p).lstrip("/") if p else ""
    return "" if p == "." else p


def cmd_position(a):
    out = os.path.abspath(a.out)
    state = load(out)
    g = find_group(state, a.group)
    rd = rdir(out, g["id"], a.round)
    raw = read_json(os.path.join(rd, "comments.json"), {}) or {}
    if isinstance(raw, list):
        raw = {"state": "DONE", "comments": raw}
    items = raw.get("comments") or []
    if isinstance(items, str):
        items = read_json_str(items)
    relocated = read_json(a.relocated, {}) or {}
    by_path = {f["path"]: f for f in state["files"]}
    group_paths = [state["files"][i]["path"] for i in g["files"]]
    cache, placed, unplaced = {}, [], []
    for idx, c in enumerate(items):
        if not isinstance(c, dict) or not str(c.get("content") or "").strip():
            continue
        path = clean_path(c.get("path")) or (group_paths[0] if len(group_paths) == 1 else "")
        if not path:
            continue
        cat = str(c.get("category") or "").lower()
        sev = str(c.get("severity") or "").lower()
        rec = {"path": path, "content": c["content"].strip(),
               "existing_code": c.get("existing_code") or "",
               "suggestion_code": c.get("suggestion_code") or "",
               "category": cat if cat in CATEGORIES else "other",
               "severity": sev if sev in SEVERITIES else "low",
               "thinking": c.get("thinking") or "", "round": a.round, "start_line": 0, "end_line": 0}
        target = target_of(rec["existing_code"])
        r = resolve(state, by_path[path], target, cache) if path in by_path else None
        if r is None and target:     # tier 2: exactly one other reviewed file
            hits = [(p, x) for p, f in by_path.items() if p != path
                    for x in [resolve(state, f, target, cache)] if x]
            if len(hits) == 1:
                rec["refiled_from"] = path
                rec["path"], r = hits[0]
        snippet = relocated.get(str(idx))
        if r is None and snippet and path in by_path:   # tier 3 answer: retry tier 1 on the same file
            r = resolve(state, by_path[path], target_of(snippet), cache)
            if r:
                rec["existing_code"] = snippet
        if r:
            rec["start_line"], rec["end_line"] = r
        elif path in by_path and not snippet:
            unplaced.append((idx, rec))
        rec["index"] = idx
        placed.append(rec)
    with open(os.path.join(rd, "positioned.json"), "w") as fh:
        json.dump({"state": raw.get("state", "DONE"), "files_reviewed": raw.get("files_reviewed", []),
                   "comments": placed}, fh, indent=1)
    print(f"{len(placed)} comments, {len(placed) - len(unplaced)} placed, {len(unplaced)} unplaced; "
          f"reviewer state {raw.get('state', 'missing')}")
    for idx, rec in unplaced:     # tier 3 prompts (RE_LOCATION_TASK) for the coordinator to run
        d = open(by_path[rec["path"]]["diff"]).read()
        user = (prompt("re_location_task_user").replace("{diff}", d)
                .replace("{existing_code}", rec["existing_code"]).replace("{suggestion_content}", rec["content"]))
        p = os.path.join(rd, f"relocate-{idx}.md")
        write(p, f"<system>\n{prompt('re_location_task_system')}\n</system>\n\n{user}")
        print(f"  relocate: {p}")
    if unplaced:
        print(f"Optional: run each relocate prompt (haiku), save {{\"<index>\": \"<code block content>\"}} "
              f"to {rd}/relocated.json, rerun position with --relocated. Unplaced comments stay at line 0.")


def read_json_str(s):
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


# ---------- filter (agent.go: executeGroupReviewFilter) ----------

def cmd_filter(a):
    out = os.path.abspath(a.out)
    state = load(out)
    g = find_group(state, a.group)
    rd = rdir(out, g["id"], a.round)
    pos = read_json(os.path.join(rd, "positioned.json"), {}) or {}
    paths = {state["files"][i]["path"] for i in g["files"]}
    cands = [c for c in pos.get("comments", []) if c["path"] in paths]
    if not cands:
        print("skip: no new comments this round")
        return
    compact = []
    for i, c in enumerate(cands):
        item = {"id": f"c-{i}", "path": c["path"], "content": c["content"]}
        if c.get("existing_code"):
            item["existing_code"] = c["existing_code"]
        compact.append(item)
    comments = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    user = (prompt("review_filter_task_user")
            .replace("{{diff}}", open(os.path.join(gdir(out, g["id"]), "diffs.txt")).read())
            .replace("{{comments}}", comments).replace("{{path}}", g["key"]))
    harness = (f"<harness>\nYou cannot call those two tools here. Use no tools at all except Write: judge "
               f"from this prompt alone (the diffs above are all you may see). Write your call to "
               f"{rd}/filter.json as JSON, keys in this order: either "
               f'{{"tool": "report_incorrect_comments", "analysis": ["<one entry per candidate>"], '
               f'"comment_ids": ["c-N", ...]}} or {{"tool": "approve_all_comments"}}. '
               f"Final message: one line, the tool you chose.\n</harness>")
    p = os.path.join(rd, "filter-prompt.md")
    write(p, f"<system>\n{prompt('review_filter_task_system')}\n</system>\n\n{user}\n\n{harness}\n")
    print(f"prompt: {p}\noutput: {rd}/filter.json\ncandidates: {len(cands)}")


def cmd_apply(a):
    out = os.path.abspath(a.out)
    state = load(out)
    g = find_group(state, a.group)
    rd = rdir(out, g["id"], a.round)
    pos = read_json(os.path.join(rd, "positioned.json"), {}) or {}
    paths = {state["files"][i]["path"] for i in g["files"]}
    cands = [c for c in pos.get("comments", []) if c["path"] in paths]
    outside = [c for c in pos.get("comments", []) if c["path"] not in paths]
    resp = read_json(a.response or os.path.join(rd, "filter.json"), None)
    remove = set()
    if isinstance(resp, dict) and resp.get("tool") != "approve_all_comments":
        ids = resp.get("comment_ids") or []
    elif isinstance(resp, list):
        ids = resp
    else:
        ids = []                          # approve, unparseable or missing: the filter fails open
    for cid in ids:
        m = re.fullmatch(r"c-(\d+)", str(cid))
        if m and int(m.group(1)) < len(cands):
            remove.add(int(m.group(1)))
    kept = [c for i, c in enumerate(cands) if i not in remove] + outside
    g["dropped"] += [{**c, "dropped_by": "review filter"} for i, c in enumerate(cands) if i in remove]
    g["confirmed"] += kept
    completed = pos.get("state") == "DONE"
    reason = None
    if not completed:
        reason = "reviewer did not finish (no DONE)"
    elif not kept:
        reason = "added no new findings; stopping early"
    elif len(g["confirmed"]) >= CONFIRMED_CAP:
        reason = f"{CONFIRMED_CAP} confirmed findings"
    elif a.round >= state["rounds"]:
        reason = "last round"
    g["files_reviewed"] = sorted(set(g.get("files_reviewed", [])) | set(pos.get("files_reviewed", [])))
    if reason:
        g["done"], g["stopped"] = True, reason
    save(out, state)
    print(f"group {g['id']} round {a.round}: kept {len(kept)}, removed {len(remove)}; "
          + (f"STOP ({reason})" if reason else f"NEXT round {a.round + 1}"))


# ---------- report (shared.go: emitRunResult) ----------

def cmd_report(a):
    out = os.path.abspath(a.out)
    state = load(out)
    allc, dropped = [], []
    for g in state["groups"] or []:
        for c in g["confirmed"]:
            allc.append({**c, "group": g["id"], "model": g["model"]})
        dropped += [{**c, "group": g["id"]} for c in g["dropped"]]
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    allc.sort(key=lambda c: rank.get(c["severity"], 4))     # ours: most severe first
    verdicts = read_json(a.verdicts, []) or []              # ours: the Opus check, by findings.json index
    for v in verdicts if isinstance(verdicts, list) else []:
        i = v.get("index") if isinstance(v, dict) else None
        if isinstance(i, int) and 0 <= i < len(allc):
            allc[i]["verdict"] = f"{v.get('verdict')}: {v.get('reason', '')}".strip()
            if str(v.get("severity") or "").lower() in SEVERITIES:
                allc[i]["severity"] = v["severity"].lower()
    rejected = [c for c in allc if str(c.get("verdict", "")).startswith("REJECTED")]
    allc = [c for c in allc if c not in rejected]
    allc.sort(key=lambda c: rank.get(c["severity"], 4))
    with open(os.path.join(out, "findings.json"), "w") as fh:
        json.dump({"comments": allc, "dropped": dropped, "rejected": rejected}, fh, indent=1)
    lines = [f"# OCR native-port review of {state['repo']}",
             f"{state['mode']}: base {state['base'][:10]}, to {state['to'][:10]}; effort {state['effort']}", "",
             "## Coverage"]
    for g in state["groups"] or []:
        paths = [state["files"][i]["path"] for i in g["files"]]
        seen = set(g.get("files_reviewed", []))
        miss = [p for p in paths if p not in seen]
        lines.append(f"- group {g['id']} ({g['model']}, stopped: {g['stopped']}): {', '.join(paths)}"
                     + (f" — reviewer did not list: {', '.join(miss)}" if miss else ""))
    for f in state["excluded"]:
        lines.append(f"- excluded {f['path']}: {f.get('exclude_reason')}")
    lines += ["", f"## Findings ({len(allc)})"]
    for c in allc:
        lines.append(f"- **{c['severity']} · {c['category']}** `{c['path']}:{c['start_line']}-{c['end_line']}` "
                     f"(round {c['round']}{', re-filed from ' + c['refiled_from'] if c.get('refiled_from') else ''})")
        lines.append(f"  {c['content']}")
        if c.get("verdict"):
            lines.append(f"  verdict: {c['verdict']}")
    lines += ["", f"## Removed by the review filter ({len(dropped)})"]
    lines += [f"- `{c['path']}:{c['start_line']}` {c['content'][:200]}" for c in dropped]
    if rejected:
        lines += ["", f"## Rejected by the Opus check ({len(rejected)})"]
        lines += [f"- `{c['path']}:{c['start_line']}` {c['content'][:200]} ({c['verdict']})" for c in rejected]
    write(os.path.join(out, "report.md"), "\n".join(lines) + "\n")
    counts = {s: sum(1 for c in allc if c["severity"] == s) for s in SEVERITIES}
    print(f"{len(allc)} findings {counts}, {len(dropped)} removed by the filter")
    print(f"report: {out}/report.md\nfindings: {out}/findings.json")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--out", required=True)
    pl.add_argument("--repo", default=".")
    pl.add_argument("--from", dest="from_ref")
    pl.add_argument("--to", dest="to_ref")
    pl.add_argument("--commit")
    pl.add_argument("--background")
    pl.add_argument("--exclude")
    pl.add_argument("--risky", default=DEFAULT_RISKY)
    pl.add_argument("--effort", choices=list(ROUNDS), default="medium")
    for name in ("groups", "report"):
        s = sub.add_parser(name)
        s.add_argument("--out", required=True)
        if name == "groups":
            s.add_argument("--response")
        else:
            s.add_argument("--verdicts")
    for name in ("render", "position", "filter", "apply"):
        s = sub.add_parser(name)
        s.add_argument("--out", required=True)
        s.add_argument("--group", type=int, required=True)
        s.add_argument("--round", type=int, required=True)
        if name == "position":
            s.add_argument("--relocated")
        if name == "apply":
            s.add_argument("--response")
    a = p.parse_args()
    {"plan": cmd_plan, "groups": cmd_groups, "render": cmd_render, "position": cmd_position,
     "filter": cmd_filter, "apply": cmd_apply, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
