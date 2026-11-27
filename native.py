#!/usr/bin/env python3
"""Deterministic half of ocr-review: Open Code Review's native pipeline, minus the model calls.

The coordinator runs each step, hands the rendered prompt files to Claude subagents
(on the user's subscription, all on one model, as OCR uses one model), saves their
answers where this script says, and runs the next step. Nothing here calls a model.

What OCR's CLI can do, it does: `ocr delegate preview` picks the files and resolves the
background (sanitised and wrapped), `ocr delegate rule` resolves the rules (including a
`--rule` file). Everything OCR does not expose as a command (diff text, grouping, prompt
rendering, positioning, round bookkeeping) is ported from its Go source at commit
e74f2c6 (alibaba/open-code-review, Apache-2.0); each function names the Go function it
follows. Prompts in prompts/ are OCR's, verbatim.

  native.py plan     --out DIR [--repo P] [--from REF [--to REF] | --commit SHA]
                     [--background-file F | --background TEXT] [--rule F] [--exclude GLOBS]
                     [--effort low|medium|high] [--model opus|sonnet] [--list-excluded] [--language L]
  native.py groups   --out DIR [--response FILE]
  native.py render   --out DIR --group N --round R
  native.py position --out DIR --group N --round R [--relocated FILE]
  native.py filter   --out DIR --group N --round R
  native.py apply    --out DIR --group N --round R [--response FILE]
  native.py report   --out DIR [--verdicts FILE]
"""
import argparse
import datetime
import json
import os
import posixpath
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS = os.path.join(HERE, "prompts")
PINNED = "e74f2c6"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# OCR constants (task_template.json, grouping.go, util.go, agent.go).
GROUPING_MIN_FILES = 4
GROUPING_BUNDLE_LINE_THRESHOLD = 200
MAX_FILES_PER_GROUP = 10
TOKEN_LIMIT = 160000            # 0.8 * MAX_TOKENS (200000)
PLAN_MODE_LINE_THRESHOLD = 50
PLAN_MODE_GROUP_LINE_THRESHOLD = 100
CONFIRMED_CAP = 30
CONFIRMED_MAX_CODE = 200
CONFIRMED_MAX_CONTENT = 300
MAX_UNTRACKED_BYTES = 64 << 20
ROUNDS = {"low": 1, "medium": 2, "high": 3}
CATEGORIES = {"bug", "security", "performance", "maintainability", "test", "style",
              "documentation", "other"}
SEVERITIES = ["critical", "high", "medium", "low"]
# Only with --list-excluded (not OCR): excluded files a reviewer may still want to read.
CONTEXT_REASONS = {"default_path", "unsupported_ext", "user_exclude", "too_large"}

# OCR's exclude reasons, in words for the report.
REASONS = {"default_path": "OCR's default excludes (tests, fixtures, build output, lock files)",
           "unsupported_ext": "file type OCR does not review (docs, data, images)",
           "user_exclude": "excluded by --exclude or a rule file",
           "provider_directory": "vendored or dependency directory",
           "binary": "binary file", "secret_exclude": "secrets file", "deleted": "deleted",
           "too_large": "diff over OCR's 160,000-token limit"}

GRACE = ("Your review budget is exhausted. This is your FINAL round. You may ONLY:\n"
         "- Call code_comment to submit any findings you have identified but not yet reported.\n"
         "- Call task_done if you have nothing more to report.\n"
         "No other tools are available. Do not attempt further analysis.")

# agent.go: filterTools, verbatim (property order is load-bearing: analysis before comment_ids).
FILTER_TOOLS = [
    {"name": "report_incorrect_comments",
     "description": "Report review comments that this diff proves to be factually wrong: either the code they "
                    "target is absent from the diff, or one diff line literally contradicts their central claim. "
                    "For every id listed you must be able to name that line. Do not use this for comments you "
                    "merely find unconvincing, unverifiable, or low-value, nor for comments about memory safety, "
                    "concurrency, linkage consistency, unused parameters, or behavioral changes.",
     "parameters": {"type": "object", "properties": {
         "analysis": {"type": "array", "description":
                      "Work through every candidate comment BEFORE deciding. One entry per candidate: its id, "
                      "whether its subject hits the protected-subject veto (Step 1) or the value veto (Step 2), "
                      "the exact diff line that refutes it if any, and your final call. Only ids you conclude "
                      "here as removable may appear in comment_ids.", "items": {"type": "string"}},
         "comment_ids": {"type": "array", "description":
                         "IDs concluded removable in analysis, e.g. [\"c-0\", \"c-2\"]. Must not be empty.",
                         "items": {"type": "string"}}},
         "required": ["analysis", "comment_ids"]}},
    {"name": "approve_all_comments",
     "description": "Keep every review comment. Call this whenever no comment clears the removal bar — "
                    "including when comments look doubtful, cannot be verified from the diff alone, or seem "
                    "minor. This is the expected outcome for most files.",
     "parameters": {"type": "object", "properties": {}}},
]


# ---------- helpers ----------

def git_bytes(repo, *args, check=True):
    res = subprocess.run(["git", "--no-pager", "-c", "core.quotepath=false", *args],
                         cwd=repo, capture_output=True)
    if check and res.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {res.stderr.decode(errors='replace').strip()}")
    return res.stdout if res.returncode == 0 else None


def git(repo, *args, check=True):
    out = git_bytes(repo, *args, check=check)
    return None if out is None else out.decode("utf-8", errors="replace")


def resolve_cmd(cmd):
    # Windows: npm installs `ocr` as ocr.cmd, which CreateProcess does not find by bare name.
    import shutil
    found = shutil.which(cmd[0])
    return [found, *cmd[1:]] if found else cmd


def run_json(cmd, cwd):
    res = subprocess.run(resolve_cmd(cmd), cwd=cwd, capture_output=True)
    if res.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{res.stderr.decode(errors='replace').strip()}")
    data = json.loads(res.stdout.decode("utf-8", errors="replace"))
    if str(data.get("schema_version", "1")) != "1":
        sys.exit(f"unexpected ocr schema_version {data.get('schema_version')}; update native.py")
    return data


def prompt(name):
    """template.go: prompt files are right-trimmed of \\r\\n."""
    with uopen(os.path.join(PROMPTS, name + ".md")) as fh:
        return fh.read().rstrip("\r\n")


def substitute(text, values, pattern=r"\{\{(\w+)\}\}"):
    """Fill every placeholder in one pass, so text inserted for one placeholder is never scanned
    for another. OCR substitutes one after another (agent.go: buildMainTaskMessages,
    executeGroupReviewFilter; diff/relocation.go), which lets a diff that contains a placeholder
    name receive another value; this is a declared improvement (README)."""
    return re.sub(pattern, lambda m: values.get(m.group(1), m.group(0)), text)


def uopen(path, mode="r", **kw):
    """Text files are UTF-8 whatever the locale; undecodable bytes read as U+FFFD."""
    kw.setdefault("encoding", "utf-8")
    if "r" in mode:
        kw.setdefault("errors", "replace")
    return open(path, mode, **kw)


def tokens(text):
    """OCR counts cl100k tokens; chars/3.5 errs towards OCR's gates (code runs ~3-3.5 chars/token)."""
    return int(len(text) / 3.5)


def load(out):
    with uopen(os.path.join(out, "state.json")) as fh:
        return json.load(fh)


def save(out, state):
    with uopen(os.path.join(out, "state.json"), "w") as fh:
        json.dump(state, fh, indent=1)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with uopen(path, "w") as fh:
        fh.write(text)


def read_text(path):
    if not path or not os.path.exists(path):
        return None
    with uopen(path, errors="replace") as fh:
        return fh.read()


def read_answer(path):
    """A subagent's JSON answer file. None when missing or unparseable (parseToolArgs-style salvage)."""
    text = read_text(path)
    if text is None:
        return None
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                return None
    return None


def s(v):
    """tool/code_comment.go: a non-string field reads as empty."""
    return v if isinstance(v, str) else ""


def status_word(f):
    return {"added": "ADDED", "deleted": "DELETED", "renamed": "RENAMED"}.get(f["status"], "MODIFIED")


def entry(f):
    """formatDiffEntry: `<STATUS>   <path> (+i/-d)`."""
    return f"{status_word(f)}   {f['path']} (+{f['insertions']}/-{f['deletions']})"


def gdir(out, g):
    return os.path.join(out, f"group-{g}")


def rdir(out, g, r):
    return os.path.join(gdir(out, g), f"round-{r}")


# ---------- diffs (diff/git.go: GetDiffSet, diff/parser.go: ParsePatch) ----------

DIFF_FLAGS = ["--no-ext-diff", "--no-textconv", "--find-renames", "--src-prefix=a/",
              "--dst-prefix=b/", "--no-color", "-U3"]

_ESC = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_git_path(s_):
    """quotedpath.go: git's C-style quoting of a path ("..." with escapes and octal bytes)."""
    if not s_.startswith('"'):
        return s_, ""
    out, i = bytearray(), 1
    while i < len(s_):
        c = s_[i]
        if c == '"':
            return out.decode("utf-8", errors="replace"), s_[i + 1:]
        if c == "\\" and i + 1 < len(s_):
            n = s_[i + 1]
            if n in _ESC:
                out.append(_ESC[n])
                i += 2
                continue
            if re.match(r"[0-7]{3}", s_[i + 1:i + 4]):
                out.append(int(s_[i + 1:i + 4], 8))
                i += 4
                continue
        out += c.encode("utf-8")
        i += 1
    return s_, ""


def parse_header(line):
    """parser.go: parseDiffHeaderLine (same-path split, then regex, then quoted)."""
    if not line.startswith("diff --git "):
        return None
    rest = line[len("diff --git "):]
    for i in range(len(rest) - 2):
        if rest[i:i + 3] == " b/" and rest[:i].startswith("a/") and rest[2:i] == rest[i + 3:]:
            return rest[2:i], rest[i + 3:]
    m = re.match(r"^a/(.+?) b/(.+)$", rest)
    if m:
        return m.group(1), m.group(2)
    if rest.startswith('"'):
        first, tail = unquote_git_path(rest)
    elif " " in rest:
        first, tail = rest.split(" ", 1)
    else:
        return None
    second, _ = unquote_git_path(tail.lstrip(" "))
    if first.startswith("a/") and second.startswith("b/"):
        return first[2:], second[2:]
    return None


def parse_patch(text):
    """parser.go: ParsePatch. Splits on \\n only, trims one \\r, drops `index ` lines outside hunks."""
    diffs, cur, buf, in_hunk = [], None, [], False
    for line in text.split("\n"):
        line = line[:-1] if line.endswith("\r") else line
        hdr = parse_header(line)
        if hdr:
            if cur is not None:
                cur["text"] = "\n".join(buf)
                diffs.append(cur)
            cur = {"old": hdr[0], "new": hdr[1], "new_file": False, "deleted": False,
                   "renamed": False, "binary": False, "ins": 0, "dels": 0}
            buf, in_hunk = [], False
        if cur is None:
            continue
        if line.startswith("@@"):
            in_hunk = True
        elif not in_hunk and line.startswith("index "):
            continue
        elif not in_hunk and line.startswith("Binary files "):
            cur["binary"] = True
        elif line.startswith("new file mode "):
            cur["new_file"] = True
        elif line.startswith("deleted file mode "):
            cur["deleted"] = True
        elif line.startswith("rename from "):
            cur["old"] = unquote_git_path(line[len("rename from "):])[0]
            cur["renamed"] = True
        elif line.startswith("rename to "):
            cur["new"] = unquote_git_path(line[len("rename to "):])[0]
            cur["renamed"] = True
        elif not in_hunk and line.startswith("copy from "):
            cur["old"] = unquote_git_path(line[len("copy from "):])[0]
        elif not in_hunk and line.startswith("copy to "):
            cur["new"] = unquote_git_path(line[len("copy to "):])[0]
        elif not in_hunk and line == "--- /dev/null":
            cur["new_file"] = True
        elif not in_hunk and line == "+++ /dev/null":
            cur["deleted"] = True
        elif in_hunk and line.startswith("+"):
            cur["ins"] += 1
        elif in_hunk and line.startswith("-"):
            cur["dels"] += 1
        buf.append(line)
    if cur is not None:
        cur["text"] = "\n".join(buf)
        diffs.append(cur)
    for d in diffs:   # trailing newline trimmed (TrimSuffix "\n")
        d["text"] = d["text"][:-1] if d["text"].endswith("\n") else d["text"]
        d["path"] = d["old"] if d["deleted"] else d["new"]
    return diffs


def untracked_diffs(repo):
    """git.go: untrackedFileDiffs, the synthetic diff OCR builds for untracked files."""
    out = git_bytes(repo, "ls-files", "-z", "--others", "--exclude-standard") or b""
    texts = []
    for raw in [p for p in out.split(b"\0") if p]:
        f = raw.decode("utf-8", errors="replace")
        path = os.path.join(repo, f)
        try:
            content = None if os.path.getsize(path) > MAX_UNTRACKED_BYTES else open(path, "rb").read()
        except OSError:
            continue
        if content is None or b"\0" in content[:8000]:
            texts.append(f"diff --git a/{f} b/{f}\nnew file mode 100644\nBinary files /dev/null and b/{f} differ\n")
            continue
        count = content.count(b"\n") + (1 if content and not content.endswith(b"\n") else 0)
        lines = content.split(b"\n")
        if lines and lines[-1] == b"":
            lines = lines[:-1]
        body = "".join("+" + l.decode("utf-8", errors="replace") + "\n" for l in lines)
        texts.append(f"diff --git a/{f} b/{f}\n--- /dev/null\n+++ b/{f}\n@@ -0,0 +1,{count} @@\n{body}")
    return "".join(texts)


def full_diff(state):
    repo, base, to, mode = state["repo"], state["base"], state["to"], state["mode"]
    if mode == "commit":
        return git(repo, "show", *DIFF_FLAGS, "--diff-merges=first-parent", "--end-of-options", to)
    if mode == "range":
        return git(repo, "diff", *DIFF_FLAGS, "--end-of-options", base, to, "--")
    text = git(repo, "-c", "diff.autoRefreshIndex=false", "diff", *DIFF_FLAGS,
               "--end-of-options", base, "--")
    return text + untracked_diffs(repo)


def new_content(state, path):
    if state["mode"] == "workspace":
        p = os.path.join(state["repo"], path)
        return uopen(p, errors="replace").read() if os.path.exists(p) else ""
    return git(state["repo"], "show", "--end-of-options", f"{state['to']}:{path}", check=False) or ""


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
    if a.rule:
        common += ["--rule", os.path.abspath(os.path.expanduser(a.rule))]
    pv_flags = []
    if a.background_file:
        pv_flags = ["--background-file", os.path.abspath(os.path.expanduser(a.background_file))]
    elif a.background:
        pv_flags = ["--background", a.background]
    preview = run_json(["ocr", "delegate", "preview"] + common + pv_flags, repo)
    version = (subprocess.run(resolve_cmd(["ocr", "--version"]), capture_output=True, text=True).stdout.strip()
               .split("\n") or [""])[0]

    mode = preview["mode"]
    head = git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False)
    head = head.strip() if head else EMPTY_TREE
    if mode == "range":
        base = preview["merge_base"]
        to = git(repo, "rev-parse", "--verify", (preview.get("to") or "HEAD") + "^{commit}").strip()
    elif mode == "commit":
        to = git(repo, "rev-parse", "--verify", a.commit + "^{commit}").strip()
        parent = git(repo, "rev-parse", "-q", "--verify", f"{to}^1", check=False)
        base = parent.strip() if parent else EMPTY_TREE      # comparison.go: a root commit's base is empty
    else:
        base, to = head, "WORKTREE"                          # workspace.go: unborn HEAD -> empty tree
    dirty = bool((git(repo, "status", "--porcelain", "--untracked-files=no", check=False) or "").strip())
    reads_tree = mode == "workspace" or (to == head and not dirty)

    state = {
        "repo": repo, "mode": mode, "base": base, "to": to, "reads_tree": reads_tree,
        "effort": a.effort, "rounds": ROUNDS[a.effort], "model": a.model, "language": a.language,
        "background": preview.get("background") or "",
        "started": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ocr_version": version, "prompts_pinned": PINNED, "list_excluded": a.list_excluded,
        "excluded": preview.get("excluded_files") or [], "files": [], "working_set": [],
        "groups": None,
    }

    parsed = parse_patch(full_diff(state))
    os.makedirs(os.path.join(out, "diffs"), exist_ok=True)
    diffmap = {}
    for i, d in enumerate(parsed):
        dp = os.path.join(out, "diffs", f"{i}.diff")
        write(dp, d["text"])
        diffmap[d["path"]] = dp
        if d["renamed"] and d["old"] != d["path"]:
            diffmap.setdefault(d["old"], dp)
    with uopen(os.path.join(out, "diffmap.json"), "w") as fh:
        json.dump(diffmap, fh, indent=1)

    by_path = {d["path"]: d for d in parsed}
    reviewable = {f["path"]: f for f in preview.get("reviewable_files") or []}
    deleted = {f["path"] for f in state["excluded"] if f.get("exclude_reason") == "deleted"}
    missing = [p for p in reviewable if p not in by_path]
    if missing:
        sys.exit(f"ocr listed files the diff does not contain: {missing}")
    for d in parsed:                       # git order, as OCR's a.diffs
        p = d["path"]
        if p in reviewable:
            f = reviewable[p]
            if tokens(d["text"]) > TOKEN_LIMIT:     # selection.go gate 8: ExcludeTooLarge
                state["excluded"].append({**f, "exclude_reason": "too_large"})
                continue
            rec = {"path": p, "status": f["status"], "insertions": d["ins"], "deletions": d["dels"],
                   "diff": diffmap[p]}
            state["files"].append(rec)
            state["working_set"].append({k: rec[k] for k in ("path", "status", "insertions", "deletions")})
        elif p in deleted and not d["binary"]:
            state["working_set"].append({"path": p, "status": "deleted", "insertions": d["ins"],
                                         "deletions": d["dels"]})

    if not state["files"]:
        save(out, state)
        print("Nothing to review.")
        return

    rule_text, rule_of = {}, {}
    paths = [f["path"] for f in state["files"]]
    for s0 in range(0, len(paths), 200):          # chunked: argv limits; keyed by text, not group id
        rules = run_json(["ocr", "delegate", "rule"] + common + paths[s0:s0 + 200], repo)
        for g in rules.get("groups", []):
            key = next((k for k, t in rule_text.items() if t == g["rule"]), str(len(rule_text) + 1))
            rule_text[key] = g["rule"]
            for p in g["files"]:
                rule_of[p] = key
    state["rules"] = rule_text
    for f in state["files"]:
        f["rule"] = rule_of.get(f["path"])
    save(out, state)

    n = len(state["files"])
    print(f"{mode} review of {repo}: {n} files (+{sum(f['insertions'] for f in state['files'])}"
          f"/-{sum(f['deletions'] for f in state['files'])}), {len(state['excluded'])} excluded, "
          f"effort {a.effort} = {state['rounds']} round(s), model {a.model}; base {base[:10]}, to {to[:10]}; "
          f"{version} with prompts from {PINNED}")
    if n >= GROUPING_MIN_FILES:          # template.go: GroupingPlan
        listing = "".join(f"[{i}] {entry(f)}\n" for i, f in enumerate(state["files"]))
        resp = os.path.join(out, "grouping.json")
        text = (f"<system>\n{prompt('grouping_task_system')}\n</system>\n\n"
                f"{substitute(prompt('grouping_task_user'), {'file_list': listing})}\n\n"
                f"<harness>\nAnswer with no tools other than Write: write only the JSON array to {resp}. "
                f"Final message: one line, \"written\".\n</harness>\n")
        write(os.path.join(out, "grouping-prompt.md"), text)
        print(f"NEXT grouping: subagent on {out}/grouping-prompt.md (writes {resp}), then "
              f"`native.py groups --out {out} --response {resp}`")
    else:
        print(f"NEXT: native.py groups --out {out}")


# ---------- groups (grouping.go: groupDiffs) ----------

def parse_grouping(path):
    """grouping.go: parseGroupingResponse. Strict: any shape error -> None (one group per file)."""
    text = read_text(path)
    if text is None:
        return None
    lines = text.strip().split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    try:
        data = json.loads("\n".join(lines))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    groups = []
    for g in data:
        if not isinstance(g, dict) or not isinstance(g.get("label", ""), str):
            return None
        files = g.get("files") or []
        if not isinstance(files, list) or any(type(i) is not int for i in files):
            return None
        groups.append({"label": g.get("label", ""), "files": files})
    return groups


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
        resp = parse_grouping(a.response)
        used = set()
        for g in resp or []:
            idx = []
            for i in g["files"]:
                if 0 <= i < n and i not in used:
                    used.add(i)
                    idx.append(i)
            if idx:
                groups.append({"label": g["label"], "files": idx})
        for i, f in enumerate(files):
            if i not in used:
                groups.append({"label": f["path"], "files": [i]})
        if resp is None:
            print("grouping answer missing or malformed: one group per file (as OCR)")
    elif churn < GROUPING_BUNDLE_LINE_THRESHOLD:
        groups = [{"label": "small change set", "files": list(range(n))}]
    else:
        groups = [{"label": f["path"], "files": [i]} for i, f in enumerate(files)]

    sized = []                                   # enforceMaxFilesPerGroup
    for g in groups:
        for s0 in range(0, len(g["files"]), MAX_FILES_PER_GROUP):
            sized.append({"label": g["label"], "files": g["files"][s0:s0 + MAX_FILES_PER_GROUP]})
    final = []                                   # enforceGroupTokenBudget
    for g in sized:
        size = sum(tokens(uopen(files[i]["diff"], errors="replace").read()) for i in g["files"])
        if size > TOKEN_LIMIT and len(g["files"]) > 1:
            final += [{"label": f"{g['label']} (split: {files[i]['path']})", "files": [i]} for i in g["files"]]
        else:
            final.append(g)

    state["groups"] = []
    for gi, g in enumerate(final, 1):
        members = [files[i] for i in g["files"]]
        churns = [f["insertions"] + f["deletions"] for f in members]
        total = sum(churns)
        state["groups"].append({
            "id": gi, "label": g["label"], "files": g["files"],
            "key": members[0]["path"] if len(members) == 1 else ",".join(sorted(f["path"] for f in members)),
            "lines": total,
            "plan": max(churns) >= PLAN_MODE_LINE_THRESHOLD                       # PlanRequired
                    or (len(members) >= 2 and total >= PLAN_MODE_GROUP_LINE_THRESHOLD),
            "confirmed": [], "dropped": [], "extra": [], "applied": [],
            "done": False, "failed": False, "stopped": None, "files_reviewed": [],
        })
    save(out, state)
    for g in state["groups"]:
        write_inputs(out, state, g)
        if g["plan"]:
            write_plan_prompt(out, state, g)
    for g in state["groups"]:
        print(f"  group {g['id']}: {len(g['files'])} files, {g['lines']} lines, "
              f"plan {'yes' if g['plan'] else 'no'}: {g['label']}")
        for i in g["files"]:
            print(f"      {files[i]['path']}")
    print(f"NEXT: per group, the plan (if yes), then `native.py render --out {out} --group N --round 1`. "
          f"Every subagent uses model {state['model']}.")


def group_rule(state, g):
    """agent.go: resolveGroupSystemRule (sorted by path, empty rules skipped, one block -> bare text)."""
    by_text = {}
    for i in sorted(g["files"], key=lambda i: state["files"][i]["path"]):
        f = state["files"][i]
        text = state["rules"].get(f["rule"] or "", "")
        if text:
            by_text.setdefault(text, []).append(f["path"])
    if len(by_text) <= 1:
        return next(iter(by_text), "")
    return "\n".join(f'<rules for="{", ".join(p)}">\n{t}\n</rules>' for t, p in by_text.items())


def write_inputs(out, state, g):
    files = state["files"]
    members = {files[i]["path"] for i in g["files"]}
    diffs = "\n\n".join(                         # buildConcatenatedDiffs
        f'<file path="{files[i]["path"]}">\n{uopen(files[i]["diff"], errors="replace").read()}\n</file>'
        for i in g["files"])
    others = [entry(f) for f in state["working_set"] if f["path"] not in members]   # buildChangeFilesExceptGroup
    if state.get("list_excluded"):
        others += [f"{entry(f)} [not under review: {f.get('exclude_reason')}]"
                   for f in state["excluded"] if f.get("exclude_reason") in CONTEXT_REASONS]
    write(os.path.join(gdir(out, g["id"]), "diffs.txt"), diffs)
    write(os.path.join(gdir(out, g["id"]), "others.txt"), "\n".join(others))
    write(os.path.join(gdir(out, g["id"]), "rule.md"), group_rule(state, g))


def fill(text, out, state, g, plan="", confirmed=""):
    """agent.go: buildMainTaskMessages, same substitution order, blocks stripped where OCR strips them."""
    gd = gdir(out, g["id"])
    if not plan:        # the empty blocks are stripped from the template, before any value goes in
        text = re.sub(r"(?m)^### [^\n]*Review Plan[^\n]*\n\{\{plan_guidance\}\}\n\n?", "", text)
    if not confirmed:
        text = re.sub(r"(?m)^### [^\n]*Confirmed Findings[^\n]*\n\{\{confirmed_comments\}\}\n\n?", "", text)
    return substitute(text, {
        "current_system_date_time": state["started"],
        "system_rule": read_text(os.path.join(gd, "rule.md")) or "",
        "change_files": read_text(os.path.join(gd, "others.txt")) or "",
        "diffs": read_text(os.path.join(gd, "diffs.txt")) or "",
        "requirement_background": state["background"],
        "plan_guidance": plan, "confirmed_comments": confirmed})


def tool_defs(names, workspace):
    """tools.json definitions, with toolsconfig/comparison.go: comparisonDefinition applied."""
    raw = json.load(uopen(os.path.join(PROMPTS, "tools.json")))
    defs = {t["name"]: json.loads(json.dumps(t["definition"])) for t in raw}
    out = []
    for name in names:
        d = defs[name]
        if name in ("file_read", "file_find", "code_search"):
            d["parameters"]["properties"]["side"] = {
                "type": "string", "enum": ["new", "old"],
                "description": "The side of the current comparison to access. Defaults to new. "
                               "Paths and line numbers belong to the selected side."}
            desc = d["description"].replace(
                "This tool can only read file content from the modified version (after changes) in git diff.",
                "This tool reads either side of the current comparison.").replace(
                "This tool searches in the current version of files.",
                "This tool searches in the selected side of the current comparison.")
            if workspace:
                desc += ("\n\nOmit side or use side=new for the live working tree, not the index. The working tree "
                         "is not snapshotted and may change during review. Use side=old for HEAD frozen when this "
                         "review started, not HEAD's parent. Before the first commit, the old side is an empty tree. "
                         "For renamed files, use the path on the selected side; paths are not automatically "
                         "translated. An empty old tree contains no files.")
            else:
                desc += ("\n\nOmit side or use side=new for the reviewed new version. Use side=old for the frozen "
                         "comparison base, not the current target branch tip. For renamed files, use the path on "
                         "the selected side; paths are not automatically translated. An empty old tree contains "
                         "no files.")
            d["description"] = desc
        out.append(d)
    return out


def format_tool_defs(defs, nested=False):
    """agent.go: formatToolDefs (parameters in definition order). `nested` (the reviewer's harness
    only, not OCR's plan prompt) also lists array items' fields and enum values, which OCR's API
    hands the model as the tool schema."""
    lines = ["### Available Tools (reference only — do not call)"]
    for d in defs:
        lines.append(f"- **{d['name']}**: {d['description']}")
        props = d.get("parameters", {}).get("properties", {})
        if props:
            req = set(d["parameters"].get("required", []))
            lines.append("  Parameters:")
            for p, spec in props.items():
                lines.append(f"  - {p}: {spec.get('description', '')}{' (required)' if p in req else ''}"
                             + (f" One of: {', '.join(spec['enum'])}." if nested and spec.get("enum") else ""))
                items = spec.get("items") or {}
                if nested and items.get("properties"):
                    ireq = set(items.get("required", []))
                    lines.append("    Each item:")
                    for ip, ispec in items["properties"].items():
                        enum = f" One of: {', '.join(ispec['enum'])}." if ispec.get("enum") else ""
                        lines.append(f"    - {ip}: {ispec.get('description', '')}"
                                     f"{' (required)' if ip in ireq else ''}{enum}")
    return "\n".join(lines) + "\n"


def write_plan_prompt(out, state, g):
    tools = format_tool_defs(tool_defs(["code_search", "file_read", "file_read_diff", "file_find"],
                                       state["mode"] == "workspace"))
    system = (substitute(prompt("plan_task_system"), {"plan_tools": tools})
              + f"\n\nAlways respond in {state.get('language', 'English')}.")
    user = fill(prompt("plan_task_user"), out, state, g)
    pf = os.path.join(gdir(out, g["id"]), "plan.txt")
    write(os.path.join(gdir(out, g["id"]), "plan-prompt.md"),
          f"<system>\n{system}\n</system>\n\n{user}\n\n<harness>\nThe tools above are reference only: use no "
          f"tools other than Write. Write your whole plan, in the output format above, to {pf}. Final message: "
          f"one line, \"written\".\n</harness>\n")


# ---------- render (buildMainTaskMessages + checkPromptBudget + the Claude adapter) ----------

def adapter(state, out, out_file):
    repo, base, to = state["repo"], state["base"], state["to"]
    tools = format_tool_defs(tool_defs(["task_done", "code_comment", "code_search", "file_read",
                                        "file_read_diff", "file_find"], state["mode"] == "workspace"), nested=True)
    new_read = ("Read the file in the working tree" if state["reads_tree"]
                else f"`git --no-pager show {to}:<path>` (the working tree is NOT the reviewed version)")
    old = ("the old side is an empty tree (no files)" if base == EMPTY_TREE
           else f"`git --no-pager show {base}:<path>`")
    grep_new = "" if state["reads_tree"] else f", or `git grep -n <text> {to}`"
    return f"""<harness>
You run inside Claude Code in place of Open Code Review's tool loop. The task above is unchanged. These are
the tools OCR gives you, verbatim, followed by how to call each one here.

{tools}
How to call them here (repository {repo}; run commands from there):
- file_read: side new: {new_read}; side old: {old}.
- code_search: Grep (side new{grep_new}); side old: `git grep -n <text> {base}`.
- file_find: Glob (side new); side old: `git ls-tree -r --name-only {base}`.
- file_read_diff: look the path up in {out}/diffmap.json and Read the file it names (any changed file in this run).
- code_comment: each comment becomes one object in "comments" of your answer file, with exactly the fields of
  code_comment's items above (content, existing_code, suggestion_code, category, severity, path).
- task_done: write your answer file with the Write tool, then stop:
  {out_file}
  {{"state": "DONE" or "FAILED", "comments": [ ... ], "files_reviewed": [every path you looked at]}}
  FAILED is task_done's FAILED: you could not complete the task with the tools you have.
Budget: about 100 tool calls. When you reach it, this applies:
{GRACE}
Do not create, edit or delete any other file. Final message: one line, "DONE <n> comments" or "FAILED".
</harness>"""


def confirmed_block(confirmed):
    """util.go: buildConfirmedCommentsBlock."""
    if not confirmed:
        return ""

    def flat(v, n):
        v = re.sub(r"\r\n|\n|\r", " ", v or "").strip()
        return v if len(v) <= n else v[:n] + "..."
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
        plan = (read_text(pf) or "").replace("<think>", "").replace("</think>", "").strip()
    system = prompt("main_task_system") + f"\n\nAlways respond in {state.get('language', 'English')}."
    user = fill(prompt("main_task_user"), out, state, g, plan=plan, confirmed=confirmed_block(g["confirmed"]))
    if tokens(system + user) > TOKEN_LIMIT:      # checkPromptBudget
        if a.round == 1:
            g["failed"], g["done"], g["stopped"] = True, True, "prompt over the token budget"
        else:
            g["done"], g["stopped"] = True, "prompt over the token budget"
        save(out, state)
        print(f"STOP: group {g['id']} round {a.round} prompt is over {TOKEN_LIMIT} tokens"
              + (" (group failed)" if a.round == 1 else ""))
        return
    out_file = os.path.join(rdir(out, g["id"], a.round), "comments.json")
    path = os.path.join(rdir(out, g["id"], a.round), "prompt.md")
    write(path, f"<system>\n{system}\n</system>\n\n{user}\n\n{adapter(state, out, out_file)}\n")
    print(f"prompt: {path}\noutput: {out_file}\nmodel: {state['model']}")


# ---------- position (diff/resolver.go, reviewtask/tools.go, diff/relocation.go) ----------

def norm(v):
    v = v.strip()
    if v.startswith("+"):
        v = v[1:]
    if v.startswith("-"):
        v = v[1:]
    return v.strip()


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
    """matchConsecutive: first match wins."""
    if not target or len(target) > len(seq):
        return None
    normed = [norm(t) for _, t in seq]
    for s0 in range(len(seq) - len(target) + 1):
        if normed[s0:s0 + len(target)] == target:
            return seq[s0][0], seq[s0 + len(target) - 1][0]
    return None


def resolve(state, f, target, cache):
    """ResolveComment: hunk new side, new side without blank lines, old side, whole new file."""
    hs = hunks(uopen(f["diff"], errors="replace").read())
    for h in hs:
        r = match(h["new"], target)
        if r:
            return r
    for h in hs:
        r = match([(n, t) for n, t in h["new"] if norm(t)], target)
        if r:
            return r
    for h in hs:
        r = match(h["old"], target)
        if r:
            return r              # OCR reports old-file numbers here as if new (a quirk kept)
    if f["path"] not in cache:
        cache[f["path"]] = new_content(state, f["path"])
    whole = [(i, l.rstrip("\r")) for i, l in enumerate(cache[f["path"]].split("\n"), 1) if norm(l)]
    return match(whole, target)


def clean_path(p):
    p = p.replace("\\", "/")
    if not p:
        return ""
    p = posixpath.normpath(p)
    p = p[1:] if p.startswith("/") else p
    return "" if p == "." else p


def extract_code_block(text):
    """relocation.go: extractCodeBlock."""
    text = (text or "").strip()
    start = text.find("```")
    if start < 0:
        return ""
    nl = text.find("\n", start + 3)
    if nl < 0:
        return ""
    end = text.find("```", nl + 1)
    return "" if end < 0 else text[nl + 1:end].strip()


def cmd_position(a):
    out = os.path.abspath(a.out)
    state = load(out)
    g = find_group(state, a.group)
    rd = rdir(out, g["id"], a.round)
    os.makedirs(rd, exist_ok=True)
    raw = read_answer(os.path.join(rd, "comments.json"))
    if isinstance(raw, list):
        raw = {"state": "DONE", "comments": raw}
    if not isinstance(raw, dict):
        raw = {"state": "FAILED", "comments": [], "why": "answer file missing or unparseable"}
    st = raw.get("state", "DONE")
    st = st if st in ("DONE", "FAILED") else "FAILED"
    items = raw.get("comments") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            items = []
    if not isinstance(items, list):
        items = []
    replies = read_answer(a.relocated) if a.relocated else {}
    replies = replies if isinstance(replies, dict) else {}
    by_path = {f["path"]: f for f in state["files"]}
    group_paths = [state["files"][i]["path"] for i in g["files"]]
    cache, recs, unplaced = {}, [], []
    for idx, c in enumerate(items):
        if not isinstance(c, dict) or s(c.get("content")) == "":
            continue
        path = clean_path(s(c.get("path"))) or (group_paths[0] if len(group_paths) == 1 else g["key"])
        cat, sev = s(c.get("category")).lower(), s(c.get("severity")).lower()
        rec = {"path": path, "content": s(c.get("content")), "existing_code": s(c.get("existing_code")),
               "suggestion_code": s(c.get("suggestion_code")),
               "category": cat if cat in CATEGORIES else "other",
               "severity": sev if sev in SEVERITIES else "low",
               "round": a.round, "index": idx,
               "start_line": 0, "end_line": 0}
        target = target_of(rec["existing_code"])
        r = resolve(state, by_path[path], target, cache) if path in by_path else None
        if r is None and target:          # RelocateAcrossFiles: exactly one other reviewed file
            hits = [(p, x) for p, f in by_path.items() if p != path
                    for x in [resolve(state, f, target, cache)] if x]
            if len(hits) == 1:
                rec["refiled_from"] = path
                rec["path"], r = hits[0]
        if r is None and path in by_path and str(idx) in replies:     # ReLocateComment's answer
            code = extract_code_block(s(replies[str(idx)]))
            if code:
                r = resolve(state, by_path[path], target_of(code), cache)
                if r:
                    rec["existing_code"] = code
        if r:
            rec["start_line"], rec["end_line"] = r
        elif path in by_path and str(idx) not in replies:
            unplaced.append(rec)
        recs.append(rec)
    with uopen(os.path.join(rd, "positioned.json"), "w") as fh:
        json.dump({"state": st, "why": raw.get("why"), "files_reviewed": raw.get("files_reviewed") or [],
                   "comments": recs}, fh, indent=1)
    placed = sum(1 for r in recs if r["start_line"] > 0)
    print(f"{len(recs)} comments, {placed} placed, {len(recs) - placed} at line 0; reviewer state {st}"
          + (f" ({raw.get('why')})" if raw.get("why") else ""))
    for rec in unplaced:                  # RE_LOCATION_TASK, one per unplaced comment in a known file
        d = uopen(by_path[rec["path"]]["diff"], errors="replace").read()
        user = substitute(prompt("re_location_task_user"),
                          {"diff": d, "existing_code": rec["existing_code"], "suggestion_content": rec["content"]},
                          r"\{(diff|existing_code|suggestion_content)\}")
        p = os.path.join(rd, f"relocate-{rec['index']}.md")
        write(p, f"<system>\n{prompt('re_location_task_system')}\n</system>\n\n{user}\n\n<harness>\nNo tools: "
                 f"answer in your final message exactly as the task says.\n</harness>\n")
        print(f"  relocate {rec['index']}: {p}")
    if unplaced:
        print(f"NEXT: run every relocate prompt (model {state['model']}), save their full replies as "
              f"{{\"<index>\": \"<reply>\"}} to {rd}/relocated.json, then rerun "
              f"`native.py position --out {out} --group {g['id']} --round {a.round} --relocated {rd}/relocated.json`.")


# ---------- filter (agent.go: executeGroupReviewFilter) ----------

def round_candidates(state, g, pos):
    """The round's comments on the group's own files, in group-file order then arrival order."""
    paths = [state["files"][i]["path"] for i in g["files"]]
    comments = pos.get("comments", [])
    cands = [c for p in paths for c in comments if c["path"] == p]
    outside = [c for c in comments if c["path"] not in paths]
    return cands, outside


def cmd_filter(a):
    out = os.path.abspath(a.out)
    state = load(out)
    g = find_group(state, a.group)
    rd = rdir(out, g["id"], a.round)
    pos = read_answer(os.path.join(rd, "positioned.json")) or {}
    if pos.get("state") != "DONE":
        print("skip: the reviewer did not finish (OCR does not filter a failed round)")
        return
    cands, _ = round_candidates(state, g, pos)
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
    user = substitute(prompt("review_filter_task_user"), {
        "path": g["key"], "comments": comments,
        "diff": read_text(os.path.join(gdir(out, g["id"]), "diffs.txt")) or ""})
    tools = json.dumps(FILTER_TOOLS, ensure_ascii=False, indent=1)
    harness = (f"<harness>\nThese are the two tools OCR gives you for this task, verbatim; you must call exactly "
               f"one:\n{tools}\nHere you cannot call tools. Use no tools other than Write: judge from this "
               f"prompt alone (the diffs above are all you may see). Write your call to {rd}/filter.json as "
               f'JSON with the keys in this order: {{"tool": "report_incorrect_comments", "analysis": [...], '
               f'"comment_ids": [...]}} or {{"tool": "approve_all_comments"}}. Final message: one line, the '
               f"tool you chose.\n</harness>")
    p = os.path.join(rd, "filter-prompt.md")
    write(p, f"<system>\n{prompt('review_filter_task_system')}\n</system>\n\n{user}\n\n{harness}\n")
    print(f"prompt: {p}\noutput: {rd}/filter.json\ncandidates: {len(cands)}\nmodel: {state['model']}")


def cmd_apply(a):
    out = os.path.abspath(a.out)
    state = load(out)
    g = find_group(state, a.group)
    if a.round in g["applied"]:
        print(f"group {g['id']} round {a.round} was already applied; nothing changed")
        return
    rd = rdir(out, g["id"], a.round)
    pos = read_answer(os.path.join(rd, "positioned.json"))
    if not isinstance(pos, dict):
        sys.exit(f"run position for group {g['id']} round {a.round} first")
    cands, outside = round_candidates(state, g, pos)
    g["applied"].append(a.round)
    g["files_reviewed"] = sorted(set(g["files_reviewed"]) | {p for p in pos.get("files_reviewed", [])
                                                             if isinstance(p, str)})
    g["extra"] += outside                       # in OCR's output, never filtered or counted
    if pos.get("state") != "DONE":              # executeGroupSubtask's error path
        g["extra"] += cands                     # already in OCR's collector, unfiltered
        g["done"] = True
        if a.round == 1:
            g["failed"], g["stopped"] = True, "reviewer failed in round 1: group failed"
        else:
            g["stopped"] = f"reviewer failed in round {a.round}: earlier findings kept"
        save(out, state)
        print(f"group {g['id']} round {a.round}: STOP ({g['stopped']})")
        return
    resp = read_answer(a.response or os.path.join(rd, "filter.json"))
    ids = []
    if isinstance(resp, dict) and resp.get("tool") == "report_incorrect_comments":
        ids = resp.get("comment_ids") or []
    elif isinstance(resp, list):
        ids = resp
    remove = set()                              # fail open: anything else removes nothing
    for cid in ids if isinstance(ids, list) else []:
        m = re.fullmatch(r"c-(\d+)", str(cid))
        if m and int(m.group(1)) < len(cands):
            remove.add(int(m.group(1)))
    kept = [c for i, c in enumerate(cands) if i not in remove]
    g["dropped"] += [{**c, "dropped_by": "review filter"} for i, c in enumerate(cands) if i in remove]
    g["confirmed"] += kept
    reason = None
    if not kept:
        reason = "added no new findings; stopping early"
    elif len(g["confirmed"]) >= CONFIRMED_CAP:
        reason = f"{CONFIRMED_CAP} confirmed findings"
    elif a.round >= state["rounds"]:
        reason = "last round"
    if reason:
        g["done"], g["stopped"] = True, reason
    save(out, state)
    print(f"group {g['id']} round {a.round}: kept {len(kept)}, removed {len(remove)}"
          + (f", {len(outside)} outside the group (kept, not counted)" if outside else "")
          + "; " + (f"STOP ({reason})" if reason else f"NEXT round {a.round + 1}"))


# ---------- report ----------

def cmd_report(a):
    out = os.path.abspath(a.out)
    state = load(out)
    allc, dropped = [], []
    for g in state["groups"] or []:
        for c in g["confirmed"] + g.get("extra", []):
            allc.append({**c, "group": g["id"]})
        dropped += [{**c, "group": g["id"]} for c in g["dropped"]]
    rank = {sv: i for i, sv in enumerate(SEVERITIES)}
    allc.sort(key=lambda c: rank.get(c["severity"], 4))     # presentation: most severe first
    verdicts = read_answer(a.verdicts) if a.verdicts else None
    for v in verdicts if isinstance(verdicts, list) else []:
        i = v.get("index") if isinstance(v, dict) else None
        if isinstance(i, int) and 0 <= i < len(allc):
            word = str(v.get("verdict") or "").upper()
            allc[i]["verdict"] = f"{word}: {v.get('reason', '')}".strip()
            new_sev = str(v.get("severity") or "").lower()
            if new_sev in SEVERITIES and new_sev != allc[i]["severity"]:
                allc[i]["ocr_severity"] = allc[i]["severity"]
                allc[i]["severity"] = new_sev
    rejected = [c for c in allc if str(c.get("verdict", "")).startswith("REJECTED")]
    allc = [c for c in allc if c not in rejected]
    allc.sort(key=lambda c: rank.get(c["severity"], 4))
    with uopen(os.path.join(out, "findings.json"), "w") as fh:
        json.dump({"comments": allc, "dropped": dropped, "rejected": rejected}, fh, indent=1)
    lines = [f"# OCR review of {state['repo']}",
             f"{state['mode']}: base {state['base'][:10]}, to {state['to'][:10]}; effort {state['effort']}; "
             f"model {state['model']}; {state.get('ocr_version')} with prompts from {state.get('prompts_pinned')}",
             ""]
    failed_n = sum(1 for g in state["groups"] or [] if g.get("failed"))
    if failed_n:
        lines += [f"**Review incomplete: {failed_n} of {len(state['groups'])} groups failed; their files were "
                  f"not reviewed, so the findings below do not cover them.**", ""]
    lines += ["## Coverage"]
    for g in state["groups"] or []:
        paths = [state["files"][i]["path"] for i in g["files"]]
        miss = [p for p in paths if p not in set(g.get("files_reviewed", []))]
        status = "FAILED" if g.get("failed") else f"stopped: {g['stopped']}"
        lines.append(f"- group {g['id']} ({status}): {', '.join(paths)}"
                     + (f" — the reviewer did not list: {', '.join(miss)}" if miss and not g.get("failed") else ""))
    for f in state["excluded"]:
        why = REASONS.get(f.get("exclude_reason"), f.get("exclude_reason"))
        lines.append(f"- excluded {f['path']}: {why}")
    lines += ["", f"## Findings ({len(allc)})"]
    for c in allc:
        where = (f"{c['path']}:{c['start_line']}-{c['end_line']}" if c["start_line"] > 0
                 else f"{c['path']}:0-0 (unplaced)")
        extra = [f"round {c['round']}"]
        if c.get("refiled_from"):
            extra.append(f"re-filed from {c['refiled_from']}")
        if c.get("ocr_severity"):
            extra.append(f"OCR said {c['ocr_severity']}")
        lines.append(f"- **{c['severity']} · {c['category']}** `{where}` ({', '.join(extra)})")
        lines.append(f"  {c['content']}")
        if c.get("suggestion_code"):
            lines.append("  ```diff")
            if c.get("existing_code"):
                lines += [f"  - {l}" for l in c["existing_code"].split("\n")]
            lines += [f"  + {l}" for l in c["suggestion_code"].split("\n")]
            lines.append("  ```")
        if c.get("verdict"):
            lines.append(f"  verdict: {c['verdict']}")
    lines += ["", f"## Removed by the review filter ({len(dropped)})"]
    lines += [f"- `{c['path']}:{c['start_line']}` {c['content'][:200]}" for c in dropped]
    if rejected:
        lines += ["", f"## Rejected by the extra check ({len(rejected)})"]
        lines += [f"- `{c['path']}:{c['start_line']}` {c['content'][:200]} ({c['verdict']})" for c in rejected]
    write(os.path.join(out, "report.md"), "\n".join(lines) + "\n")
    counts = {sv: sum(1 for c in allc if c["severity"] == sv) for sv in SEVERITIES}
    groups = state["groups"] or []
    failed = [g["id"] for g in groups if g.get("failed")]
    if failed:
        print(f"REVIEW INCOMPLETE: {len(failed)} of {len(groups)} groups failed ({', '.join(map(str, failed))}); "
              f"their files were not reviewed")
    print(f"{len(allc)} findings {counts}, {len(dropped)} removed by the filter"
          + (f", {len(rejected)} rejected by the check" if rejected else ""))
    print(f"report: {out}/report.md\nfindings: {out}/findings.json "
          f"(for --verdicts, 'index' is the 0-based position in findings.json's \"comments\")")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--out", required=True)
    pl.add_argument("--repo", default=".")
    pl.add_argument("--from", dest="from_ref")
    pl.add_argument("--to", dest="to_ref")
    pl.add_argument("--commit")
    pl.add_argument("--background-file")
    pl.add_argument("--background")
    pl.add_argument("--rule")
    pl.add_argument("--exclude")
    pl.add_argument("--effort", choices=list(ROUNDS), default="medium")
    pl.add_argument("--model", choices=["opus", "sonnet"], default="opus")
    pl.add_argument("--list-excluded", action="store_true")
    pl.add_argument("--language", default="English")
    for name in ("groups", "report"):
        s1 = sub.add_parser(name)
        s1.add_argument("--out", required=True)
        if name == "groups":
            s1.add_argument("--response")
        else:
            s1.add_argument("--verdicts")
    for name in ("render", "position", "filter", "apply"):
        s1 = sub.add_parser(name)
        s1.add_argument("--out", required=True)
        s1.add_argument("--group", type=int, required=True)
        s1.add_argument("--round", type=int, required=True)
        if name == "position":
            s1.add_argument("--relocated")
        if name == "apply":
            s1.add_argument("--response")
    a = p.parse_args()
    {"plan": cmd_plan, "groups": cmd_groups, "render": cmd_render, "position": cmd_position,
     "filter": cmd_filter, "apply": cmd_apply, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
