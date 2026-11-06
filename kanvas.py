#!/usr/bin/env python3
"""Factory: a task board shared by humans and AI agents.

The board is a single JSON file (kanvas.json). Agents change it only through
this CLI, which enforces the workflow rules. Humans plan and review in the
browser:

    python kanvas.py serve

Python 3.8+, standard library only. Run `python kanvas.py -h` for commands.
"""

import argparse
import errno
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

VERSION = "0.6.0"
FORMAT = 1
INSTALLED = __name__ == "kanvas_board"
BOARD_FILE = "kanvas.json"
HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(HERE, "kanvas.html")
SNAPSHOT_TAG = '<script id="kanvas-snapshot" type="application/json"></script>'

STATES = ("proposed", "todo", "doing", "review", "develop", "done", "production")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}$")
UNGROUPED_PREFIX = "G"
TASK_KEYS = ("id", "title", "state", "group", "deps", "yolo", "at", "by", "desc",
             "definition", "work", "review", "delivery", "notes")
REQUIRED_TASK_KEYS = ("id", "title", "state")
GROUP_KEYS = ("id", "name")
NOTE_KEYS = ("at", "by", "edited", "text")
REQUIRED_NOTE_KEYS = ("at", "by", "text")

# A lock older than STALE_LOCK_AGE was left by a crashed process. Writers wait longer than that, so they break it
# and go on; a lock is only held while the board is read and written.
LOCK_TIMEOUT = 10.0
STALE_LOCK_AGE = 5.0
MAX_UPLOAD = 20 * 1024 * 1024

# What the board shows as a task's evidence: only screenshots and videos, from the folder of that task.
EVIDENCE_TYPES = {
    ".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"), ".webp": ("image", "image/webp"),
    ".webm": ("video", "video/webm"), ".mp4": ("video", "video/mp4"),
}
EVIDENCE_MAX_FILES = 200
EVIDENCE_MAX_DEPTH = 3
EVIDENCE_MAX_BYTES = 200 * 1024 * 1024


class KanvasError(Exception):
    pass


# ---------------------------------------------------------------------------
# Board file: discovery, parsing, canonical serialization, safe writes
# ---------------------------------------------------------------------------


def find_board(start=None):
    """Return the nearest kanvas.json in `start` or any parent directory."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(d, BOARD_FILE)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def new_board(title=""):
    return {"kanvas": FORMAT, "title": title, "groups": [], "tasks": []}


def read_text_file(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8-sig")


def parse_board(text):
    try:
        board = json.loads(text)
    except json.JSONDecodeError as e:
        raise KanvasError(f"Board is not valid JSON: {e}")
    if not isinstance(board, dict):
        raise KanvasError("Board must be a JSON object")
    if isinstance(board.get("kanvas"), int) and board["kanvas"] > FORMAT:
        upgrade = "upgrade with `uv tool upgrade kanvas-board`" if INSTALLED else "upgrade kanvas.py"
        raise KanvasError(f"Board was written by a newer version of Factory; {upgrade}")
    # Defaults follow JavaScript's truthiness and String(), as normalize() in kanvas.html does.
    board["title"] = _js_text(board.get("title"))
    for key in ("groups", "tasks"):
        board.setdefault(key, [])
        if not isinstance(board[key], list) or not all(isinstance(x, dict) for x in board[key]):
            raise KanvasError(f"Board '{key}' must be a list of objects")
    for g in board["groups"]:
        if not isinstance(g.get("id"), str):
            raise KanvasError("Every group needs a string 'id'")
        g["name"] = _js_str(g["name"]) if _js_truthy(g.get("name")) else g["id"]
    for t in board["tasks"]:
        if not isinstance(t.get("id"), str):
            raise KanvasError("Every task needs a string 'id'")
        t["title"] = _js_text(t.get("title"))
        t["state"] = t["state"] if _js_truthy(t.get("state")) else "todo"
        t["group"] = t["group"] if _js_truthy(t.get("group")) else None
        t["desc"] = _js_text(t.get("desc"))
        deps = t.get("deps")
        t["deps"] = [_js_str(d) for d in deps] if isinstance(deps, list) else []
        if _js_truthy(t.get("yolo")):
            t["yolo"] = True
        else:
            t.pop("yolo", None)
        notes = t.get("notes")
        t["notes"] = [n for n in notes if isinstance(n, dict)] if isinstance(notes, list) else []
        for key in ("definition", "work", "review", "delivery"):
            if key in t and not isinstance(t[key], dict):
                t.pop(key, None)
    return board


def _js_truthy(v):
    if v is None or v is False or v == "":
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v == v and v != 0
    return True


def _js_str(v):
    """String(v) in JavaScript, for the values JSON can hold."""
    if isinstance(v, str):
        return v
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        text = _js_number(v)
        if text is None:
            return "NaN" if v != v else "Infinity" if v > 0 else "-Infinity"
        return text
    if isinstance(v, list):
        return ",".join("" if x is None else _js_str(x) for x in v)
    return "[object Object]"


def _js_text(v):
    """String(v || "") in JavaScript."""
    return _js_str(v) if _js_truthy(v) else ""


def _js_number(x):
    """A number as JavaScript prints it (shortest round trip), or None if it is not finite."""
    if isinstance(x, int) and abs(x) <= 2 ** 53:
        return str(x)
    try:
        x = float(x)
    except OverflowError:
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    mantissa, _, exp = repr(abs(x)).partition("e")
    whole, _, frac = mantissa.partition(".")
    digits = (whole + frac).lstrip("0")
    point = len(whole) + int(exp or 0) - (len(whole + frac) - len(digits))
    digits = digits.rstrip("0")
    if len(digits) <= point <= 21:
        return sign + digits + "0" * (point - len(digits))
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * -point + digits
    e = point - 1
    return f"{sign}{digits[0]}{'.' + digits[1:] if len(digits) > 1 else ''}e{'+' if e > 0 else '-'}{abs(e)}"


def load_board(path):
    if not os.path.isfile(path):
        raise KanvasError(f"Board not found: {path}")
    return parse_board(read_text_file(path))


def _ordered(obj, keys, keep=()):
    out = {}
    for k in keys:
        v = obj.get(k)
        if k in keep or v not in (None, "", []):
            out[k] = v if v is not None else ""
    for k, v in obj.items():
        if k not in keys:
            out[k] = v
    return out


def canonical(board):
    """Stable key order and no empty optional fields, so diffs stay small."""
    out = {"kanvas": FORMAT, "title": board.get("title") or ""}
    out["groups"] = [_ordered(g, GROUP_KEYS, GROUP_KEYS) for g in board.get("groups", [])]
    tasks = []
    for t in board.get("tasks", []):
        t = _ordered(t, TASK_KEYS, REQUIRED_TASK_KEYS)
        if "notes" in t:
            t["notes"] = [_ordered(n, NOTE_KEYS, REQUIRED_NOTE_KEYS) for n in t["notes"]]
        tasks.append(t)
    out["tasks"] = tasks
    for k, v in board.items():
        if k not in out:
            out[k] = v
    return out


# kanvas.html applies the same transformation, so files written from the
# browser and from the CLI are byte-identical.
_STR_ARRAY = re.compile(r'\[\n\s*("(?:[^"\\\n]|\\.)*"(?:,\n\s*"(?:[^"\\\n]|\\.)*")*)\n\s*\]')


_INDEX_KEY = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _json(v, indent=""):
    """JSON.stringify(v, null, 2): integer-like keys first, numbers and lone surrogates as JavaScript writes them."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _js_number(v) or "null"
    if isinstance(v, str):
        return _LONE_SURROGATE.sub(lambda m: "\\u%04x" % ord(m.group()), json.dumps(v, ensure_ascii=False))
    inner = indent + "  "
    if isinstance(v, list):
        items = [inner + _json(x, inner) for x in v]
    else:
        index = sorted((k for k in v if _INDEX_KEY.match(k) and int(k) < 2 ** 32 - 1), key=int)
        first = set(index)
        items = [f"{inner}{_json(k)}: {_json(v[k], inner)}" for k in index + [k for k in v if k not in first]]
    if not items:
        return "[]" if isinstance(v, list) else "{}"
    open_, close = "[]" if isinstance(v, list) else "{}"
    return f"{open_}\n" + ",\n".join(items) + f"\n{indent}{close}"


def dumps(board):
    text = _json(canonical(board))
    text = _STR_ARRAY.sub(lambda m: "[" + re.sub(r",\n\s*", ", ", m.group(1)) + "]", text)
    return text + "\n"


@contextmanager
def locked(path):
    """Cross-process lock so agents, the server and humans never lose writes."""
    lock = path + ".lock"
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError) as e:
            # Windows also raises PermissionError while another process deletes the lock, so retry until the deadline.
            try:
                if time.time() - os.path.getmtime(lock) > STALE_LOCK_AGE:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                if isinstance(e, PermissionError) and not os.path.exists(lock):
                    raise KanvasError(f"Cannot create the lock file {lock}: permission denied. "
                                      "Check that you can write to the board's folder.")
                raise KanvasError(
                    f"Board is busy (lock file {lock}). "
                    "Delete that file if no other kanvas process is running."
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def write_text_atomic(path, text):
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        for _ in range(40):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows refuses to replace a file another process is reading.
                time.sleep(0.05)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def mutate(path, fn):
    """Load, apply `fn(board)`, and save atomically under the lock.

    Nothing is written when `fn` raises, so failed commands leave the board
    untouched.
    """
    with locked(path):
        board = load_board(path)
        result = fn(board)
        write_text_atomic(path, dumps(board))
    return result


def etag_of(data):
    return '"' + hashlib.sha1(data).hexdigest()[:20] + '"'


# ---------------------------------------------------------------------------
# Board model
# ---------------------------------------------------------------------------


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def task_map(board):
    return {t["id"].upper(): t for t in board["tasks"]}


def find_task(board, ref):
    task = task_map(board).get(str(ref).strip().upper())
    if not task:
        raise KanvasError(f"Task '{ref}' not found")
    return task


def find_group(board, ref):
    key = str(ref).strip().lower()
    for g in board["groups"]:
        if g["id"].lower() == key or g["name"].lower() == key:
            return g
    known = ", ".join(f"{g['name']} [{g['id']}]" for g in board["groups"]) or "none yet"
    raise KanvasError(f"Group '{ref}' not found (groups: {known}). Create it with add-group.")


def group_label(board, gid):
    for g in board["groups"]:
        if g["id"] == gid:
            return f"{g['name']} [{g['id']}]"
    return "Ungrouped" if not gid else f"{gid} (unknown group)"


def is_complete(task):
    """A dependency is complete once merged to dev, whether or not it is in production."""
    return bool(task) and task.get("state") in ("develop", "done", "production")


def unmet_deps(task, tasks):
    """Dependency IDs that are not merged to dev yet. Missing tasks count as unmet."""
    out = []
    for dep in task.get("deps", []):
        d = tasks.get(dep.upper())
        if not is_complete(d):
            out.append(dep)
    return out


def yolo_active(task):
    """A YOLO mark counts until its task goes to review: the chain is finished then."""
    return bool(task.get("yolo")) and task.get("state") not in ("review", "develop", "done", "production")


def yolo_cover(board):
    """Map task ID (upper case) -> ID of the YOLO task covering it.

    Until the marked task goes to review, done or production, it selects itself
    and everything it transitively depends on for autonomous attention. It does
    not bypass definitions, review gates or delivery evidence.
    """
    tasks = task_map(board)
    active = [t for t in board["tasks"] if yolo_active(t)]
    reach = [(t, _upstream(tasks, t)) for t in active]
    inside = set().union(*(up for _, up in reach))
    cover = {}
    for t, up in reach:
        if t["id"].upper() in inside:
            continue
        cover.setdefault(t["id"].upper(), t["id"])
        for k in up:
            cover.setdefault(k, t["id"])
    return cover


def _upstream(tasks, task):
    """Upper-case IDs of everything `task` transitively depends on."""
    seen, stack = set(), [x.upper() for x in task.get("deps", [])]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        d = tasks.get(cur)
        if d:
            stack.extend(x.upper() for x in d.get("deps", []))
    return seen


def view_state(task, tasks):
    """Stored state, with 'blocked' derived for to-do tasks waiting on deps."""
    state = task.get("state", "todo")
    if state == "todo" and unmet_deps(task, tasks):
        return "blocked"
    return state


def depends_on(board, task_id, target_id):
    """True if `task_id` is or transitively depends on `target_id`."""
    tasks = task_map(board)
    target = target_id.upper()
    seen, stack = set(), [task_id.upper()]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        t = tasks.get(cur)
        if t:
            stack.extend(d.upper() for d in t.get("deps", []))
    return False


def next_id(board, prefix):
    prefix = prefix.upper()
    highest = 0
    for t in board["tasks"]:
        head, _, num = t["id"].rpartition("-")
        if head.upper() == prefix and num.isascii() and num.isdigit():
            highest = max(highest, int(num))
    return f"{prefix}-{highest + 1:02d}"


def suggest_prefix(name, taken):
    """Short, readable ID prefix for a group: Research -> RS, Report Writing -> RW."""
    taken = {p.upper() for p in taken}
    words = [w.upper() for w in re.findall(r"[A-Za-z0-9]+", name)]
    candidates = []
    if len(words) >= 2:
        candidates.append("".join(w[0] for w in words[:3]))
    if words:
        w = words[0]
        candidates += [w[0] + c for c in w[1:] if c not in "AEIOU"]
        candidates += [w[:2], w[:3]]
    for c in candidates:
        if PREFIX_RE.match(c) and c not in taken:
            return c
    letter = next((ch for w in words for ch in w if ch.isalpha()), UNGROUPED_PREFIX)
    n = 2
    while f"{letter}{n}" in taken:
        n += 1
    return f"{letter}{n}"


def add_group(board, name, prefix=None):
    """Create a group, or return the existing one with that name."""
    name = " ".join(str(name or "").split())
    if not name:
        raise KanvasError("Group name is empty")
    for g in board["groups"]:
        if g["name"].lower() == name.lower():
            return g, False
    taken = [g["id"] for g in board["groups"]]
    if prefix:
        prefix = prefix.strip().upper()
        if not PREFIX_RE.match(prefix):
            raise KanvasError(f"Invalid prefix '{prefix}': use 1-6 letters or digits, starting with a letter")
        if prefix in taken:
            raise KanvasError(f"Prefix '{prefix}' is already used by another group")
    else:
        prefix = suggest_prefix(name, taken)
    group = {"id": prefix, "name": name}
    board["groups"].append(group)
    return group, True


def add_task(board, title, group=None, desc="", deps=(), state="proposed", by=None):
    title = " ".join(str(title or "").split())
    if not title:
        raise KanvasError("Task title is empty")
    tasks = task_map(board)
    dep_ids = []
    for ref in deps:
        d = tasks.get(str(ref).strip().upper())
        if d is None:
            raise KanvasError(f"Dependency '{ref}' not found")
        if d["id"] not in dep_ids:
            dep_ids.append(d["id"])
    task = {
        "id": next_id(board, group["id"] if group else UNGROUPED_PREFIX),
        "title": title,
        "state": state,
        "group": group["id"] if group else None,
        "deps": dep_ids,
        "at": now_iso() if by else None,
        "by": by,
        "desc": (desc or "").strip(),
        "notes": [],
    }
    board["tasks"].append(task)
    return task


def add_note(task, text, by):
    text = (text or "").strip()
    if not text:
        raise KanvasError("Note text is empty")
    task.setdefault("notes", []).append({"at": now_iso(), "by": by, "text": text})


QUESTION = "Question:"


def open_question(task):
    """The note in which the agent working on `task` asked the human something in its chat, while it waits.

    The next note, from anyone, ends the wait, and so does the task leaving doing (mirrored in kanvas.html).
    """
    notes = task.get("notes") or []
    last = notes[-1] if task.get("state") == "doing" and notes else None
    if last and last.get("by") != "human" and str(last.get("text", "")).startswith(QUESTION):
        return last
    return None


def question_text(note):
    return str(note.get("text", ""))[len(QUESTION):].strip()


CHANGES = "Changes requested:"


def change_request(task):
    """The note in which the human asked for changes to a proposal, until an agent answers it.

    A note from an agent after it answers it, and so does the task leaving proposed (mirrored in kanvas.html).
    """
    if task.get("state") != "proposed":
        return None
    for note in reversed(task.get("notes") or []):
        if note.get("by") != "human":
            return None
        if str(note.get("text", "")).startswith(CHANGES):
            return note
    return None


def change_text(note):
    return str(note.get("text", ""))[len(CHANGES):].strip()


def find_cycles(board):
    """Each dependency cycle once, as a list of IDs (A depends on B depends on ...)."""
    tasks = task_map(board)
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(tasks, white)
    cycles, seen = [], set()

    def deps(key):
        return iter([d.upper() for d in tasks[key].get("deps", [])])

    for root in tasks:
        if color[root] != white:
            continue
        color[root] = gray
        path, stack = [root], [(root, deps(root))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = black
                stack.pop()
                path.pop()
            elif nxt not in tasks:
                continue
            elif color[nxt] == gray:
                cycle = path[path.index(nxt):]
                if len(cycle) > 1 and frozenset(cycle) not in seen:
                    seen.add(frozenset(cycle))
                    cycles.append([tasks[k]["id"] for k in cycle])
            elif color[nxt] == white:
                color[nxt] = gray
                path.append(nxt)
                stack.append((nxt, deps(nxt)))
    return cycles


def structural_errors(board):
    """Problems that make the board unusable for the CLI."""
    errors, seen = [], set()
    group_ids = set()
    for g in board["groups"]:
        if not PREFIX_RE.match(g["id"]):
            errors.append(f"Group '{g['name']}' has an invalid id '{g['id']}'")
        elif g["id"] in group_ids:
            errors.append(f"Duplicate group id {g['id']}")
        group_ids.add(g["id"])
    for t in board["tasks"]:
        tid = t["id"]
        if not ID_RE.match(tid):
            errors.append(f"Invalid task id '{tid}' (expected PREFIX-NN, e.g. DV-03)")
        elif tid.upper() in seen:
            errors.append(f"Duplicate task id {tid}")
        seen.add(tid.upper())
        if t.get("state") not in STATES:
            errors.append(f"{tid}: unknown state '{t.get('state')}' (use one of {', '.join(STATES)})")
    return errors


def problems(board):
    """Return (errors, warnings) describing everything wrong with the board."""
    errors = structural_errors(board)
    warnings = []
    tasks = task_map(board)
    group_ids = {g["id"] for g in board["groups"]}
    for t in board["tasks"]:
        tid = t["id"]
        if t.get("group") and t["group"] not in group_ids:
            warnings.append(f"{tid}: unknown group '{t['group']}'")
        for dep in t.get("deps", []):
            if dep.upper() == tid.upper():
                errors.append(f"{tid} depends on itself")
            elif dep.upper() not in tasks:
                errors.append(f"{tid} depends on missing task {dep}")
        if t.get("state") in ("doing", "review", "develop", "done", "production"):
            pending = [d for d in unmet_deps(t, tasks) if d.upper() in tasks]
            if pending:
                warnings.append(f"{tid} is {t['state']} but depends on unfinished {', '.join(pending)}")
    for cycle in find_cycles(board):
        errors.append("Circular dependency: " + " -> ".join(cycle + [cycle[0]]))
    return errors, warnings


def chain_depths(deps):
    """Longest dependency chain below each node of {node: [dep, ...]} (cycle-safe)."""
    depth = {}
    for root in deps:
        stack, on_path = [(root, False)], set()
        while stack:
            key, expanded = stack.pop()
            if expanded:
                on_path.discard(key)
                depth[key] = max([depth[d] + 1 for d in deps[key] if d in depth] + [0])
            elif key not in depth and key not in on_path:
                on_path.add(key)
                stack.append((key, True))
                stack.extend((d, False) for d in deps[key] if d in deps and d not in depth)
    return depth


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def board_path(args, must_exist=True):
    path = getattr(args, "board", None) or os.environ.get("KANVAS_BOARD") or find_board()
    if not path:
        raise KanvasError(
            f"No {BOARD_FILE} found here or in any parent directory. "
            f"Create one with `{command_line('init')}`, or pass --board PATH."
        )
    path = os.path.abspath(path)
    if must_exist and not os.path.isfile(path):
        raise KanvasError(f"Board not found: {path}")
    return path


def actor(args):
    return getattr(args, "as_name", None) or os.environ.get("KANVAS_AGENT") or "agent"


def git_snapshot(path, required=False):
    """Describe the Git worktree containing path, without changing it."""
    path = os.path.abspath(path)

    def git(*parts):
        proc = subprocess.run(
            ["git", "-C", path, *parts], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    code, root, error = git("rev-parse", "--show-toplevel")
    if code:
        if required:
            raise KanvasError(f"Not a Git worktree: {path}" + (f" ({error})" if error else ""))
        return None
    code, commit, error = git("rev-parse", "HEAD")
    if code:
        raise KanvasError(f"Cannot read the Git commit in {path}: {error or 'unknown Git error'}")
    _, branch, _ = git("branch", "--show-current")
    _, common, _ = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    repo = os.path.dirname(common) if os.path.basename(common).lower() == ".git" else os.path.abspath(root)
    code, changes, error = git("status", "--porcelain")
    if code:
        raise KanvasError(f"Cannot read Git status in {path}: {error or 'unknown Git error'}")
    return {
        "repo": repo,
        "worktree": os.path.abspath(root),
        "branch": branch or "(detached)",
        "commit": commit,
        "dirty": bool(changes),
    }


def worktree_snapshots(paths):
    explicit = bool(paths)
    snapshots, seen = [], set()
    for path in paths or [os.getcwd()]:
        snap = git_snapshot(path, required=explicit)
        if snap and snap["worktree"].lower() not in seen:
            seen.add(snap["worktree"].lower())
            snapshots.append(snap)
    return snapshots


def automation_config(board, path):
    """Validated project automation config, or None when this board is passive."""
    config = board.get("automation")
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return None
    if config.get("executor") not in (None, "claude"):
        raise KanvasError("automation.executor is no longer needed: Factory runs tasks with Claude Code")
    root = os.path.abspath(os.path.join(os.path.dirname(path), config.get("project_root") or "."))
    repositories = {}
    for item in config.get("repositories") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("path"):
            raise KanvasError("Each automated repository needs id and path")
        repo = os.path.abspath(os.path.join(root, item["path"]))
        if not os.path.isdir(repo):
            raise KanvasError(f"Configured repository does not exist: {repo}")
        repositories[str(item["id"])] = {
            "id": str(item["id"]),
            "name": str(item.get("name") or item["id"]),
            "path": repo,
            "base": str(item.get("base") or "origin/dev"),
        }
    if not repositories:
        raise KanvasError("Automation needs at least one configured repository")
    worktrees = os.path.abspath(os.path.join(root, config.get("worktrees_root") or ".kanvas/worktrees"))
    runs = os.path.abspath(os.path.join(root, config.get("runs_root") or ".kanvas/runs"))
    return {
        "root": root,
        "board": path,
        "repositories": repositories,
        "worktrees": worktrees,
        "runs": runs,
        "branch_pattern": str(config.get("branch_pattern") or "kanvas/{id_slug}-{title_slug}"),
        "model": config.get("model"),
        "reasoning": config.get("reasoning"),
        "lean": _flag(config.get("lean"), True, "automation.lean"),
        "agents": agent_roles(config.get("agents")),
        "assist": assist_settings(config.get("assist")),
        "review": review_settings(config.get("review")),
        "curator": curator_settings(config.get("curator")),
        "design": design_settings(config.get("design")),
        "ci": ci_settings(config.get("ci")),
        "max_rounds": _positive_int(config.get("max_rounds"), 3, "automation.max_rounds"),
        "timeout_minutes": _positive_int(config.get("timeout_minutes"), None, "automation.timeout_minutes"),
        "idle_minutes": _positive_int(config.get("idle_minutes"), None, "automation.idle_minutes"),
        "max_turns": config.get("max_turns"),
        "max_budget_usd": config.get("max_budget_usd"),
        "allowed_tools": _tool_list(config.get("allowed_tools"), CLAUDE_ALLOWED_TOOLS),
        "disallowed_tools": _tool_list(config.get("disallowed_tools"), CLAUDE_DISALLOWED_TOOLS),
    }


def task_repository_ids(task):
    """Repositories selected in the product definition and copied into the construction plan."""
    work = task.get("work") if isinstance(task.get("work"), dict) else {}
    definition = task.get("definition") if isinstance(task.get("definition"), dict) else {}
    values = work.get("repositories") or definition.get("repositories") or task.get("repositories") or []
    return [str(x) for x in values if str(x).strip()]


# Only Factory pushes and opens pull requests, so the agent must not inherit any GitHub credential.
GITHUB_TOKEN_VARS = ("KANVAS_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
# Branches Factory never pushes to, besides the branch each pull request goes into.
PROTECTED_BRANCHES = ("dev", "main", "master")


# Other credentials of Factory's own that an agent must not inherit: the token of the Telegram bot.
SECRET_VARS = ("KANVAS_TELEGRAM_TOKEN",)


def agent_environment():
    """The environment the agent runs in: Factory's own without GitHub tokens, the bot's token or the SSH agent."""
    return {k: v for k, v in os.environ.items()
            if k.upper() not in GITHUB_TOKEN_VARS and k.upper() not in SECRET_VARS and k.upper() != "SSH_AUTH_SOCK"}


def publisher_environment():
    """The environment of Factory's own `git push` and `gh` calls, and whether it holds a GitHub token of its own.
    With KANVAS_GITHUB_TOKEN set, that token is the only GitHub credential they use; without it they use the computer's."""
    env = dict(os.environ)
    token = env.get("KANVAS_GITHUB_TOKEN")
    if token:
        for name in [n for n in env if n.upper() in GITHUB_TOKEN_VARS]:
            del env[name]
        env["GH_TOKEN"] = token
    env["GH_PROMPT_DISABLED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env, bool(token)


def check_task_branch(branch, repo):
    """Factory only pushes branches it made for a task: never a protected one, nor the one a pull request goes into."""
    name = branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch
    refused = {b.lower() for b in PROTECTED_BRANCHES} | {pr_base_branch(repo).lower()}
    if name.lower() in refused:
        raise KanvasError(f"Refusing to use {name} as a task branch: Factory never pushes to {', '.join(PROTECTED_BRANCHES)} "
                          "or to the branch a pull request goes into")


def _git(path, *args, env=None):
    proc = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    if proc.returncode:
        raise KanvasError(proc.stderr.strip() or proc.stdout.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def push_branch(folder, branch):
    """Pushes a task branch with Factory's own credentials. With a token of its own the push goes over HTTPS, through
    the GitHub CLI's credential helper, and uses only that token."""
    env, token = publisher_environment()
    options = []
    if token:
        options = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                   "-c", "url.https://github.com/.insteadOf=git@github.com:",
                   "-c", "url.https://github.com/.insteadOf=ssh://git@github.com/"]
    _git(folder, *options, "push", "--set-upstream", "origin", branch, env=env)


def automation_slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug[:36] or "task"


def automation_branch(task, config):
    """Render the configured branch name for one automated task."""
    values = {
        "id": str(task["id"]),
        "id_lower": str(task["id"]).lower(),
        "id_slug": automation_slug(task["id"]),
        "title_slug": automation_slug(task["title"]),
    }
    try:
        branch = config["branch_pattern"].format(**values).strip().strip("/")
    except (KeyError, ValueError) as e:
        raise KanvasError(f"Invalid automation.branch_pattern: {e}")
    if not branch or branch.startswith("-") or branch.endswith(".") or any(x in branch for x in ("..", "@{", "\\", " ")):
        raise KanvasError(f"Invalid automated branch name: {branch or '(empty)'}")
    return branch


def automation_prompt(board, task, config, worktrees, round_no=1, feedback=""):
    repos = "\n".join(f"- {x['id']} ({x['name']}): {x['worktree']} on {x['branch']}" for x in worktrees)
    definition = task.get("definition") or {}
    work = task.get("work") or {}
    approved = work.get("design") if isinstance(work.get("design"), dict) else {}
    design = (f"\nApproved UI/UX design (the human approved it: build exactly this and do not invent other UI; if it cannot "
              f"be built as written, say so in your summary):\n{approved['text']}\n"
              + (f"\nThe approved mockup (the exact look the human accepted, to be rebuilt with the application's own "
                 f"components and styles, not pasted):\n```html\n{approved['mockup']}\n```\n" if approved.get("mockup") else "")
              if approved.get("status") == DESIGN_APPROVED and approved.get("text") else "")
    if design:
        design += (f"\nWhen the UI is built, look at the real result: run `kanvas --board \"{config['board']}\" shots <the html file, or the URL "
                   f"of the app you started> --task {task['id']}`. It saves screenshots at phone, iPad and desktop size as this task's "
                   f"evidence, where the human compares them with the design. Open them with Read, compare them with the mockup and fix "
                   f"what differs before you finish. Stop any server you started.\n")
    followup = ""
    if round_no > 1:
        followup = (f"\nThis is round {round_no}: your earlier work was reviewed and sent back. The branches already hold "
                    f"your commits. Address every point below, then commit.\n\nFeedback to address:\n"
                    f"{feedback or '(no written feedback: check the earlier work against the acceptance criteria)'}\n")
    return f"""You are the implementation agent for Factory task {task['id']}: {task['title']}.

Project root: {config['root']}
Repositories and isolated worktrees:
{repos}

Product outcome:
{definition.get('outcome') or task.get('desc') or '(not supplied)'}

Needs and constraints:
{definition.get('needs') or task.get('desc') or '(not supplied)'}

Acceptance criteria:
{definition.get('acceptance') or '(not supplied)'}

Non-goals:
{definition.get('non_goals') or '(none)'}

Technical plan:
{work.get('plan') or '(not supplied)'}
{design}{followup}
Read the root AGENTS.md (or CLAUDE.md), then each repository's AGENTS.md or CLAUDE.md and the documentation it points to.
Work only in the isolated worktrees listed above; never modify the original checkouts. The branches may already
hold commits from an earlier attempt: continue from them instead of starting over. Implement the complete task
across every selected repository, run proportionate tests and commit each repository with a conventional commit,
leaving no uncommitted changes to tracked files. Do not push, open pull requests, merge or deploy: Factory pushes
each branch and opens the pull request itself when you finish. End with a concise summary of changes, tests and
commits. Do not edit kanvas.json; Factory records your result itself.
"""


def prepare_automation_worktrees(task, config):
    selected = task_repository_ids(task)
    if not selected:
        raise KanvasError(f"{task['id']} has no repositories selected in its product definition")
    unknown = [x for x in selected if x not in config["repositories"]]
    if unknown:
        raise KanvasError(f"{task['id']} names unknown repositories: {', '.join(unknown)}")
    task_folder = os.path.join(config["worktrees"], automation_slug(task["id"]))
    os.makedirs(task_folder, exist_ok=True)
    out = []
    for repo_id in selected:
        repo = config["repositories"][repo_id]
        branch = automation_branch(task, config)
        check_task_branch(branch, repo)
        target = os.path.join(task_folder, repo_id)
        if os.path.exists(target):
            current = _git(target, "branch", "--show-current")
            if os.path.normcase(current) != os.path.normcase(branch):
                raise KanvasError(f"Automation worktree already exists on {current}, expected {branch}: {target}")
            out.append({**repo, "branch": branch, "worktree": target,
                        "commit": _git(target, "rev-parse", "HEAD")})
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _git(repo["path"], "worktree", "add", "-b", branch, target, repo["base"])
        out.append({**repo, "branch": branch, "worktree": target, "commit": _git(target, "rev-parse", "HEAD")})
    return out


def update_automation(path, task_id, status, **fields):
    def op(board):
        task = find_task(board, task_id)
        work = task.setdefault("work", {})
        run = work.setdefault("automation", {})
        run.update({"status": status, "updated": now_iso(), **fields})
        return task
    return mutate(path, op)


def resume_run(task, why):
    """Puts a cut-off run back to be run again from its worktree, once per round: a second cut-off in the same round fails
    and waits for the human. Returns whether it resumed."""
    work = task.get("work") if isinstance(task.get("work"), dict) else {}
    run = work.get("automation") if isinstance(work.get("automation"), dict) else {}
    round_no = int(run.get("round") or 1)
    if run.get("resumed") == round_no:
        return False
    kept = {k: run[k] for k in ("round", "since", "pull_requests", "cycle_start") if k in run}
    work["automation"] = {**kept, "resumed": round_no}
    task["work"] = work
    add_note(task, f"Factory continues the run by itself: {why}. What it had done is kept in the worktree.", "kanvas-runner")
    return True


def finish_automation(path, task_id, success, summary, pull_requests=None, resume=""):
    def op(board):
        task = find_task(board, task_id)
        work = task.setdefault("work", {})
        run = work.setdefault("automation", {})
        if not success and resume and task.get("state") == "doing" and resume_run(task, resume):
            return task
        run.update({"status": "completed" if success else "failed", "updated": now_iso(), "summary": summary})
        if pull_requests:
            run["pull_requests"] = pull_requests
        add_note(task, ("Automation completed: " if success else "Automation failed: ") + summary, "kanvas-runner")
        if success and task.get("state") == "doing":
            task["state"] = "review"
            task["review"] = {"agent": {"status": "pending"}, "manual": {"status": "pending"}}
        return task
    return mutate(path, op)


CLAUDE_ALLOWED_TOOLS = ("Bash", "Edit", "Write", "Read", "Glob", "Grep")
# Factory pushes the branch and opens the pull request itself, so the agent needs no GitHub access. The `git -*`
# patterns catch pushes and merges that put an option first (`git -C . push`, `git --no-pager push`), which the
# plain prefixes miss, without refusing a commit message that happens to say push.
CLAUDE_DISALLOWED_TOOLS = ("Bash(git push:*)", "Bash(git -* push*)", "Bash(git merge:*)", "Bash(git -* merge*)",
                           "Bash(gh:*)")


def _positive_int(value, default, name):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise KanvasError(f"{name} must be a whole number of at least 1")
    return value


def _tool_list(value, default):
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
        raise KanvasError("automation allowed_tools / disallowed_tools must be lists of strings")
    return list(value)


def _flag(value, default, name):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise KanvasError(f"{name} must be true or false")
    return value


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_AGENT = "implementer"
# The agent roles Factory knows without configuration, so that each kind of work runs on the cheapest model that does it
# well. A role may set model, effort, max_turns, max_budget_usd, allowed_tools and disallowed_tools, which win over the
# same keys of `automation`. A task picks its role with `definition.agent`; without one it runs as the implementer.
# kanvas.html repeats these roles to fill the Agent choice of the product definition: change both together.
DEFAULT_AGENTS = {
    "implementer": {"model": "sonnet", "effort": "medium", "timeout_minutes": 40, "max_budget_usd": 2},
    "complex": {"model": "opus", "effort": "high", "timeout_minutes": 60, "max_budget_usd": 5},
    # The reviewers read the diff in their prompt and open other files only when it is not enough, so they need no shell.
    # `agent` runs them as the project's agent of that name. The strong one looks again at what the first could not decide.
    "reviewer": {"agent": "reviewer", "model": "haiku", "effort": "low", "max_turns": 12, "timeout_minutes": 10,
                 "max_budget_usd": 0.3, "allowed_tools": ["Read", "Glob", "Grep"]},
    "reviewer-strong": {"agent": "reviewer", "model": "sonnet", "effort": "medium", "max_turns": 16, "timeout_minutes": 15,
                        "max_budget_usd": 0.6, "allowed_tools": ["Read", "Glob", "Grep"]},
    # Proposes the UI/UX of a front task before anyone implements it; it reads the app and the design rules, nothing else.
    "designer": {"agent": "designer", "model": "sonnet", "effort": "medium", "max_turns": 15, "timeout_minutes": 15,
                 "max_budget_usd": 1, "allowed_tools": ["Read", "Glob", "Grep"]},
    # Turns an idea or an incident the owner sends from their phone into a task with its definition. Reads, never writes.
    "po": {"agent": "product-owner", "model": "sonnet", "effort": "low", "max_turns": 12, "timeout_minutes": 10,
           "max_budget_usd": 0.5, "allowed_tools": ["Read", "Glob", "Grep"]},
    # Keeps the documentation repository in step with an accepted task; it only edits documentation, so it needs no shell.
    "curator": {"agent": "brain-curator", "model": "haiku", "effort": "low", "max_turns": 15, "timeout_minutes": 15,
                "max_budget_usd": 0.5, "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep"]},
}


def agent_roles(value):
    """The agent roles of a board: the built-in ones, with the changes and additions of `automation.agents`."""
    roles = {name: dict(role) for name, role in DEFAULT_AGENTS.items()}
    if value is None:
        return roles
    if not isinstance(value, dict):
        raise KanvasError("automation.agents must be an object with one entry per role")
    for name, given in value.items():
        if not isinstance(given, dict):
            raise KanvasError(f"automation.agents.{name} must be an object")
        role = {**roles.get(name, {}), **given}
        if role.get("model") is not None and not isinstance(role["model"], str):
            raise KanvasError(f"automation.agents.{name}.model must be a model name")
        if role.get("agent") is not None and not isinstance(role["agent"], str):
            raise KanvasError(f"automation.agents.{name}.agent must be the name of a Claude Code agent")
        if role.get("effort") is not None and role["effort"] not in EFFORT_LEVELS:
            raise KanvasError(f"automation.agents.{name}.effort must be one of {', '.join(EFFORT_LEVELS)}")
        if role.get("max_turns") is not None:
            role["max_turns"] = _positive_int(role["max_turns"], None, f"automation.agents.{name}.max_turns")
        for key in ("timeout_minutes", "idle_minutes"):
            if role.get(key) is not None:
                role[key] = _positive_int(role[key], None, f"automation.agents.{name}.{key}")
        for key in ("allowed_tools", "disallowed_tools"):
            if key in given:
                role[key] = _tool_list(given[key], ())
        roles[name] = role
    return roles


def review_settings(value):
    """`automation.review`: whether an agent reviews every task that reaches Review, and how many times in a row it may
    send the work back to the implementer on its own before it leaves the findings to the human."""
    if value is None:
        return {"enabled": True, "rounds": 2}
    if isinstance(value, bool):
        return {"enabled": value, "rounds": 2}
    if not isinstance(value, dict):
        raise KanvasError("automation.review must be true, false or an object")
    rounds = value.get("rounds", 2)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
        raise KanvasError("automation.review.rounds must be a whole number, 0 or more")
    return {"enabled": _flag(value.get("enabled"), True, "automation.review.enabled"), "rounds": rounds}


CURATOR_REPOSITORY = "brain"


def curator_settings(value):
    """`automation.curator`: whether accepting an automated task creates a documentation task for the `brain`
    repository. It only happens when the board has a repository with that id."""
    if value is None:
        return {"enabled": True}
    if isinstance(value, bool):
        return {"enabled": value}
    if not isinstance(value, dict):
        raise KanvasError("automation.curator must be true, false or an object")
    return {"enabled": _flag(value.get("enabled"), True, "automation.curator.enabled")}


def design_settings(value):
    """`automation.design`: whether a task that changes one of these repositories gets a UI/UX design proposal that the
    human approves before the implementer starts."""
    if value is None:
        return {"enabled": True, "repositories": ["front"]}
    if isinstance(value, bool):
        return {"enabled": value, "repositories": ["front"]}
    if not isinstance(value, dict):
        raise KanvasError("automation.design must be true, false or an object")
    repositories = value.get("repositories", ["front"])
    if not isinstance(repositories, list) or not all(isinstance(x, str) for x in repositories):
        raise KanvasError("automation.design.repositories must be a list of repository ids")
    return {"enabled": _flag(value.get("enabled"), True, "automation.design.enabled"), "repositories": repositories}


def ci_settings(value):
    """`automation.ci`: before a task goes to Review Factory waits for the checks of its pull requests and, when one fails,
    has the implementer fix it, at most `max_fixes` times; it waits at most `timeout_minutes` for the checks to finish."""
    default = {"enabled": True, "max_fixes": 2, "timeout_minutes": 15}
    if value is None:
        return default
    if isinstance(value, bool):
        return {**default, "enabled": value}
    if not isinstance(value, dict):
        raise KanvasError("automation.ci must be true, false or an object")
    return {"enabled": _flag(value.get("enabled"), True, "automation.ci.enabled"),
            "max_fixes": _positive_int(value.get("max_fixes"), 2, "automation.ci.max_fixes"),
            "timeout_minutes": _positive_int(value.get("timeout_minutes"), 15, "automation.ci.timeout_minutes")}


def task_agent_name(task):
    """The role that implements a task: the one its product definition names, or the implementer."""
    work = task.get("work") if isinstance(task.get("work"), dict) else {}
    definition = task.get("definition") if isinstance(task.get("definition"), dict) else {}
    return str(work.get("agent") or definition.get("agent") or DEFAULT_AGENT)


def claude_command(config, worktrees, role=None):
    """Claude Code in headless mode: the prompt arrives on stdin and the run is logged as stream-json. `role` holds the
    model, effort, limits and tools of the agent role it runs as, which win over the same keys of `automation`.

    Unless `automation.lean` is false, the agent starts with only what it needs: no MCP servers, only the project's
    settings and only its own tools. Every turn of a run re-reads that start, so a smaller one makes each turn cheaper."""
    role = role or {}

    def pick(key):
        return role[key] if role.get(key) is not None else config.get(key)

    command = [shutil.which("claude") or "claude", "-p", "--output-format", "stream-json", "--verbose",
               "--permission-mode", "acceptEdits"]
    for extra in worktrees[1:]:
        command += ["--add-dir", extra["worktree"]]
    if role.get("agent"):
        command += ["--agent", str(role["agent"])]
    if pick("model"):
        command += ["--model", str(pick("model"))]
    if pick("effort"):
        command += ["--effort", str(pick("effort"))]
    if pick("max_turns"):
        command += ["--max-turns", str(pick("max_turns"))]
    if pick("max_budget_usd"):
        command += ["--max-budget-usd", str(pick("max_budget_usd"))]
    allowed = role["allowed_tools"] if "allowed_tools" in role else config["allowed_tools"]
    disallowed = role["disallowed_tools"] if "disallowed_tools" in role else config["disallowed_tools"]
    if config.get("lean", True):
        command += ["--strict-mcp-config", "--setting-sources", "project,local"]
        tools = list(dict.fromkeys(t.split("(")[0] for t in allowed))
        if tools:
            command += ["--tools", ",".join(tools)]
    if allowed:
        command += ["--allowedTools", *allowed]
    if disallowed:
        command += ["--disallowedTools", *disallowed]
    return command


# ---------------------------------------------------------------------------
# An agent that is stuck must not go on spending tokens. Every run of Claude Code is watched: it is stopped when the
# human asks (`kanvas stop`, the Stop button), when it has run longer than its role allows, or when its log has not grown
# for a while (a command that never ends, a question nobody can answer). Its role's `max_budget_usd` caps what it spends.

DEFAULT_TIMEOUT_MINUTES = 40
DEFAULT_IDLE_MINUTES = 10


def agent_limits(config, role):
    role = role or {}
    minutes = role.get("timeout_minutes") or config.get("timeout_minutes") or DEFAULT_TIMEOUT_MINUTES
    idle = role.get("idle_minutes") or config.get("idle_minutes") or DEFAULT_IDLE_MINUTES
    return {"timeout": minutes * 60, "idle": idle * 60}


def kill_tree(proc):
    """Ends the agent and everything it started, such as a dev server that would otherwise keep running."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def agent_run(command, input="", stdout=None, cwd=None, env=None, creationflags=0, limits=None, stop=None, **_):
    """Runs Claude Code with `input` on its stdin and its output in `stdout`, and watches it. The result has the exit
    code and `stopped`, the reason Factory ended it, or None when it ended by itself."""
    limits = limits or {}
    options = {"stdin": subprocess.PIPE, "stdout": stdout, "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8",
               "errors": "replace", "cwd": cwd, "env": env}
    if os.name == "nt":
        options["creationflags"] = creationflags
    else:
        options["start_new_session"] = True
    proc = subprocess.Popen(command, **options)

    def feed():
        try:
            proc.stdin.write(input)
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    threading.Thread(target=feed, daemon=True).start()
    started = changed = time.monotonic()
    size, reason = -1, None
    while True:
        try:
            proc.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        try:
            current = os.path.getsize(stdout.name) if stdout else 0
        except OSError:
            current = size
        if current != size:
            size, changed = current, now
        if stop and stop():
            reason = "stopped"
        elif limits.get("timeout") and now - started > limits["timeout"]:
            reason = f"it ran for more than {limits['timeout'] // 60} minutes"
        elif limits.get("idle") and now - changed > limits["idle"]:
            reason = f"it wrote nothing for more than {limits['idle'] // 60} minutes"
        if reason:
            kill_tree(proc)
            proc.wait()
            break
    return SimpleNamespace(returncode=proc.returncode, stopped=reason)


# ---------------------------------------------------------------------------
# Seeing the result: `kanvas shots` opens a page in a headless browser at the size of a phone, an iPad and a desktop browser
# and keeps the screenshots as the task's evidence, so that the human can put what was built next to the design they approved.

SHOT_DEVICES = (("mobile", 390, 844), ("ipad", 820, 1180), ("web", 1280, 800))
BROWSER_NAMES = ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser", "msedge", "microsoft-edge")


def find_browser():
    """Chrome, Chromium or Edge: the one named by KANVAS_BROWSER, or the first found on PATH or in the usual folders."""
    wanted = os.environ.get("KANVAS_BROWSER")
    if wanted:
        return wanted if os.path.isfile(wanted) else shutil.which(wanted)
    found = next(filter(None, map(shutil.which, BROWSER_NAMES)), None)
    if found:
        return found
    roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    candidates = [os.path.join(r, *rest) for r in roots if r for rest in (
        ("Google", "Chrome", "Application", "chrome.exe"), ("Microsoft", "Edge", "Application", "msedge.exe"))]
    candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    return next(filter(os.path.isfile, candidates), None)


def take_shots(browser, target, folder, wait=4):
    """Saves design-mobile.png, design-ipad.png and design-web.png of `target` (a URL or an HTML file) in `folder`."""
    url = target if re.match(r"(https?|file)://", target) else "file:///" + os.path.abspath(target).replace(os.sep, "/").lstrip("/")
    if not re.match(r"(https?|file)://", target) and not os.path.isfile(target):
        raise KanvasError(f"{target} is not a file or a URL")
    os.makedirs(folder, exist_ok=True)
    saved = []
    for name, width, height in SHOT_DEVICES:
        out = os.path.join(folder, f"design-{name}.png")
        if os.path.exists(out):
            os.remove(out)
        with tempfile.TemporaryDirectory(prefix="kanvas-shot-") as profile:
            command = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-sandbox",
                       "--disable-extensions", f"--user-data-dir={profile}", f"--window-size={width},{height}",
                       f"--virtual-time-budget={int(wait * 1000)}", f"--screenshot={out}", url]
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=90, encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                raise KanvasError(f"The browser did not finish the {name} screenshot of {target} in 90 seconds")
        if not os.path.isfile(out) or not os.path.getsize(out):
            raise KanvasError(f"The browser took no {name} screenshot of {target}: {(proc.stderr or proc.stdout).strip()[-300:]}")
        saved.append(out)
    return saved


def cmd_shots(args):
    path = board_path(args)
    task = find_task(load_board(path), args.task or os.environ.get("KANVAS_TASK") or "")
    folder = args.out or os.path.join(evidence_folder(evidence_root(path), task["id"]) or "", "final")
    if not args.out and not evidence_folder(evidence_root(path), task["id"]):
        raise KanvasError("This board has no evidence folder for that task")
    browser = find_browser()
    if not browser:
        raise KanvasError("No browser to take the screenshots: install Chrome, Chromium or Edge, or set KANVAS_BROWSER to it")
    for shot in take_shots(browser, args.target, folder, args.wait):
        print(shot)


def stop_text(proc):
    """Why Factory ended a run, in a sentence, or "" when the run ended by itself."""
    reason = getattr(proc, "stopped", None)
    if not isinstance(reason, str):
        return ""
    return "Stopped by you." if reason == "stopped" else f"Stopped by Factory because {reason}."


def request_stop(path, task_id):
    """Asks the agent working on a task to stop. The runner that owns the process sees the request and ends it."""
    def op(board):
        t = find_task(board, task_id)
        run = ((t.get("work") or {}).get("automation")) or {}
        if run.get("status") not in ("queued", "running") and not (t.get("work") or {}).get("busy"):
            raise KanvasError(f"No agent is working on {t['id']}")
        t["work"]["stop"] = {"at": now_iso()}
        return t
    return mutate(path, op)


def stop_requested(path, task_id):
    try:
        return bool((find_task(load_board(path), task_id).get("work") or {}).get("stop"))
    except (KanvasError, OSError):
        return False


def clear_stop(path, task_id):
    def op(board):
        work = find_task(board, task_id).get("work")
        if isinstance(work, dict):
            work.pop("stop", None)
    try:
        if stop_requested(path, task_id):
            mutate(path, op)
    except (KanvasError, OSError):
        pass


def read_result(log):
    """The final `result` event of a stream-json log, or None when the run left none."""
    result = None
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    result = event
    except OSError:
        pass
    return result


def claude_outcome(proc, log):
    """The final `result` event of the stream-json log holds Claude's closing summary and whether it failed."""
    result = read_result(log)
    if result is None:
        return False, ""
    summary = str(result.get("result") or "").strip()
    if not summary and result.get("subtype"):
        summary = f"Claude stopped: {result['subtype']}"
    if result.get("total_cost_usd") is not None:
        summary += f"\n\n(Claude: {result.get('num_turns', '?')} turns, ${float(result['total_cost_usd']):.2f})"
    return proc.returncode == 0 and not result.get("is_error"), summary.strip()


def pull_request_feedback(pulls, since, root):
    """Comments and reviews left on the task's pull requests after `since`, as lines of text. Never raises."""
    lines = []

    def add(who, when, where, body):
        body = (body or "").strip()
        if body and str(when or "") > since:
            lines.append(f"- {who} on the pull request{where} ({when}): {body[:2000]}")

    for pull in pulls or []:
        url = str(pull.get("url") or "")
        m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
        if not m:
            continue
        try:
            data = json.loads(_gh(root, "pr", "view", url, "--json", "comments,reviews") or "{}")
            for c in data.get("comments") or []:
                add((c.get("author") or {}).get("login", "?"), c.get("createdAt"), "", c.get("body"))
            for r in data.get("reviews") or []:
                add((r.get("author") or {}).get("login", "?"), r.get("submittedAt"),
                    f" ({str(r.get('state') or '').lower().replace('_', ' ')})", r.get("body"))
            inline = json.loads(_gh(root, "api", f"repos/{m.group(1)}/{m.group(2)}/pulls/{m.group(3)}/comments") or "[]")
            for c in inline:
                where = f" at {c.get('path')}:{c.get('line') or c.get('original_line') or '?'}"
                add((c.get("user") or {}).get("login", "?"), c.get("created_at"), where, c.get("body"))
        except (KanvasError, ValueError, AttributeError) as e:
            lines.append(f"- (could not read the comments on {url}: {e})")
    return lines[:30]


def automation_feedback(task, run, root):
    """What the human and the reviewers said since the last round: board notes, then pull request comments."""
    since = str(run.get("since") or "")
    lines = [f"- {n.get('by', '?')} ({n.get('at')}): {n.get('text')}" for n in task.get("notes") or []
             if n.get("by") != "kanvas-runner" and str(n.get("at") or "") > since]
    return "\n".join(lines + pull_request_feedback(run.get("pull_requests"), since, root))


def pr_base_branch(repo):
    base = repo["base"]
    return base.split("/", 1)[1] if base.startswith("origin/") else base


def _gh(path, *args):
    exe = shutil.which("gh")
    if not exe:
        raise KanvasError("The GitHub CLI (gh) is not installed or not on PATH; Factory needs it to open pull requests")
    proc = subprocess.run([exe, *args], cwd=path, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=publisher_environment()[0])
    if proc.returncode:
        raise KanvasError("gh " + " ".join(args[:2]) + ": " + (proc.stderr.strip() or proc.stdout.strip() or "failed"))
    return proc.stdout.strip()


CI_POLL_SECONDS = 20
CI_APPEAR_SECONDS = 90
CI_LOG_LIMIT = 5000


def pull_request_checks(folder, url):
    """The checks of a pull request as [{"name", "state", "url"}], where state is passed, failed or pending."""
    raw = _gh(folder, "pr", "view", url, "--json", "statusCheckRollup", "--jq", ".statusCheckRollup")
    out = []
    for c in json.loads(raw or "[]") or []:
        status = str(c.get("status") or "").upper()
        conclusion = str(c.get("conclusion") or c.get("state") or "").upper()
        if status in ("QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED") or conclusion in ("PENDING", "EXPECTED") \
                or (not conclusion and status != "COMPLETED"):
            state = "pending"
        elif conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            state = "passed"
        else:
            state = "failed"
        out.append({"name": c.get("name") or c.get("context") or "check", "state": state,
                    "url": c.get("detailsUrl") or c.get("targetUrl") or ""})
    return out


def wait_for_checks(pulls, folders, minutes, stop=lambda: False):
    """Waits for the checks of every pull request. Returns (verdict, failures): "passed", "none" (no CI answered),
    "failed" (with [{"pull", "check", "url"}]), "pending" (the time ran out) or "stopped"."""
    start = time.monotonic()
    while True:
        if stop():
            return "stopped", []
        failures, pending, seen = [], False, False
        for pull in pulls:
            try:
                checks = pull_request_checks(folders[pull["id"]], pull["url"])
            except (KanvasError, ValueError):
                checks = []
            seen = seen or bool(checks)
            for c in checks:
                if c["state"] == "pending":
                    pending = True
                elif c["state"] == "failed":
                    failures.append({"pull": pull, "check": c["name"], "url": c["url"]})
        if not seen:
            if time.monotonic() - start >= CI_APPEAR_SECONDS:
                return "none", []
        elif not pending:
            return ("failed", failures) if failures else ("passed", [])
        elif time.monotonic() - start >= minutes * 60:
            return "pending", failures
        time.sleep(CI_POLL_SECONDS)


def ci_failure_logs(failures, folders):
    """The end of the log of each failed run, once per run, so the agent sees why the check failed."""
    seen, logs = set(), []
    for f in failures:
        run = re.search(r"/actions/runs/(\d+)", f["url"] or "")
        key = run.group(1) if run else f["url"] or f["check"]
        if key in seen:
            continue
        seen.add(key)
        try:
            text = _gh(folders[f["pull"]["id"]], "run", "view", run.group(1), "--log-failed") if run else ""
        except KanvasError as e:
            text = f"(the log could not be read: {e})"
        logs.append(f"### {f['pull']['name']}: {f['check']} ({f['url']})\n{text.strip()[-CI_LOG_LIMIT:]}")
    return "\n\n".join(logs)


def ci_fix_prompt(task, worktrees, failures, logs, attempt):
    repos = "\n".join(f"- {x['id']} ({x['name']}): {x['worktree']} on {x['branch']}" for x in worktrees)
    failed = "\n".join(f"- {f['pull']['name']}: {f['check']} ({f['pull']['url']})" for f in failures)
    return f"""You are fixing the failing CI of Factory task {task['id']}: {task['title']}. The pull requests are already open and the checks below failed (attempt {attempt}).

Worktrees:
{repos}

Failed checks:
{failed}

What the failed runs printed:
{logs or '(no log available; run the same commands the workflow runs, see .github/workflows)'}

Find the cause and fix it in the worktrees. Run the failing command again here before you finish when you can, with a time limit. Fix the code, not the check: do not delete, skip or weaken a test, a lint rule or the build to make it pass unless that test is wrong, and then say so. Do not change what the task is about. Commit your work with a conventional commit. Do not push or open pull requests: Factory does it. End with a short summary of the cause and the fix."""


def pull_request_body(task, summary):
    definition = task.get("definition") if isinstance(task.get("definition"), dict) else {}
    parts = [f"Factory task **{task['id']}**: {task['title']}"]
    for heading, key in (("Outcome", "outcome"), ("Acceptance criteria", "acceptance")):
        if definition.get(key):
            parts.append(f"## {heading}\n{definition[key]}")
    parts.append("## Agent summary\n" + (summary or "(none)"))
    parts.append("Opened by Factory after a Claude Code run. Review it, then merge it yourself.")
    return "\n\n".join(parts)


def publish_pull_requests(task, worktrees, summary, round_no=1):
    """Pushes each worktree branch that holds commits and opens (or finds) its pull request. Returns their records."""
    pulls = []
    for wt in worktrees:
        folder = wt["worktree"]
        check_task_branch(wt["branch"], wt)
        ahead = int(_git(folder, "rev-list", "--count", f"{wt['base']}..HEAD") or 0)
        if not ahead:
            continue
        if _git(folder, "status", "--porcelain", "--untracked-files=no"):
            raise KanvasError(f"{wt['id']}: tracked files in {folder} have uncommitted changes; the agent must commit its work")
        base = pr_base_branch(wt)
        push_branch(folder, wt["branch"])
        url = _gh(folder, "pr", "list", "--head", wt["branch"], "--base", base, "--state", "open",
                  "--json", "url", "--jq", ".[0].url")
        if url and round_no > 1:
            _gh(folder, "pr", "comment", url, "--body", f"Round {round_no} pushed by Factory.\n\n{summary}")
        if not url:
            url = _gh(folder, "pr", "create", "--base", base, "--head", wt["branch"],
                      "--title", f"{task['id']}: {task['title']}", "--body", pull_request_body(task, summary))
        pulls.append({"id": wt["id"], "name": wt["name"], "branch": wt["branch"], "base": base,
                      "commits": ahead, "url": url.splitlines()[-1].strip()})
    if not pulls:
        raise KanvasError("The agent made no commits, so there is no pull request to open")
    return pulls


# ---------------------------------------------------------------------------
# The reviewer: when a task reaches Review an agent reads the diff of its branches against the acceptance criteria. It
# approves, asks the implementer for changes (which the dispatcher runs as the next round, and reviews again), or leaves
# its findings for the human when it cannot decide or has already sent the work back too many times in a row. A human
# who sends the task back starts the cycle again.

REVIEW_DIFF_LIMIT = 60000
REVIEW_VERDICTS = ("approve", "changes", "escalate")
CHANGES_NOTE = "Agent review changes requested"


def review_worktrees(config, run):
    """The worktrees of a task's last run that still exist, with their repository's name and base."""
    found = []
    for item in run.get("worktrees") or []:
        repo = config["repositories"].get(item.get("id"))
        if repo and os.path.isdir(item.get("worktree") or ""):
            found.append({**repo, "worktree": item["worktree"], "branch": item.get("branch") or ""})
    if not found:
        raise KanvasError("The worktrees of this task no longer exist, so there is nothing to review")
    return found


def review_diff(worktree, base):
    """The commits, the files and the diff of a task's branch against its base, cut when the diff is very long."""
    log = _git(worktree, "log", "--oneline", f"{base}..HEAD")
    stat = _git(worktree, "diff", "--stat", f"{base}...HEAD")
    diff = _git(worktree, "diff", "--no-color", f"{base}...HEAD")
    if len(diff) > REVIEW_DIFF_LIMIT:
        diff = diff[:REVIEW_DIFF_LIMIT] + f"\n... (diff cut, {len(diff) - REVIEW_DIFF_LIMIT} more characters: open the files for the rest)"
    return log, stat, diff


def consecutive_agent_changes(task):
    """How many times in a row the reviewer sent the task back with no human note in between."""
    count = 0
    for note in reversed(task.get("notes") or []):
        if note.get("by") == "kanvas-runner":
            continue
        if not str(note.get("text") or "").startswith(CHANGES_NOTE):
            break
        count += 1
    return count


def sent_back_by_agent(task):
    """Whether the last thing said about the task, apart from the runner, was the reviewer asking for changes."""
    return consecutive_agent_changes(task) > 0


def last_review_findings(task):
    for note in reversed(task.get("notes") or []):
        text = str(note.get("text") or "")
        if text.startswith(CHANGES_NOTE):
            return text[len(CHANGES_NOTE):].lstrip(": ").strip()
    return ""


def review_prompt(task, run, worktrees, changes, previous, strong=False):
    definition = task.get("definition") if isinstance(task.get("definition"), dict) else {}
    work = task.get("work") if isinstance(task.get("work"), dict) else {}
    sections = "\n\n".join(
        f"## Repository {w['id']} ({w['name']})\nWorktree: {w['worktree']}\nBranch {w['branch']} against {w['base']}\n"
        f"Commits:\n{log or '(none)'}\n\nFiles changed:\n{stat or '(none)'}\n\n```diff\n{diff}\n```"
        for w, (log, stat, diff) in zip(worktrees, changes))
    again = (f"\nYour previous review asked for these changes; check that each one was fixed:\n{previous}\n"
             if previous else "")
    approved = work.get("design") if isinstance(work.get("design"), dict) else {}
    designed = (f"\nUI/UX design the human approved before the work started. It was decided on purpose: check that the change "
                f"builds it, and never ask for something it rules out (for example a convention it sets aside):\n"
                f"{_clip(approved.get('text'), 6000)}\n"
                if approved.get("status") == DESIGN_APPROVED and approved.get("text") else "")
    undecided = ('A first reviewer could not decide and sent this to you, so do not escalate: decide "approve" or '
                 '"changes". What the diff cannot show, such as how the backend behaves, goes in a finding to check, and '
                 'you ask for changes only for what you can point at. The human reads the pull request after you.'
                 if strong else
                 'Use "escalate" when the change touches authentication, permissions, multi-club isolation, migrations or '
                 'money, or when you cannot tell whether it is correct.')
    return f"""You review the work of an automated implementation agent for Factory task {task['id']}: {task['title']}. You did not write it and you must not edit it.

Product outcome: {definition.get('outcome') or task.get('desc') or '(not supplied)'}
Needs and constraints: {definition.get('needs') or '(not supplied)'}
Acceptance criteria: {definition.get('acceptance') or '(not supplied)'}
Non-goals: {definition.get('non_goals') or '(none)'}
Technical plan: {_clip(work.get('plan'), 3000) or '(not supplied)'}
{designed}
What the implementer reported about its last round:
{_clip(run.get('summary'), 3000) or '(nothing)'}
{again}
{sections}

Judge the diff against the acceptance criteria, one by one, and against the rules of the project (the CLAUDE.md of the repository, only for what changed). Open other files only when the diff is not enough, and run no builds or tests. If the report says the e2e did not run because its environment is missing, that alone is not a reason to ask for changes. Ask for changes only for problems you can point at in the diff; do not invent findings. {undecided} Answer in the language the task is written in.

End your reply with one fenced json block and nothing after it:
```json
{{"verdict": "approve", "summary": "<one line>", "findings": [{{"where": "path/to/file:line", "text": "..."}}]}}
```
"""


def clean_review(data):
    """A reviewer's JSON checked: {"verdict", "summary", "findings"} with at most 8 findings, or None."""
    if not isinstance(data, dict) or data.get("verdict") not in REVIEW_VERDICTS:
        return None
    findings = []
    for item in data.get("findings") or []:
        if isinstance(item, dict):
            where, text = _clip(item.get("where"), 200), _clip(item.get("text"), 600)
            if text:
                findings.append(f"`{where}`: {text}" if where else text)
        elif isinstance(item, str) and item.strip():
            findings.append(_clip(item, 600))
    return {"verdict": data["verdict"], "summary": _clip(data.get("summary"), 400) or data["verdict"], "findings": findings[:8]}


def ask_reviewer(config, task, run, worktrees, changes, previous, role_name):
    """Runs one reviewer role over the diffs and returns its checked verdict, what it cost and the model that answered."""
    role = config["agents"].get(role_name)
    if role is None:
        raise KanvasError(f"The board does not define the agent role {role_name}")
    os.makedirs(config["runs"], exist_ok=True)
    log = os.path.join(config["runs"], f"{automation_slug(task['id'])}-{role_name.replace('reviewer', 'review', 1)}.jsonl")
    prompt = review_prompt(task, run, worktrees, changes, previous, strong=role_name != "reviewer")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    # It runs from the project folder, where the project's agents live, and may read every worktree.
    command = claude_command(config, [{"worktree": config["root"]}] + worktrees, role)
    clear_stop(config["board"], task["id"])
    with open(log, "w", encoding="utf-8", newline="\n") as output:
        proc = agent_run(command, input=prompt, text=True, encoding="utf-8", errors="replace", stdout=output,
                         stderr=subprocess.STDOUT, creationflags=flags, cwd=config["root"], env=agent_environment(),
                         limits=agent_limits(config, role), stop=lambda: stop_requested(config["board"], task["id"]))
    clear_stop(config["board"], task["id"])
    if stop_text(proc):
        raise KanvasError(f"{role_name} did not finish the review. {stop_text(proc)}")
    result = read_result(log)
    if result is None or proc.returncode or result.get("is_error"):
        detail = str((result or {}).get("result") or f"it exited with code {proc.returncode}. See {log}")
        raise KanvasError(f"{role_name} could not review: {_clip(detail, 300)}")
    review = clean_review(split_assist_reply(str(result.get("result") or ""))[1])
    if review is None:
        raise KanvasError(f"{role_name} did not return a verdict")
    models = result.get("modelUsage") if isinstance(result.get("modelUsage"), dict) else {}
    return review, float(result.get("total_cost_usd") or 0), ", ".join(models)


def record_review(path, task_id, round_no, rounds_allowed, role_name, review, force=False):
    """Writes a reviewer's verdict on the task and returns what became of it: "approved", "changes" (the task goes back
    to Doing), "left" (the findings are left for the human) or None when the task had moved on while it was reviewed."""
    text = review["summary"] + "".join(f"\n- {f}" for f in review["findings"])

    def op(board):
        t = find_task(board, task_id)
        run = t.setdefault("work", {}).setdefault("automation", {})
        gate = (t.get("review") or {}).get("agent") or {}
        if t.get("state") != "review" or int(run.get("round") or 1) != round_no or (gate.get("status") != "pending" and not force):
            return None
        run["reviewed_round"] = round_no
        record = t.setdefault("review", {})
        record.setdefault("manual", {"status": "pending"})
        if review["verdict"] == "approve":
            record["agent"] = {"status": "approved", "by": role_name, "at": now_iso(), "summary": text}
            add_note(t, f"Agent review approved: {text}", role_name)
            return "approved"
        cycles = consecutive_agent_changes(t)
        if review["verdict"] == "changes" and cycles < rounds_allowed:
            record["agent"] = {"status": "changes_requested", "by": role_name, "at": now_iso(), "summary": text}
            record["manual"] = {"status": "pending"}
            t["state"] = "doing"
            add_note(t, f"{CHANGES_NOTE}: {text}", role_name)
            return "changes"
        why = "it could not decide" if review["verdict"] == "escalate" else f"it has asked for changes {cycles} times in a row"
        add_note(t, f"Automatic review left this to you ({why}): {text}", role_name)
        return "left"
    return mutate(path, op)


def review_comment(role_name, model, round_no, outcome, review):
    """The comment the reviewer leaves on each pull request, for the human who reads it next."""
    heading = {"approved": "Approved", "changes": "Changes requested", "left": "Needs your attention"}[outcome]
    tail = {"approved": "No blocking findings: ready for your review.",
            "changes": "The implementer will fix this in the next round and Factory will review it again.",
            "left": "The automatic review stops here: the points above are left for you."}[outcome]
    lines = [f"**Factory review** · {role_name}" + (f" ({model})" if model else "") + f" · round {round_no}", "",
             f"**{heading}.** {review['summary']}"]
    if review["findings"]:
        lines += [""] + [f"- {f}" for f in review["findings"]]
    return "\n".join(lines + ["", tail])


def _run_review(path, task_id, force=False):
    """Has the reviewer agent look at a task that reached Review and records its verdict. Returns (outcome, text) and
    never raises: a review that cannot run leaves a note and the task for the human. A stronger reviewer looks again
    at what the first could not decide."""
    round_no = 1
    try:
        board = load_board(path)
        config = automation_config(board, path)
        if config is None:
            raise KanvasError("Automation is not enabled for this board")
        task = find_task(board, task_id)
        run = ((task.get("work") or {}).get("automation")) or {}
        round_no = int(run.get("round") or 1)
        worktrees = review_worktrees(config, run)
        changes = [review_diff(w["worktree"], w["base"]) for w in worktrees]
        previous = last_review_findings(task)
        role_name = "reviewer"
        review, cost, model = ask_reviewer(config, task, run, worktrees, changes, previous, role_name)
        if review["verdict"] == "escalate":
            role_name = "reviewer-strong"
            review, more, model = ask_reviewer(config, task, run, worktrees, changes, previous, role_name)
            cost += more
        outcome = record_review(path, task_id, round_no, config["review"]["rounds"], role_name, review, force)
        if outcome is None:
            return "stale", "The task moved on while it was being reviewed."
        body = review_comment(role_name, model, round_no, outcome, review)
        for pull in run.get("pull_requests") or []:
            try:
                _gh(config["root"], "pr", "comment", pull["url"], "--body", body)
            except (KanvasError, KeyError):
                pass
        return outcome, review["summary"]
    except Exception as e:
        def fail(board):
            t = find_task(board, task_id)
            run = t.setdefault("work", {}).setdefault("automation", {})
            if t.get("state") == "review" and int(run.get("round") or 1) == round_no:
                run["reviewed_round"] = round_no
                add_note(t, f"Automatic review failed: {e}", "kanvas-runner")
            return t
        try:
            mutate(path, fail)
        except Exception:
            pass
        return "failed", str(e)


def run_automation(path, task_id):
    """Prepares worktrees, runs one Claude Code session and opens the pull requests. Meant for a daemon thread."""
    try:
        board = load_board(path)
        task = find_task(board, task_id)
        config = automation_config(board, path)
        if config is None:
            raise KanvasError("Automation is not enabled for this board")
        role_name = task_agent_name(task)
        role = config["agents"].get(role_name)
        if role is None:
            raise KanvasError(f"{task_id} asks for the agent role {role_name}, which the board does not define "
                              f"(it defines {', '.join(config['agents'])})")
        worktrees = prepare_automation_worktrees(task, config)
        previous = (task.get("work") or {}).get("automation") or {}
        round_no = int(previous.get("round") or 1)
        feedback = automation_feedback(task, previous, config["root"]) if round_no > 1 else ""
        os.makedirs(config["runs"], exist_ok=True)
        log = os.path.join(config["runs"], f"{automation_slug(task_id)}.jsonl")
        update_automation(path, task_id, "running", started=now_iso(), log=log, executor="claude", agent=role_name,
                          worktrees=[{k: x[k] for k in ("id", "path", "worktree", "branch", "commit")} for x in worktrees])
        prompt = automation_prompt(board, task, config, worktrees, round_no, feedback)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        clear_stop(path, task_id)
        with open(log, "w", encoding="utf-8", newline="\n") as output:
            proc = agent_run(claude_command(config, worktrees, role), input=prompt, text=True, encoding="utf-8",
                             errors="replace", stdout=output, stderr=subprocess.STDOUT, creationflags=flags,
                             cwd=worktrees[0]["worktree"], env=agent_environment(),
                             limits=agent_limits(config, role), stop=lambda: stop_requested(path, task_id))
        clear_stop(path, task_id)
        if stop_text(proc):
            # An agent that wrote nothing for a while usually lost its connection: it is continued once before it waits for you.
            quiet = str(proc.stopped).startswith("it wrote nothing")
            finish_automation(path, task_id, False, f"{stop_text(proc)} What it had done so far is in the worktree and "
                                                    f"the log ({log}). `retry {task_id}` runs it again from there.",
                              resume="it wrote nothing for a while, probably a lost connection" if quiet else "")
            return
        ok, summary = claude_outcome(proc, log)
        if not summary:
            summary = f"Claude exited with code {proc.returncode}. See {log}"
        if round_no > 1:
            summary = f"Round {round_no}. {summary}"
        pulls = []
        if ok:
            save_changes(config, task_id, worktrees)
            try:
                pulls = publish_pull_requests(task, worktrees, summary, round_no)
                summary += "\n\nPull requests:\n" + "\n".join(f"- {x['name']}: {x['url']}" for x in pulls)
            except KanvasError as e:
                ok = False
                summary += f"\n\nPull request not opened: {e}"
            if ok and config["ci"]["enabled"]:
                ok, ci_text = ensure_checks(path, task, config, role, worktrees, pulls, summary, log)
                summary += "\n\n" + ci_text
        finish_automation(path, task_id, ok, summary, pulls)
    except Exception as e:
        try:
            finish_automation(path, task_id, False, str(e))
        except Exception:
            pass


def _heads(worktrees):
    return [_git(w["worktree"], "rev-parse", "HEAD") for w in worktrees]


def ensure_checks(path, task, config, role, worktrees, pulls, summary, log):
    """Before the task goes to Review: waits for the checks of its pull requests and has the agent fix the ones that fail,
    up to automation.ci.max_fixes times. Returns (ok, one line for the summary)."""
    task_id, ci = task["id"], config["ci"]
    folders = {w["id"]: w["worktree"] for w in worktrees}
    fixes = 0
    while True:
        update_automation(path, task_id, "running", ci="waiting for the checks of the pull requests")
        verdict, failures = wait_for_checks(pulls, folders, ci["timeout_minutes"], lambda: stop_requested(path, task_id))
        update_automation(path, task_id, "running", ci="")
        if verdict == "stopped":
            return False, "CI: stopped while waiting for the checks."
        if verdict == "none":
            return True, "CI: the pull requests have no checks."
        if verdict == "pending":
            return True, f"CI: the checks were still running after {ci['timeout_minutes']} minutes; check them before you approve."
        if verdict == "passed":
            return True, "CI: all checks passed" + (f" after {fixes} fix{'es' if fixes > 1 else ''} by the agent." if fixes else ".")
        names = ", ".join(sorted({f"{f['pull']['name']}: {f['check']}" for f in failures}))
        if fixes >= ci["max_fixes"]:
            return False, f"CI still fails after {fixes} fix{'es' if fixes != 1 else ''} by the agent: {names}."
        fixes += 1
        before = _heads(worktrees)
        logs = ci_failure_logs(failures, folders)
        fix_log = os.path.splitext(log)[0] + f"-ci{fixes}.jsonl"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        update_automation(path, task_id, "running", ci=f"the agent is fixing the failed checks (attempt {fixes})")
        clear_stop(path, task_id)
        with open(fix_log, "w", encoding="utf-8", newline="\n") as output:
            proc = agent_run(claude_command(config, worktrees, role), input=ci_fix_prompt(task, worktrees, failures, logs, fixes),
                             text=True, encoding="utf-8", errors="replace", stdout=output, stderr=subprocess.STDOUT,
                             creationflags=flags, cwd=worktrees[0]["worktree"], env=agent_environment(),
                             limits=agent_limits(config, role), stop=lambda: stop_requested(path, task_id))
        clear_stop(path, task_id)
        update_automation(path, task_id, "running", ci="")
        if stop_text(proc):
            return False, f"CI fails ({names}). {stop_text(proc)}"
        save_changes(config, task_id, worktrees)
        if _heads(worktrees) == before:
            return False, f"CI fails ({names}) and the agent changed nothing to fix it. See {fix_log}."
        try:
            publish_pull_requests(task, worktrees, summary, 1)
        except KanvasError as e:
            return False, f"CI fails ({names}); the fix could not be pushed: {e}"


DESIGN_APPROVED, DESIGN_PENDING, DESIGN_CHANGES, DESIGN_FAILED = "approved", "pending", "changes_requested", "failed"


def design_of(task):
    work = task.get("work") if isinstance(task.get("work"), dict) else {}
    return work.get("design") if isinstance(work.get("design"), dict) else {}


DESIGN_HISTORY_LIMIT = 30


def design_history(design):
    """The conversation about a design, oldest first: each proposal of the designer, each answer of the human (which
    round it answers) and the approval. A design made before the history existed starts from its current proposal."""
    history = [dict(x) for x in design.get("history") or [] if isinstance(x, dict)]
    if not history and design.get("text"):
        history.append({"type": "proposal", "round": int(design.get("round") or 1), "at": design.get("at") or "",
                        "by": design.get("by") or "designer", "text": design["text"],
                        **({"mockup": design["mockup"]} if design.get("mockup") else {})})
    return history


def design_applies(task, config):
    """Whether the task still has to have its UI/UX design approved before the implementer runs: it is in progress,
    has not run yet and changes a repository that needs a design."""
    work = task.get("work") if isinstance(task.get("work"), dict) else {}
    run = work.get("automation") if isinstance(work.get("automation"), dict) else {}
    if not (config["design"]["enabled"] and task.get("state") == "doing" and not run.get("status")):
        return False
    # The "Requires design" check of the plan dialog decides; a task started before it existed follows its repositories.
    if isinstance(work.get("needs_design"), bool):
        return work["needs_design"]
    return bool(set(task_repository_ids(task)) & set(config["design"]["repositories"]))


def design_prompt(task, config, previous, feedback):
    definition = task.get("definition") if isinstance(task.get("definition"), dict) else {}
    work = task.get("work") if isinstance(task.get("work"), dict) else {}
    repos = "\n".join(f"- {r['id']} ({r['name']}): {r['path']}" for i, r in config["repositories"].items()
                      if i in config["design"]["repositories"])
    again = (f"\nYour previous proposal:\n{_clip(previous, 6000)}\n\nThe human asked for these changes; address every one:\n"
             f"{feedback or '(no written feedback)'}\n" if previous else "")
    return f"""You are the UI/UX designer for Factory task {task['id']}: {task['title']}. Nobody has written code for it yet and you do not write any: you propose the design and a human approves it before an implementer builds it.

Application repositories (read only):
{repos}

Product outcome: {definition.get('outcome') or task.get('desc') or '(not supplied)'}
Needs and constraints: {definition.get('needs') or '(not supplied)'}
Acceptance criteria: {definition.get('acceptance') or '(not supplied)'}
Non-goals: {definition.get('non_goals') or '(none)'}
Technical plan: {_clip(work.get('plan'), 3000) or '(not supplied)'}
{again}
Do not invent a visual language. Read the CLAUDE.md of the application, its design decisions (the adr folder), the shared styles and components, and one or two existing screens that resemble this one, then design with what exists. Design the three screen sizes the app is used on: mobile (a phone, the main one), tablet (an iPad, in portrait and landscape) and web (a desktop browser). Mobile first; say what changes on each of the others.

Answer in the language the task is written in, in markdown, under 700 words, with these sections:
1. **Goal** and who uses it, per role.
2. **Mobile layout** (phone): how it is laid out, top to bottom, and the interactions.
3. **Tablet layout** (iPad, portrait and landscape): what it does with the extra space (columns, split view, side panel) instead of stretching the phone layout.
4. **Web layout** (desktop browser): what changes with a mouse and keyboard (hover, context menu, shortcuts, wide tables).
5. **Reuse**: the existing components, styles and screens it builds on, with their paths. A new component needs one line of justification.
6. **States**: loading, empty, error, success and no permission.
7. **Texts**: the labels and messages, as the app would show them.
8. **Accessibility** that matters here.
9. **Open questions** the human should decide. If there are none, say so.

Then end with the mockup, so the human sees the design as it will look instead of reading about it: one fenced ```html block with a complete, standalone HTML page (under 40,000 characters) and nothing after it. Rules for the mockup:
- It is the real design, not a sketch: copy the application's own CSS (its variables, classes, fonts and spacing) into one `<style>`, use the shared classes, and fill it with believable sample data. No new colours or fonts.
- One responsive file: the same page is shown in a 390 px wide frame (phone), an 820 px frame (iPad portrait) and a 1280 px frame (web), so it must adapt with the application's own breakpoints.
- Make it interactive where the task needs it (filters that filter, tabs that switch, a form that shows its validation) with a few lines of inline JavaScript.
- No external requests: no links to files, fonts, images or scripts; use inline SVG or CSS for any image.
"""


# While an agent designs or reviews a task the board marks it, and the page lets the human look at it and nothing else.
# (A run of the implementer is already marked by its own status.) The page ignores a mark older than half an hour.
def mark_busy(path, task_id, who):
    def op(board):
        t = find_task(board, task_id)
        work = t.get("work") if isinstance(t.get("work"), dict) else None
        if who:
            t.setdefault("work", {})["busy"] = {"by": who, "at": now_iso()}
        elif work is not None:
            work.pop("busy", None)
        return t
    try:
        mutate(path, op)
    except (KanvasError, OSError):
        pass


def clear_busy_marks(path):
    """When Factory starts nothing is being worked on, so any mark a crash or a restart left behind is stale: a run that
    still says queued or running was cut off, and is marked failed so that `retry` (the Retry button) continues it."""
    def live(t):
        work = t.get("work") if isinstance(t.get("work"), dict) else {}
        run = work.get("automation") if isinstance(work.get("automation"), dict) else {}
        return isinstance(work.get("busy"), dict) or run.get("status") in ("queued", "running")
    if not any(live(t) for t in load_board(path)["tasks"]):
        return

    def op(board):
        for t in board["tasks"]:
            if not isinstance(t.get("work"), dict):
                continue
            t["work"].pop("busy", None)
            run = t["work"].get("automation")
            if isinstance(run, dict) and run.get("status") in ("queued", "running") and not t["work"].get("stop"):
                if t.get("state") == "doing" and resume_run(t, "Factory restarted while the agent was working"):
                    continue
                message = "Factory restarted while the agent was working; its work is kept in the worktree. Press Retry to continue."
                t["work"]["automation"] = {**run, "status": "failed", "updated": now_iso(), "summary": message}
                add_note(t, "Automation interrupted: " + message, "kanvas-runner")
    mutate(path, op)


def run_review(path, task_id, force=False):
    mark_busy(path, task_id, "reviewer")
    try:
        return _run_review(path, task_id, force)
    finally:
        mark_busy(path, task_id, None)


def run_design(path, task_id):
    mark_busy(path, task_id, "designer")
    try:
        return _run_design(path, task_id)
    finally:
        mark_busy(path, task_id, None)


MOCKUP_LIMIT = 60000


def split_design_reply(text):
    """The designer's description and the HTML mockup that ends it, which is "" when there is none or it is not a page."""
    found = list(re.finditer(r"```html\s*(.*?)```", text, re.S))
    if not found:
        return text.strip(), ""
    last = found[-1]
    page_html = last.group(1).strip()
    prose = (text[:last.start()] + text[last.end():]).strip()
    if not re.search(r"<(!doctype|html|body|main|div|style)\b", page_html, re.I) or len(page_html) > MOCKUP_LIMIT:
        return prose, ""
    return prose, page_html


# The mockup is the designer's own markup, shown in the human's browser: it may run its own script (a filter that
# filters) but gets no origin, no network and no access to the board.
MOCKUP_HEADERS = {"Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; "
                                              "script-src 'unsafe-inline'; img-src data:; font-src data:",
                  "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def _run_design(path, task_id):
    """Has the designer propose the UI/UX of a task and records the proposal for the human to approve. Never raises: a
    design that cannot be made leaves a note and the task waiting."""
    try:
        board = load_board(path)
        config = automation_config(board, path)
        if config is None:
            raise KanvasError("Automation is not enabled for this board")
        task = find_task(board, task_id)
        role = config["agents"].get("designer")
        if role is None:
            raise KanvasError("The board does not define the agent role designer")
        before = design_of(task)
        os.makedirs(config["runs"], exist_ok=True)
        log = os.path.join(config["runs"], f"{automation_slug(task_id)}-design.jsonl")
        prompt = design_prompt(task, config, before.get("text") if before.get("status") == DESIGN_CHANGES else "",
                               before.get("feedback") or "")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        folders = [{"worktree": config["root"]}] + [{"worktree": config["repositories"][i]["path"]}
                                                   for i in config["design"]["repositories"] if i in config["repositories"]]
        clear_stop(path, task_id)
        with open(log, "w", encoding="utf-8", newline="\n") as output:
            proc = agent_run(claude_command(config, folders, role), input=prompt, text=True, encoding="utf-8",
                             errors="replace", stdout=output, stderr=subprocess.STDOUT, creationflags=flags,
                             cwd=config["root"], env=agent_environment(),
                             limits=agent_limits(config, role), stop=lambda: stop_requested(path, task_id))
        clear_stop(path, task_id)
        if stop_text(proc):
            raise KanvasError(f"the designer did not finish. {stop_text(proc)}")
        result = read_result(log)
        text = str((result or {}).get("result") or "").strip()
        if result is None or proc.returncode or result.get("is_error") or not text:
            detail = text or f"it exited with code {proc.returncode}. See {log}"
            raise KanvasError(f"the designer could not make the proposal: {_clip(detail, 300)}")
        text, mockup = split_design_reply(text)
    except Exception as e:
        def fail(board):
            t = find_task(board, task_id)
            if design_applies(t, automation_config(board, path)):
                t.setdefault("work", {})["design"] = {**design_of(t), "status": DESIGN_FAILED, "at": now_iso()}
                add_note(t, f"Design failed: {e}", "kanvas-runner")
            return t
        try:
            mutate(path, fail)
        except Exception:
            pass
        return "failed", str(e)

    def record(board):
        t = find_task(board, task_id)
        if not design_applies(t, automation_config(board, path)):
            return None
        design = design_of(t)
        if design.get("status") in (DESIGN_PENDING, DESIGN_APPROVED):
            return None
        round_no, at = int(design.get("round") or 0) + 1, now_iso()
        entry = {"type": "proposal", "round": round_no, "at": at, "by": "designer", "text": text,
                 **({"mockup": mockup} if mockup else {})}
        t.setdefault("work", {})["design"] = {"status": DESIGN_PENDING, "text": text, "round": round_no, "by": "designer", "at": at,
                                              **({"mockup": mockup} if mockup else {}),
                                              "history": (design_history(design) + [entry])[-DESIGN_HISTORY_LIMIT:]}
        add_note(t, "Design proposal ready: approve it or ask for changes before the implementation starts.", "designer")
        return t
    return ("pending", text) if mutate(path, record) else ("stale", "The task moved on while the design was made.")


def set_design(path, task_id, approve, feedback=""):
    """The human's answer to a design proposal: approve it, or ask for changes, which makes the designer try again."""
    def op(board):
        t = find_task(board, task_id)
        design = design_of(t)
        if t.get("state") != "doing" or design.get("status") not in (DESIGN_PENDING, DESIGN_FAILED, DESIGN_CHANGES):
            raise KanvasError(f"{t['id']} has no design waiting for an answer")
        if approve:
            if design.get("status") != DESIGN_PENDING:
                raise KanvasError(f"{t['id']} has no design proposal to approve")
            at = now_iso()
            t["work"]["design"] = {**design, "status": DESIGN_APPROVED, "by": "human", "at": at, "feedback": "",
                                   "history": design_history(design) + [{"type": "approved", "round": int(design.get("round") or 1),
                                                                         "at": at, "by": "human"}]}
            add_note(t, "Design approved.", "human")
        else:
            at = now_iso()
            t["work"]["design"] = {**design, "status": DESIGN_CHANGES, "feedback": feedback, "at": at,
                                   "history": design_history(design) + [{"type": "feedback", "round": int(design.get("round") or 1),
                                                                         "at": at, "by": "human", "text": feedback}]}
            add_note(t, f"Design changes requested: {feedback}" if feedback else "Design requested again.", "human")
        return t
    return mutate(path, op)


CHANGES_LIMIT = 30000


def changes_path(config, task_id):
    return os.path.join(config["runs"], f"{automation_slug(task_id)}.diff")


def save_changes(config, task_id, worktrees):
    """Keeps what a finished run changed (files and diff, per repository) beside its log. By the time the task is
    accepted its branch is merged and no longer differs from its base, so the documentation task reads this copy."""
    parts = []
    for item in worktrees:
        try:
            base = config["repositories"][item["id"]]["base"]
            stat = _git(item["worktree"], "diff", "--stat", f"{base}...HEAD")
            diff = _git(item["worktree"], "diff", "--no-color", f"{base}...HEAD")
        except (KanvasError, KeyError, OSError):
            continue
        if len(diff) > CHANGES_LIMIT:
            diff = diff[:CHANGES_LIMIT] + "\n... (diff cut)"
        parts.append(f"## {item['id']}\n\n{stat}\n\n{diff}")
    try:
        with open(changes_path(config, task_id), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n\n".join(parts))
    except OSError:
        pass


def curator_plan(task, run, config):
    """The plan of the documentation task: what the accepted task did and where to find it."""
    try:
        with open(changes_path(config, task["id"]), encoding="utf-8") as f:
            changes = f.read().strip()
    except OSError:
        changes = ""
    return (f"Update the documentation of the `brain` repository for the accepted task {task['id']}: {task['title']}.\n\n"
            f"Edit only files of this repository (modules/, adr/, ops/ and README.md). Change only the statements the work "
            f"makes false, keep the wording of the file, and add an ADR in adr/ only for a new decision that is hard to "
            f"reverse. If the documentation is already correct, change nothing and say so.\n\n"
            f"What the implementer reported:\n{run.get('summary') or '(no summary)'}\n\n"
            f"Changes of the accepted task:\n{changes or '(not available: rely on the summary)'}")


def curate_task(path, task_id):
    """Creates the documentation task for an accepted automated task and marks the accepted one as handled.
    Returns the new task's id, or None when there is nothing to do."""
    def op(board):
        config = automation_config(board, path)
        if config is None or not config["curator"]["enabled"] or CURATOR_REPOSITORY not in config["repositories"]:
            return None
        task = find_task(board, task_id)
        run = ((task.get("work") or {}).get("automation")) or {}
        repos = task_repository_ids(task)
        if (task.get("state") != "done" or task.get("curated") or run.get("status") != "completed" or not repos
                or CURATOR_REPOSITORY in repos):
            return None
        group = next((g for g in board["groups"] if g["id"] == task.get("group")), None)
        doc = add_task(board, f"Docs: {task['title']}", group, f"Documentation for {task['id']}, kept up to date by the curator.",
                       state="doing", by="kanvas")
        doc["definition"] = {
            "outcome": f"The brain documentation matches the accepted task {task['id']}.",
            "needs": "Only documentation; change what the work made false.",
            "acceptance": "Every statement the accepted work changed is correct in the brain documentation, and nothing else changes.",
            "non_goals": "Code changes; rewriting sections that are still true.",
            "repositories": [CURATOR_REPOSITORY], "agent": "curator", "approved": True, "by": "kanvas", "at": now_iso()}
        doc["work"] = {"by": "kanvas", "at": now_iso(), "plan": curator_plan(task, run, config),
                       "repositories": [CURATOR_REPOSITORY], "agent": "curator"}
        task["curated"] = doc["id"]
        return doc["id"]
    return mutate(path, op)


def read_input(value, use_stdin=False, file_path=None, what="text"):
    """Text from an argument, stdin, or a file.

    stdin and files are decoded as UTF-8 on every platform, and they avoid
    shells that mangle multi-line arguments (notably on Windows).
    """
    if sum([value is not None, bool(use_stdin), bool(file_path)]) > 1:
        raise KanvasError(f"Give the {what} only once: as an argument, with --stdin, or with --file")
    if use_stdin:
        text = sys.stdin.buffer.read().decode("utf-8-sig")
    elif file_path:
        try:
            text = read_text_file(file_path)
        except OSError as e:
            raise KanvasError(f"Cannot read {file_path}: {e}")
    else:
        text = value or ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def rel(path):
    try:
        r = os.path.relpath(path)
    except ValueError:
        return path
    return path if r.startswith("..") else r


def id_width(tasks):
    return max([len(t["id"]) for t in tasks] + [5])


def yolo_mark(task, cover):
    return "  (YOLO)" if cover and task["state"] in ("proposed", "todo", "doing") and task["id"].upper() in cover else ""


def _gist(text):
    first = text.split("\n", 1)[0]
    return first[:57].rstrip() + "..." if len(first) > 60 else first


def waiting_mark(task):
    q = open_question(task)
    return f"  (waiting for the human: {_gist(question_text(q))})" if q else ""


def changes_mark(task, cover):
    r = change_request(task) if task["id"].upper() not in cover else None
    return f"  (changes requested: {_gist(change_text(r))})" if r else ""


def print_rows(rows, tasks_by_id, indent="  ", show_state=True, cover=None):
    w = id_width(rows)
    for t in rows:
        state = f"{view_state(t, tasks_by_id):<10}" if show_state else ""
        print(f"{indent}{t['id']:<{w}}  {state}{t['title']}{yolo_mark(t, cover)}")


def print_task(board, task):
    tasks = task_map(board)
    cover = yolo_cover(board)
    print(f"{task['id']}  {task['title']}")
    print(f"State:  {view_state(task, tasks)}")
    if q := open_question(task):
        print(f"Waits:  for the human to reply to the question {q.get('by') or 'agent'} asked; the next note ends the wait")
    if changes_mark(task, cover):
        print(f"Revise: the human asked for changes (see the notes); change the proposal with "
              f"`{command_line('edit ' + task['id'])}` and say what you changed with -m, which answers the request")
    print(f"Group:  {group_label(board, task.get('group'))}")
    if task.get("by"):
        print(f"By:     {task['by']}" + (f", {task['at']}" if task.get("at") else ""))
    via = cover.get(task["id"].upper())
    if via and task["state"] == "proposed":
        print(f"YOLO:   joins the chain of {via} once a human approves it")
    elif via and task["state"] not in ("review", "develop", "done", "production"):
        if via == task["id"]:
            print("YOLO:   set on this task; all workflow gates still apply")
        else:
            print(f"YOLO:   through {via}; all workflow gates still apply")
    if task.get("deps"):
        print("Depends on:")
        for dep in task["deps"]:
            d = tasks.get(dep.upper())
            if d is None:
                print(f"  [?] {dep}  (missing)")
            else:
                mark = "x" if is_complete(d) else " "
                print(f"  [{mark}] {d['id']}  {d['title']}  ({view_state(d, tasks)}){yolo_mark(d, cover)}")
    blocks = [t for t in board["tasks"] if task["id"].upper() in (d.upper() for d in t.get("deps", []))]
    if blocks:
        print("Blocks:")
        for t in blocks:
            print(f"  {t['id']}  {t['title']}  ({view_state(t, tasks)}){yolo_mark(t, cover)}")
    print("\nDescription:")
    print(_indent(task.get("desc") or "(none)"))
    if task.get("definition"):
        print("\nProduct definition:")
        for key in ("outcome", "needs", "acceptance", "non_goals", "priority"):
            if task["definition"].get(key):
                print(f"  {key.replace('_', ' ').title()}: {PRIORITIES.get(task['definition'][key], task['definition'][key])}")
    if task.get("work"):
        print("\nConstruction:")
        print(f"  {task['work'].get('by', '?')} · {task['work'].get('at', '')}")
        print(_indent(task["work"].get("plan", ""), "  "))
        for git in task["work"].get("git", []):
            dirty = " + uncommitted changes" if git.get("dirty") else ""
            print(f"  Git: {git.get('branch', '?')} @ {str(git.get('commit', ''))[:12]}{dirty}")
            print(f"       {git.get('worktree') or git.get('repo', '')}")
        if task["work"].get("automation"):
            run = task["work"]["automation"]
            print(f"  Automation: {run.get('status', 'unknown')}" + (f" · {run.get('log')}" if run.get("log") else ""))
            if run.get("summary"):
                print(_indent(run["summary"], "    "))
    if task.get("review"):
        agent = task["review"].get("agent") or {}
        manual = task["review"].get("manual") or {}
        print(f"\nReview: agent={agent.get('status', 'pending')}, manual={manual.get('status', 'pending')}")
    if task.get("delivery"):
        print("\nDelivery:")
        for stage in ("dev", "production"):
            evidence = task["delivery"].get(stage)
            if evidence:
                print(f"  {stage}: {evidence.get('evidence', '')}")
    if task.get("notes"):
        print("\nNotes:")
        for n in task["notes"]:
            edited = f", edited {_when(n['edited'])}" if n.get("edited") else ""
            print(f"  {_when(n.get('at', ''))}  {n.get('by', '?')}{edited}:")
            print(_indent(n.get("text", ""), "    "))


def _when(iso):
    return str(iso).replace("T", " ").replace("Z", " UTC")


def _indent(text, prefix="  "):
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


STATE_HINTS = {
    "proposed": "it is a proposal; a human must approve it first",
    "doing": "it is already in progress",
    "review": "it is waiting for agent and manual review",
    "develop": "it is merged to dev",
    "done": "it is done",
    "production": "it is in production",
    "blocked": "it is blocked",
}


def kanvas_on_path():
    """True if the `kanvas` command is there to type after this run.

    uvx and `uv run` (which set UV) put the running environment on PATH only while it runs.
    """
    found = shutil.which("kanvas")
    if not found or not os.environ.get("UV"):
        return bool(found)
    inside = os.path.normcase(os.path.join(os.path.abspath(sys.prefix), ""))
    return not os.path.normcase(os.path.abspath(found)).startswith(inside)


def command_name():
    """The installed command the user typed: `factory`, or `kanvas`, which works the same."""
    return "factory" if os.path.splitext(os.path.basename(sys.argv[0] or ""))[0].lower() == "factory" else "kanvas"


def command_line(rest=""):
    """The command to type to run Factory the way it was started, followed by `rest`."""
    if INSTALLED:
        head = command_name() if kanvas_on_path() else "uvx kanvas-board"
    else:
        script = sys.argv[0] or "kanvas.py"
        head = f'python "{script}"' if " " in script else f"python {script}"
    return f"{head} {rest}" if rest else head


def project_command(rest=""):
    """What to type inside a project after `init`: the installed command, or the project's own copy."""
    head = command_name() if INSTALLED and kanvas_on_path() else "python .agents/skills/kanvas/kanvas.py"
    return f"{head} {rest}" if rest else head


def _version_key(version):
    """Sortable form of a release number: 0.4.0a1 < 0.4.0b1 < 0.4.0rc1 < 0.4.0 < 0.4.1."""
    m = re.fullmatch(r"(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?", version.strip())
    if not m:
        return None
    numbers = tuple(int(part) for part in m.group(1).split("."))
    numbers += (0,) * (3 - len(numbers))
    stage = ("a", "b", "rc").index(m.group(2)) if m.group(2) else 3
    return numbers, stage, int(m.group(3) or 0)


def agent_copy_hint(board_path):
    """A line saying the project's agent copy differs from the installed command, or None."""
    if not INSTALLED:
        return None
    copy = os.path.join(os.path.dirname(board_path), ".agents", "skills", "kanvas", "kanvas.py")
    try:
        m = re.search(r'^VERSION = "([^"]+)"', read_text_file(copy), re.M)
    except (OSError, ValueError):
        return None
    theirs, ours = _version_key(m.group(1)) if m else None, _version_key(VERSION)
    if theirs is None or ours is None or theirs == ours:
        return None
    if theirs < ours:
        return f"This project's agent copy is {m.group(1)}; run `{command_line('init')}` to update it to {VERSION}."
    return (f"This project's agent copy is {m.group(1)}, newer than your kanvas {VERSION}; "
            "update it with `uv tool upgrade kanvas-board`.")


# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------


def cmd_status(args):
    path = board_path(args)
    board = load_board(path)
    tasks = task_map(board)
    cover = yolo_cover(board)
    buckets = {k: [] for k in ("doing", "review", "todo", "blocked", "proposed", "done", "production")}
    for t in board["tasks"]:
        buckets.setdefault(view_state(t, tasks), []).append(t)
    total = len(board["tasks"])
    print(f"{board.get('title') or board_name(path)}  ({rel(path)})")
    if not total:
        print("No tasks yet. Propose some with `propose` or `batch`.")
    else:
        names = {"todo": "ready"}
        counts = ", ".join(f"{len(v)} {names.get(k, k)}" for k, v in buckets.items() if v)
        print(f"{total} tasks: {counts}")
    if board["groups"]:
        parts = []
        for g in board["groups"]:
            members = [t for t in board["tasks"] if t.get("group") == g["id"]]
            done = sum(1 for t in members if is_complete(t))
            parts.append(f"{g['name']} [{g['id']}] {done}/{len(members)}")
        print("Groups: " + ", ".join(parts))
    yolo = [t["id"] for t in board["tasks"] if cover.get(t["id"].upper()) == t["id"]]
    if yolo:
        names = ", ".join(yolo)
        print(f"YOLO on {names}: tasks marked (YOLO) still require definition, design and review gates; "
              "YOLO writes their plans and keeps the chain selected for autonomous work.")
    sections = (
        ("doing", "Doing"),
        ("review", "Waiting for agent/manual review"),
        ("todo", "Ready to start"),
        ("blocked", "Blocked"),
        ("proposed", "Proposed, awaiting human approval"),
    )
    for key, label in sections:
        rows = buckets[key]
        if not rows:
            continue
        print(f"\n{label} ({len(rows)})")
        w = id_width(rows)
        for t in rows:
            line = f"  {t['id']:<{w}}  {t['title']}"
            if key == "blocked":
                line += f"  (waiting on {', '.join(unmet_deps(t, tasks))})"
            line += yolo_mark(t, cover) + waiting_mark(t) + changes_mark(t, cover)
            print(line)
    errors, warnings = problems(board)
    if errors or warnings:
        print("\nProblems")
        for e in errors:
            print(f"  error: {e}")
        for w in warnings:
            print(f"  warning: {w}")
    if hint := agent_copy_hint(path):
        print(f"\n{hint}")


def cmd_list(args):
    board = load_board(board_path(args))
    tasks = task_map(board)
    cover = yolo_cover(board)
    rows = board["tasks"]
    groups = list(board["groups"])
    flt = (args.filter or "").strip().lower()
    if flt:
        state = "todo" if flt == "ready" else flt
        if state in STATES or state == "blocked":
            rows = [t for t in rows if view_state(t, tasks) == state]
        else:
            g = find_group(board, args.filter)
            groups = [g]
            rows = [t for t in rows if t.get("group") == g["id"]]
    if not rows:
        print("No matching tasks.")
        return
    known = {g["id"] for g in board["groups"]}
    sections = [(f"{g['name']} [{g['id']}]", [t for t in rows if t.get("group") == g["id"]]) for g in groups]
    sections.append(("Ungrouped", [t for t in rows if t.get("group") not in known]))
    first = True
    for label, members in sections:
        if not members:
            continue
        if not first:
            print()
        first = False
        print(label)
        print_rows(members, tasks, cover=cover)


def cmd_show(args):
    board = load_board(board_path(args))
    print_task(board, find_task(board, args.id))


def cmd_ready(args):
    board = load_board(board_path(args))
    tasks = task_map(board)
    cover = yolo_cover(board)
    ready = [t for t in board["tasks"] if view_state(t, tasks) == "todo"]
    if ready:
        print_rows(ready, tasks, indent="", show_state=False, cover=cover)
        return
    states = Counter(view_state(t, tasks) for t in board["tasks"])
    hints = [f"{states[k]} {k}" for k in ("doing", "review", "blocked", "proposed") if states[k]]
    print("No tasks ready to start." + (f" ({', '.join(hints)})" if hints else ""))
    if states["proposed"]:
        print("Ask the human to approve proposals in the board.")
    elif states["review"]:
        print("Ask the human to review finished tasks.")


def cmd_blocked(args):
    board = load_board(board_path(args))
    tasks = task_map(board)
    blocked = [t for t in board["tasks"] if view_state(t, tasks) == "blocked"]
    if not blocked:
        print("No blocked tasks.")
        return
    for t in blocked:
        print(f"{t['id']}  {t['title']}")
        for dep in unmet_deps(t, tasks):
            d = tasks.get(dep.upper())
            print(f"  waiting on {dep}  " + (f"{d['title']}  ({view_state(d, tasks)})" if d else "(missing)"))


def cmd_check(args):
    path = board_path(args)
    board = load_board(path)
    errors, warnings = problems(board)
    for e in errors:
        print(f"error: {e}")
    for w in warnings:
        print(f"warning: {w}")
    if not errors:
        print(f"OK: {len(board['tasks'])} tasks in {len(board['groups'])} groups ({rel(path)})")
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Agent write commands
# ---------------------------------------------------------------------------


def cmd_start(args):
    path = board_path(args)
    preview = load_board(path)
    preview_tasks = task_map(preview)
    preview_task = find_task(preview, args.id)
    preview_state = view_state(preview_task, preview_tasks)
    if preview_state == "blocked":
        raise KanvasError(f"{preview_task['id']} is blocked by unfinished {', '.join(unmet_deps(preview_task, preview_tasks))}")
    if preview_state != "todo":
        raise KanvasError(f"{preview_task['id']} cannot be started: {STATE_HINTS[preview_state]}")
    if not isinstance(preview_task.get("definition"), dict) or not preview_task["definition"].get("approved"):
        raise KanvasError(f"{preview_task['id']} has no approved product definition; complete the PO/TL grill in the board first")
    note = read_input(args.message, args.stdin, args.file, "technical plan")
    if not note:
        raise KanvasError(
            "start needs the architecture/senior construction plan after the technical grill, e.g. "
            f'`{command_line("start " + args.id)} -m "Plan: API, data, tests and rollout"`'
        )

    git = worktree_snapshots(args.worktree)

    def op(board):
        tasks = task_map(board)
        t = find_task(board, args.id)
        vs = view_state(t, tasks)
        if vs == "blocked":
            raise KanvasError(f"{t['id']} is blocked by unfinished {', '.join(unmet_deps(t, tasks))}")
        if vs != "todo":
            raise KanvasError(f"{t['id']} cannot be started: {STATE_HINTS[vs]}")
        if not isinstance(t.get("definition"), dict) or not t["definition"].get("approved"):
            raise KanvasError(f"{t['id']} has no approved product definition; complete the PO/TL grill in the board first")
        t["state"] = "doing"
        t["work"] = {"by": actor(args), "at": now_iso(), "plan": note}
        if t["definition"].get("repositories"):
            t["work"]["repositories"] = list(t["definition"]["repositories"])
        if git:
            t["work"]["git"] = git
        t.pop("review", None)
        add_note(t, note, actor(args))
        return board, t

    board, t = mutate(path, op)
    print(f"Started {t['id']} (todo -> doing)\n")
    if not git:
        print("Warning: no Git worktree was detected; use --worktree PATH for code work.\n")
    print_task(board, t)


def cmd_finish(args):
    path = board_path(args)
    summary = read_input(args.message, args.stdin, args.file, "summary")
    if not summary:
        raise KanvasError(
            "finish needs a summary of what you did, e.g. "
            f'`{command_line("finish " + args.id)} -m "Added X; tests pass"` (or pipe it with --stdin)'
        )

    def op(board):
        t = find_task(board, args.id)
        if t["state"] != "doing":
            raise KanvasError(f"{t['id']} is {view_state(t, task_map(board))}; only tasks in progress can be finished")
        add_note(t, summary, actor(args))
        t["state"] = "review"
        t["review"] = {
            "agent": {"status": "pending"},
            "manual": {"status": "pending"},
        }
        return t

    t = mutate(path, op)
    print(f"Finished {t['id']} (doing -> review). It now needs an independent agent review and manual review.")


def cmd_agent_review(args):
    path = board_path(args)
    summary = read_input(args.message, args.stdin, args.file, "review summary")
    if not summary:
        raise KanvasError("agent-review needs findings or an approval summary")

    def op(board):
        t = find_task(board, args.id)
        if t["state"] != "review":
            raise KanvasError(f"{t['id']} is {view_state(t, task_map(board))}; only tasks in review can be reviewed")
        reviewer = actor(args)
        implementer = (t.get("work") or {}).get("by")
        if args.approve and implementer and reviewer == implementer:
            raise KanvasError(f"{t['id']} was implemented by {implementer}; independent agent review needs another identity")
        status = "approved" if args.approve else "changes_requested"
        review = t.setdefault("review", {})
        review["agent"] = {"status": status, "by": reviewer, "at": now_iso(), "summary": summary}
        review.setdefault("manual", {"status": "pending"})
        add_note(t, f"Agent review {status.replace('_', ' ')}: {summary}", reviewer)
        if not args.approve:
            t["state"] = "doing"
            review["manual"] = {"status": "pending"}
        return t, status

    t, status = mutate(path, op)
    if status == "approved":
        print(f"Agent review approved for {t['id']}. It still needs manual review and merge evidence for dev.")
    else:
        print(f"Agent review requested changes on {t['id']} (review -> doing).")


def cmd_pause(args):
    path = board_path(args)
    note = read_input(args.message, args.stdin, args.file, "note")

    def op(board):
        t = find_task(board, args.id)
        if t["state"] != "doing":
            raise KanvasError(f"{t['id']} is {view_state(t, task_map(board))}; only tasks in progress can be paused")
        t["state"] = "todo"
        if note:
            add_note(t, note, actor(args))
        return t

    t = mutate(path, op)
    print(f"Paused {t['id']} (doing -> todo)")


def cmd_retry(args):
    path = board_path(args)

    def op(board):
        t = find_task(board, args.id)
        if t["state"] != "doing":
            raise KanvasError(f"{t['id']} is {view_state(t, task_map(board))}; only failed work in progress can be retried")
        work = t.get("work") if isinstance(t.get("work"), dict) else {}
        run = work.get("automation") if isinstance(work.get("automation"), dict) else {}
        if run.get("status") != "failed":
            raise KanvasError(f"{t['id']} automation is {run.get('status') or 'not started'}; only failed automation can be retried")
        kept = {k: run[k] for k in ("round", "since", "pull_requests") if k in run}
        # The human choosing to continue starts a new count of review rounds: without it the run would stop again at
        # automation.max_rounds as soon as the reviewer asks for one more change.
        if run.get("round"):
            kept["cycle_start"] = int(run["round"])
        if kept:
            work["automation"] = kept
        else:
            work.pop("automation", None)
        t["work"] = work
        add_note(t, "Retrying failed automation in its existing isolated worktree.", actor(args))
        return t

    t = mutate(path, op)
    print(f"Queued retry for {t['id']}; the Factory server will reuse its isolated worktree.")


def cmd_design(args):
    path = board_path(args)
    board = load_board(path)
    task = find_task(board, args.id)
    if args.approve:
        set_design(path, task["id"], True)
        print(f"Design approved for {task['id']}; the implementation starts")
    elif args.changes is not None or args.again:
        set_design(path, task["id"], False, (args.changes or "").strip())
        print(f"The designer will propose {task['id']} again")
    else:
        design = design_of(task)
        print(f"{task['id']}: design {design.get('status') or 'not made yet'}" + (f"\n\n{design['text']}" if design.get("text") else ""))


def cmd_stop(args):
    path = board_path(args)
    task = find_task(load_board(path), args.id)
    request_stop(path, task["id"])
    print(f"Asked the agent on {task['id']} to stop; it ends within a few seconds")


# ---------------------------------------------------------------------------
# `kanvas mcp`: the board and the documentation, as tools for any Claude session or agent that speaks MCP (the commercial
# agent that prices a client's request, a bot, an interactive session). It reads the board and the documentation folders
# and writes only what agents may write in Factory anyway: a proposed task and a note. Approving, starting, reviewing,
# stopping and merging stay with the human, so there is no tool for them.

MCP_PROTOCOL = "2025-06-18"
DOC_EXTENSIONS = (".md", ".html", ".txt", ".json")
DOC_SKIP = {"node_modules", "bin", "obj", "dist", "build", "__pycache__"}
DOC_MAX_BYTES = 60000
MCP_INSTRUCTIONS = ("The board of a project where AI agents work under a human's approval, and its documentation. Read the "
                    "board with kanvas_status, kanvas_list and kanvas_show, and what past runs cost with kanvas_usage. Look "
                    "things up in the documentation with brain_search and brain_read. You may propose a task or add a note; "
                    "a human approves, starts and reviews the work.")


def docs_roots(board, path):
    """The folders of documentation the tools may read, as [(label, folder)]: the `brain` repository, and the adr and docs
    folders of the other repositories."""
    config = automation_config(board, path)
    roots = []
    for repo_id, repo in (config["repositories"] if config else {}).items():
        if repo_id == CURATOR_REPOSITORY:
            roots.append((repo_id, repo["path"]))
            continue
        for sub_folder in ("adr", "docs"):
            folder = os.path.join(repo["path"], sub_folder)
            if os.path.isdir(folder):
                roots.append((f"{repo_id}/{sub_folder}", folder))
    return roots


def docs_files(roots):
    """Every readable document as (label path, real path), in a stable order."""
    found = []
    for label, root in roots:
        for here, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in DOC_SKIP)
            for name in sorted(names):
                full = os.path.join(here, name)
                if name.lower().endswith(DOC_EXTENSIONS) and not name.startswith(".") and os.path.isfile(full) \
                        and not os.path.islink(full):
                    found.append((f"{label}/{os.path.relpath(full, root)}".replace(os.sep, "/"), full))
    return sorted(found)


def docs_search(roots, query, limit=8):
    terms = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    if not terms:
        raise KanvasError("Give at least one word of three letters or more to search for")
    results = []
    for label_path, full in docs_files(roots):
        try:
            if os.path.getsize(full) > 500000:
                continue
            with open(full, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        lower = [line.lower() for line in lines]
        text = "\n".join(lower)
        score = sum(text.count(t) for t in terms) + 5 * sum(t in label_path.lower() for t in terms)
        if not score:
            continue
        best = max(range(len(lines)), key=lambda i: (sum(t in lower[i] for t in terms), -i)) if lines else 0
        snippet = "\n".join(lines[max(0, best - 1):best + 2]).strip()
        results.append((score, label_path, best + 1, snippet[:400]))
    results.sort(key=lambda r: (-r[0], r[1]))
    return [{"path": p, "line": n, "score": sc, "snippet": snip} for sc, p, n, snip in results[:limit]]


def docs_read(roots, wanted):
    wanted = (wanted or "").strip().replace("\\", "/")
    for label_path, full in docs_files(roots):
        if label_path == wanted:
            if os.path.getsize(full) > DOC_MAX_BYTES:
                with open(full, encoding="utf-8", errors="replace") as f:
                    return f.read(DOC_MAX_BYTES) + f"\n... (cut at {DOC_MAX_BYTES} characters)"
            with open(full, encoding="utf-8", errors="replace") as f:
                return f.read()
    raise KanvasError(f"No document called {wanted}. brain_list shows the paths, brain_search finds one by its words")


def task_brief(t, tasks):
    return {"id": t["id"], "title": t["title"], "state": view_state(t, tasks), "group": t.get("group"),
            "depends_on": t.get("deps") or [], "proposed_by": t.get("by"), "notes": len(t.get("notes") or [])}


def task_detail(t, tasks):
    work = t.get("work") if isinstance(t.get("work"), dict) else {}
    run = work.get("automation") if isinstance(work.get("automation"), dict) else {}
    detail = {**task_brief(t, tasks), "description": _clip(t.get("desc"), 3000)}
    if isinstance(t.get("definition"), dict):
        detail["definition"] = {k: v for k, v in t["definition"].items() if k in
                                ("outcome", "needs", "acceptance", "non_goals", "priority", "repositories", "approved")}
    if work.get("plan"):
        detail["plan"] = _clip(work["plan"], 3000)
    if run:
        detail["run"] = {k: run.get(k) for k in ("status", "round", "agent", "summary") if run.get(k) is not None}
        detail["run"]["summary"] = _clip(str(detail["run"].get("summary") or ""), 1500)
        detail["pull_requests"] = [{"name": x.get("name"), "url": x.get("url")} for x in run.get("pull_requests") or []]
    if design_of(t).get("status"):
        detail["design"] = {"status": design_of(t)["status"], "round": design_of(t).get("round")}
    if isinstance(t.get("review"), dict):
        detail["review"] = {gate: (t["review"].get(gate) or {}).get("status") for gate in ("agent", "manual")}
    detail["recent_notes"] = [{"by": n.get("by"), "at": n.get("at"), "text": _clip(n.get("text"), 800)}
                              for n in (t.get("notes") or [])[-8:]]
    return detail


MCP_TOOLS = [
    {"name": "kanvas_status", "description": "Overview of the board: how many tasks are in each state, and what is in progress, in review or waiting for the human.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "kanvas_list", "description": "List tasks, optionally only those in a state (proposed, todo, blocked, doing, review, done, production) or in a group.",
     "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "A state or a group name or id"}}}},
    {"name": "kanvas_show", "description": "Everything about one task: description, product definition, plan, run, pull requests, review gates and recent notes.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "kanvas_usage", "description": "What the automated runs cost: turns, dollars and tokens per task, agent role and model. Useful to estimate what similar work costs.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "brain_search", "description": "Search the project documentation (the brain: modules, decisions, operations, and the ADRs and docs of each repository) by words. Returns the best matches with a snippet.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["query"]}},
    {"name": "brain_read", "description": "Read one document of the documentation by the path brain_search or brain_list gave.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "brain_list", "description": "List the documents of the documentation.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "kanvas_propose", "description": "Propose a task for the human to approve. Nothing starts until they do.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                                                        "group": {"type": "string", "description": "Group name or id; omit for none"},
                                                        "depends_on": {"type": "array", "items": {"type": "string"}}}, "required": ["title"]}},
    {"name": "kanvas_note", "description": "Add a note to a task that is not done yet.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}}, "required": ["id", "text"]}},
]
MCP_WRITE_TOOLS = ("kanvas_propose", "kanvas_note")


def mcp_call(path, name, arguments, read_only=False):
    """Runs one tool and returns its answer as text. A KanvasError is the tool's own refusal."""
    args = arguments if isinstance(arguments, dict) else {}
    if name in MCP_WRITE_TOOLS and read_only:
        raise KanvasError("This server was started with --read-only")
    board = load_board(path)
    tasks = task_map(board)
    if name == "kanvas_status":
        by_state = {}
        for t in board["tasks"]:
            by_state.setdefault(view_state(t, tasks), []).append(task_brief(t, tasks))
        return json.dumps({"title": board.get("title"), "total": len(board["tasks"]),
                           "counts": {k: len(v) for k, v in by_state.items()},
                           "doing": by_state.get("doing", []), "review": by_state.get("review", []),
                           "proposed": by_state.get("proposed", [])}, ensure_ascii=False, indent=1)
    if name == "kanvas_list":
        flt = str(args.get("filter") or "").strip().lower()
        rows = board["tasks"]
        if flt:
            state = "todo" if flt == "ready" else flt
            if state in STATES or state == "blocked":
                rows = [t for t in rows if view_state(t, tasks) == state]
            else:
                group = find_group(board, flt)
                rows = [t for t in rows if t.get("group") == group["id"]]
        return json.dumps([task_brief(t, tasks) for t in rows[:80]], ensure_ascii=False, indent=1)
    if name == "kanvas_show":
        return json.dumps(task_detail(find_task(board, str(args.get("id") or "")), tasks), ensure_ascii=False, indent=1)
    if name == "kanvas_usage":
        config = automation_config(board, path)
        if config is None:
            raise KanvasError("Automation is not enabled for this board, so there are no runs to measure")
        rows = usage_runs(board, config)
        total = sum(r["cost"] for r in rows)
        return json.dumps({"runs": len(rows), "total_usd": round(total, 2),
                           "average_usd": round(total / len(rows), 2) if rows else 0, "rows": rows}, ensure_ascii=False, indent=1)
    if name in ("brain_search", "brain_read", "brain_list"):
        roots = docs_roots(board, path)
        if not roots:
            raise KanvasError("This board has no documentation folders: configure a `brain` repository under automation")
        if name == "brain_search":
            limit = args.get("limit") if isinstance(args.get("limit"), int) and not isinstance(args.get("limit"), bool) else 8
            found = docs_search(roots, str(args.get("query") or ""), max(1, min(20, limit)))
            return json.dumps(found, ensure_ascii=False, indent=1) if found else "No document matches those words."
        if name == "brain_read":
            return docs_read(roots, args.get("path"))
        return "\n".join(label_path for label_path, _ in docs_files(roots)[:400])
    actor_name = os.environ.get("KANVAS_AGENT") or "mcp"
    if name == "kanvas_propose":
        deps = [str(d) for d in args.get("depends_on") or []] if isinstance(args.get("depends_on"), list) else []
        group_ref = str(args.get("group") or "").strip()

        def propose(fresh):
            group = find_group(fresh, group_ref) if group_ref and group_ref != "-" else None
            return add_task(fresh, args.get("title"), group, str(args.get("description") or ""), deps, by=actor_name)

        t = mutate(path, propose)
        return f"Proposed {t['id']}: {t['title']}. A human must approve it before any work starts."
    if name == "kanvas_note":
        text = str(args.get("text") or "").strip()

        def note(fresh):
            t = find_task(fresh, str(args.get("id") or ""))
            if t["state"] in ("develop", "done", "production"):
                raise KanvasError(f"{t['id']} is {t['state']}; propose a follow-up task instead")
            add_note(t, text, actor_name)
            return t

        t = mutate(path, note)
        return f"Added a note to {t['id']}."
    raise KanvasError(f"There is no tool called {name}")


def mcp_handle(path, request, read_only=False):
    """The JSON-RPC response to one MCP request, or None for a notification."""
    method, ident = request.get("method"), request.get("id")
    if ident is None:
        return None

    def reply(result=None, error=None):
        message = {"jsonrpc": "2.0", "id": ident}
        message.update({"error": error} if error else {"result": result})
        return message

    if method == "initialize":
        asked = (request.get("params") or {}).get("protocolVersion")
        return reply({"protocolVersion": asked if isinstance(asked, str) and asked else MCP_PROTOCOL,
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "kanvas", "version": VERSION},
                      "instructions": MCP_INSTRUCTIONS})
    if method == "ping":
        return reply({})
    if method == "tools/list":
        return reply({"tools": [t for t in MCP_TOOLS if not (read_only and t["name"] in MCP_WRITE_TOOLS)]})
    if method == "tools/call":
        params = request.get("params") or {}
        try:
            text, failed = mcp_call(path, str(params.get("name") or ""), params.get("arguments"), read_only), False
        except KanvasError as e:
            text, failed = str(e), True
        except Exception as e:
            text, failed = f"The tool failed: {e}", True
        return reply({"content": [{"type": "text", "text": text}], "isError": failed})
    return reply(error={"code": -32601, "message": f"Method not found: {method}"})


def cmd_mcp(args):
    path = board_path(args)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw.decode("utf-8"))
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            response = mcp_handle(path, request, args.read_only) if isinstance(request, dict) else None
        if response is not None:
            stdout.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()


# ---------------------------------------------------------------------------
# The inbox: the owner sends a raw idea or an incident someone reported, from the phone (Telegram or the inbox page) or the
# terminal, and the product owner turns it into a task: it reads the documentation and the board, names the task, drafts
# the product definition and asks only what it cannot infer. The owner answers, and approves the definition with one tap.
# What a remote channel may do stops there: starting the work (plan, design, running agents, which cost tokens) stays with
# the board.

INBOX_KINDS = ("idea", "bug", "client", "question")
INBOX_STALE_MINUTES = 30
OWNER_NAMES = ("owner", "human")


def inbox_of(task):
    return task.get("inbox") if isinstance(task.get("inbox"), dict) else {}


def inbox_tasks(board, open_only=True):
    """The tasks that came through the inbox and still wait for the owner's approval of their definition."""
    return [t for t in board["tasks"] if inbox_of(t) and (not open_only or (t.get("state") == "proposed" and inbox_of(t).get("status") != "approved"))]


def inbox_title(text):
    first = re.split(r"[\n.!?]", " ".join(str(text).split()), maxsplit=1)[0].strip() or "New idea"
    return first[:80]


def inbox_submit(path, text, source="cli", task_id=None, attachments=()):
    """Records what the owner wrote: a new proposed task, or one more message in the conversation of an existing one.
    Returns the task's id."""
    text = (text or "").strip()
    if not text and not attachments:
        raise KanvasError("There is nothing to send")
    shown = text
    for kept in attachments:
        shown += f"\n[attachment: {kept}]"

    def op(board):
        if task_id:
            t = find_task(board, task_id)
            if t.get("state") != "proposed" or not inbox_of(t):
                raise KanvasError(f"{t['id']} is not an idea waiting in the inbox")
        else:
            t = add_task(board, inbox_title(text), None, text, state="proposed", by=f"owner via {source}")
            t["inbox"] = {"source": source, "status": "shaping", "created": now_iso()}
        add_note(t, shown.strip(), "owner")
        inbox_of(t)["updated"] = now_iso()
        inbox_of(t)["status"] = "shaping"
        return t["id"]
    return mutate(path, op)


def inbox_thread(task):
    """The conversation so far, from the owner's messages and the product owner's replies."""
    lines = []
    for note in task.get("notes") or []:
        by = note.get("by")
        if by in OWNER_NAMES:
            lines.append(f"[owner] {_clip(note.get('text'), 2000)}")
        elif by == "product-owner":
            lines.append(f"[product owner] {_clip(note.get('text'), 1500)}")
    return "\n".join(lines[-24:])


def inbox_prompt(board, task, config):
    groups = "\n".join(f"- {g['id']}: {g['name']}" for g in board["groups"]) or "(none)"
    open_tasks = "\n".join(f"- {t['id']} [{t['state']}] {t['title']}" for t in board["tasks"]
                           if t["state"] not in ("develop", "done", "production") and t["id"] != task["id"])[:6000] or "(none)"
    repos = ", ".join(f"{k}: {v['name']}" for k, v in config["repositories"].items() if k != CURATOR_REPOSITORY) or "none"
    roles = ", ".join(r for r in config["agents"] if r not in ("reviewer", "reviewer-strong", "curator", "designer", "po"))
    brain = config["repositories"].get(CURATOR_REPOSITORY, {}).get("path")
    docs = f"The documentation is in {brain} (modules, adr, ops)." if brain else "Read the documentation you find in the project."
    return f"""You are the product owner of the Factory board "{board['title']}". The owner sends you, from their phone, a raw idea or an incident that someone reported. Turn it into a well-formed task with as little work for the owner as possible: infer everything you can from the project, and ask only what you cannot infer, at most 3 questions.

The project is in {config['root']}. {docs} Read what you need, change nothing. If the owner mentions an attachment, open the file with Read.

Groups:
{groups}

Other open tasks (to spot duplicates):
{open_tasks}

Repositories (id: name): {repos}
Agent roles: {roles}. The implementer is the default; choose complex only for security, multi-club isolation, permissions or data migrations.

Conversation so far:
{inbox_thread(task)}

Decide what it is: "idea", "bug" (something that works wrong for a user), "client" (a request from a club) or "question" (the owner only wants an answer; then the draft stays empty).
- A bug: write the steps to reproduce, who is affected, how bad it is (low, medium, high or blocker), the module and repository that most likely hold it (look at the documentation and the code) and a workaround if there is one.
- A client request: say whether it exists already or what it would touch.
- If an open task already covers it, list it in "duplicates".

Write "reply" for the owner's phone, in the language they write in, in at most 6 short lines: what you understood, what you decided (title, repository, agent) and your questions if any. Set "ready" to true only when the draft is complete enough to approve without more answers.

End your reply with one fenced json block and nothing after it:
```json
{{"kind": "idea", "title": "...", "group": "<group id or empty>", "desc": "<2 to 6 lines of markdown>", "duplicates": ["<task id>"], "reply": "...", "questions": ["..."], "ready": false, "draft": {{"outcome": "...", "needs": "...", "acceptance": "...", "non_goals": "...", "repositories": ["<repository id>"], "agent": "implementer"}}}}
```
"""


def clean_inbox(data, board, config):
    """The product owner's JSON checked, or None. Unknown groups, tasks and repositories are dropped."""
    if not isinstance(data, dict) or data.get("kind") not in INBOX_KINDS:
        return None
    groups = {g["id"] for g in board["groups"]}
    known = {t["id"] for t in board["tasks"]}
    draft = data.get("draft") if isinstance(data.get("draft"), dict) else {}
    repos = [r for r in draft.get("repositories") or [] if isinstance(r, str) and r in config["repositories"] and r != CURATOR_REPOSITORY]
    agent = draft.get("agent") if draft.get("agent") in config["agents"] and draft.get("agent") not in (
        "reviewer", "reviewer-strong", "curator", "designer", "po") else DEFAULT_AGENT
    cleaned = {"outcome": _clip(draft.get("outcome"), 1500), "needs": _clip(draft.get("needs"), 2500),
               "acceptance": _clip(draft.get("acceptance"), 2500), "non_goals": _clip(draft.get("non_goals"), 1500),
               "repositories": repos, "agent": agent}
    complete = bool(cleaned["outcome"] and cleaned["acceptance"] and repos)
    return {"kind": data["kind"], "title": _clip(data.get("title"), 120), "desc": _clip(data.get("desc"), 3000),
            "group": data.get("group") if data.get("group") in groups else None,
            "duplicates": [d for d in data.get("duplicates") or [] if d in known][:5],
            "reply": _clip(data.get("reply"), 1200), "questions": [_clip(q, 300) for q in data.get("questions") or [] if isinstance(q, str)][:3],
            "ready": bool(data.get("ready")) and complete and data["kind"] != "question", "draft": cleaned}


def inbox_po_turn(path, task_id):
    """One turn of the product owner over an idea in the inbox: it updates the task and its draft definition and returns
    {"reply", "ready", "kind", "questions", "duplicates"}. A turn that fails leaves a note and raises."""
    board = load_board(path)
    config = automation_config(board, path)
    if config is None:
        raise KanvasError("Automation is not enabled for this board, so the product owner has no way to run")
    task = find_task(board, task_id)
    role = config["agents"].get("po")
    if role is None:
        raise KanvasError("The board does not define the agent role po")
    if not shutil.which("claude"):
        raise KanvasError("Claude Code (claude) is not installed or not on PATH; the product owner needs it")
    os.makedirs(config["runs"], exist_ok=True)
    log = os.path.join(config["runs"], f"{automation_slug(task_id)}-po.jsonl")
    prompt = inbox_prompt(board, task, config)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    mark_busy(path, task_id, "product-owner")
    clear_stop(path, task_id)
    try:
        with open(log, "w", encoding="utf-8", newline="\n") as output:
            proc = agent_run(claude_command(config, [{"worktree": config["root"]}], role), input=prompt, text=True,
                             encoding="utf-8", errors="replace", stdout=output, stderr=subprocess.STDOUT, creationflags=flags,
                             cwd=config["root"], env=agent_environment(), limits=agent_limits(config, role),
                             stop=lambda: stop_requested(path, task_id))
        clear_stop(path, task_id)
        if stop_text(proc):
            raise KanvasError(f"the product owner did not finish. {stop_text(proc)}")
        result = read_result(log)
        if result is None or proc.returncode or result.get("is_error"):
            detail = str((result or {}).get("result") or f"it exited with code {proc.returncode}. See {log}")
            raise KanvasError(f"the product owner could not answer: {_clip(detail, 300)}")
        prose, data = split_assist_reply(str(result.get("result") or ""))
        outcome = clean_inbox(data, load_board(path), config)
        if outcome is None:
            raise KanvasError("the product owner did not return a task")
    except Exception as e:
        def fail(fresh):
            t = find_task(fresh, task_id)
            add_note(t, f"The product owner could not shape this: {e}", "kanvas-runner")
        try:
            mutate(path, fail)
        except (KanvasError, OSError):
            pass
        raise
    finally:
        mark_busy(path, task_id, None)
    reply = outcome["reply"] or prose or "Done."

    def record(fresh):
        t = find_task(fresh, task_id)
        if t.get("state") != "proposed":
            return
        info = t.setdefault("inbox", {})
        if outcome["title"]:
            t["title"] = outcome["title"]
        if outcome["desc"]:
            t["desc"] = outcome["desc"]
        if outcome["group"]:
            t["group"] = outcome["group"]
        info.update(kind=outcome["kind"], draft=outcome["draft"], ready=outcome["ready"], questions=outcome["questions"],
                    duplicates=outcome["duplicates"], status="ready" if outcome["ready"] else "shaping", updated=now_iso(),
                    turns=int(info.get("turns") or 0) + 1)
        add_note(t, reply, "product-owner")
    mutate(path, record)
    return {"reply": reply, "ready": outcome["ready"], "kind": outcome["kind"], "questions": outcome["questions"],
            "duplicates": outcome["duplicates"], "task": task_id}


def inbox_send(path, text, source="cli", task_id=None, attachments=()):
    """The whole round: record the message, let the product owner answer, and return its answer."""
    task_id = inbox_submit(path, text, source, task_id, attachments)
    return inbox_po_turn(path, task_id)


def inbox_approve(path, task_id, via="the inbox"):
    """The owner approves the draft definition: the task becomes To do, ready for its construction plan."""
    def op(board):
        t = find_task(board, task_id)
        info = inbox_of(t)
        if t.get("state") != "proposed" or not info:
            raise KanvasError(f"{t['id']} is not an idea waiting for approval in the inbox")
        draft = info.get("draft") if isinstance(info.get("draft"), dict) else {}
        missing = [k for k, label in (("outcome", "an outcome"), ("acceptance", "acceptance criteria"), ("repositories", "a repository")) if not draft.get(k)]
        if missing:
            raise KanvasError(f"The draft of {t['id']} still lacks {', '.join(missing)}: answer the product owner's questions first")
        t["definition"] = {**draft, "approved": True, "by": "human", "at": now_iso(), "via": via}
        t["state"] = "todo"
        info["status"] = "approved"
        add_note(t, f"PO/TL product definition approved from {via}.", "owner")
        return t
    return mutate(path, op)


def inbox_drop(path, task_id):
    """The owner throws the idea away: the proposed task is removed."""
    def op(board):
        t = find_task(board, task_id)
        if t.get("state") != "proposed" or not inbox_of(t):
            raise KanvasError(f"{t['id']} is not an idea waiting in the inbox")
        board["tasks"].remove(t)
        for other in board["tasks"]:
            other["deps"] = [d for d in other.get("deps") or [] if d != t["id"]]
        return t["id"]
    return mutate(path, op)


def inbox_summary(task):
    info = inbox_of(task)
    thread = [{"by": n.get("by"), "at": n.get("at"), "text": n.get("text")} for n in task.get("notes") or []
              if n.get("by") in OWNER_NAMES + ("product-owner",)]
    return {"id": task["id"], "title": task["title"], "status": info.get("status"), "kind": info.get("kind"),
            "ready": bool(info.get("ready")), "questions": info.get("questions") or [], "duplicates": info.get("duplicates") or [],
            "draft": info.get("draft") or {}, "thread": thread[-12:]}


def inbox_token_file():
    return os.path.join(os.path.dirname(state_file()), "inbox-token.json")


def new_inbox_token():
    """A new pairing token for phones. Only its hash is kept, so the token is shown once, here."""
    token = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    os.makedirs(os.path.dirname(inbox_token_file()), exist_ok=True)
    write_text_atomic(inbox_token_file(), json.dumps({"hash": hashlib.sha256(token.encode("utf-8")).hexdigest(), "at": now_iso()}) + "\n")
    return token


def inbox_token_ok(token):
    if not token:
        return False
    try:
        with open(inbox_token_file(), encoding="utf-8") as f:
            kept = json.load(f).get("hash")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(kept, str) and hmac.compare_digest(kept, hashlib.sha256(token.encode("utf-8")).hexdigest())


def phone_bases():
    """The addresses a phone on Tailscale uses to reach this computer's Factory, or none without Tailscale."""
    try:
        ip, name, magic = tailscale_self()
    except KanvasError:
        return []
    return [f"http://{h}:{DEFAULT_PORT}/" for h in ([name] if magic else []) + [ip]]


INBOX_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Inbox</title>
<style>
:root { color-scheme: light dark; font: 16px/1.45 system-ui, sans-serif; --line: #8884; --brand: #4f46e5; }
* { box-sizing: border-box; } body { margin: 0; display: flex; flex-direction: column; height: 100dvh; }
header { padding: 10px 14px; border-bottom: 1px solid var(--line); display: flex; gap: 10px; align-items: center; }
header b { flex: 1; } button { font: inherit; border: 1px solid var(--line); background: transparent; color: inherit; border-radius: 10px; padding: 8px 12px; }
button.primary { background: var(--brand); color: #fff; border-color: var(--brand); } button:disabled { opacity: .45; }
main { flex: 1; overflow: auto; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
.card { border: 1px solid var(--line); border-radius: 12px; padding: 10px 12px; cursor: pointer; }
.card small { opacity: .65; display: block; } .chip { font-size: 12px; border: 1px solid var(--line); border-radius: 999px; padding: 1px 8px; margin-left: 6px; }
.msg { max-width: 88%; padding: 8px 12px; border-radius: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
.msg.owner { align-self: flex-end; background: var(--brand); color: #fff; } .msg.po { align-self: flex-start; border: 1px solid var(--line); }
.draft { border: 1px dashed var(--line); border-radius: 12px; padding: 10px 12px; font-size: 14px; } .draft b { display: block; }
.actions { display: flex; gap: 8px; } .actions button { flex: 1; }
form { display: flex; gap: 8px; padding: 10px 14px calc(10px + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); }
textarea { flex: 1; font: inherit; border: 1px solid var(--line); border-radius: 12px; padding: 8px 12px; background: transparent; color: inherit; resize: none; }
.err { color: #dc2626; } .muted { opacity: .65; }
</style></head><body>
<header><button id="back" hidden>&larr;</button><b id="title">Inbox</b><button id="new">New idea</button></header>
<main id="main"></main>
<form id="form"><textarea id="text" rows="2" placeholder="An idea, or an incident someone reported&hellip;"></textarea><button class="primary" id="send">Send</button></form>
<script>
const base = location.pathname.replace(/inbox\/?$/, "") + "api/inbox";
const saved = new URLSearchParams(location.hash.slice(1)).get("token");
if (saved) { localStorage.setItem("kanvas-inbox-token", saved); history.replaceState(null, "", location.pathname); }
const token = localStorage.getItem("kanvas-inbox-token") || "";
const $ = (id) => document.getElementById(id);
let items = [], current = null, busy = false;
async function api(path, body) {
  const r = await fetch(base + path, { method: body ? "POST" : "GET", cache: "no-store",
    headers: { "Content-Type": "application/json", "X-Kanvas-Token": token }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function show() {
  const main = $("main"); main.replaceChildren(); $("back").hidden = !current; $("new").hidden = !!current === false && false;
  const task = items.find((t) => t.id === current);
  $("title").textContent = task ? `${task.id} · ${task.title}` : "Inbox";
  if (!task) {
    if (!items.length) main.append(el("p", "muted", "Nothing waits here. Send an idea or an incident below."));
    for (const t of items) {
      const card = el("div", "card"); const top = el("div", "", t.title);
      top.append(el("span", "chip", t.ready ? "ready" : t.status || "new"), el("span", "chip", t.kind || "..."));
      card.append(top, el("small", "", t.questions[0] || t.id)); card.onclick = () => { current = t.id; show(); }; main.append(card);
    }
    return;
  }
  for (const m of task.thread) main.append(el("div", "msg " + (m.by === "product-owner" ? "po" : "owner"), m.text));
  if (busy) main.append(el("p", "muted", "The product owner is thinking…"));
  const d = task.draft || {};
  if (d.outcome) {
    const box = el("div", "draft"); for (const [k, label] of [["outcome", "Outcome"], ["acceptance", "Acceptance"], ["repositories", "Repositories"], ["agent", "Agent"]]) {
      const v = Array.isArray(d[k]) ? d[k].join(", ") : d[k]; if (v) { box.append(el("b", "", label), el("span", "", v)); } }
    main.append(box);
  }
  if (task.duplicates.length) main.append(el("p", "muted", "Looks like: " + task.duplicates.join(", ")));
  const bar = el("div", "actions"); const ok = el("button", "primary", "Approve definition"); ok.disabled = !task.ready || busy;
  ok.onclick = () => act(task.id, "approve"); const drop = el("button", "", "Drop"); drop.disabled = busy; drop.onclick = () => act(task.id, "drop");
  bar.append(ok, drop); main.append(bar); main.scrollTop = main.scrollHeight;
}
async function load() { try { items = (await api("")).items; } catch (e) { $("main").replaceChildren(el("p", "err", e.message)); return; } show(); }
async function act(id, what) {
  try { await api(`/${id}/${what}`, {}); if (what === "drop" || what === "approve") { current = null; } await load(); alert(what === "approve" ? `${id} is To do` : "Dropped"); }
  catch (e) { alert(e.message); }
}
async function send(text) {
  busy = true; const start = items.find((t) => t.id === current);
  if (start) start.thread.push({ by: "owner", text }); show();
  try {
    const job = await api("", { text, task: current || undefined, source: "web" });
    current = job.task;
    for (;;) { const s = await api(`/job/${job.job}`); if (s.status !== "running") { if (s.error) throw new Error(s.error); break; } await new Promise((r) => setTimeout(r, 1500)); }
  } catch (e) { alert(e.message); }
  busy = false; await load();
}
$("form").onsubmit = (e) => { e.preventDefault(); const text = $("text").value.trim(); if (!text || busy) return; $("text").value = ""; send(text); };
$("back").onclick = () => { current = null; show(); }; $("new").onclick = () => { current = null; show(); $("text").focus(); };
load();
</script></body></html>"""


# ---------------------------------------------------------------------------
# Telegram: a private bot for the owner. Anything the owner writes is an idea or an incident for the inbox, a reply goes
# to the same idea while the product owner is still shaping it, and two buttons approve the definition or drop it. Only the
# Telegram users named in KANVAS_TELEGRAM_USERS are heard; everyone else is ignored without a word. The bot's token comes
# from KANVAS_TELEGRAM_TOKEN, which the agents never inherit.

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX_FILE = 8 * 1024 * 1024
TELEGRAM_HELP = ("Mandame una idea o una incidencia tal cual y la convierto en tarea con el PO.\n"
                 "Si te pregunta algo, contesta aqui mismo. Cuando este lista, pulsa Aprobar.\n\n"
                 "/new empieza una idea nueva aunque haya una a medias\n/list ideas pendientes\n"
                 "/approve ID   /drop ID\n/help esta ayuda")


class TelegramError(KanvasError):
    pass


PAIRING_MINUTES = 10


def telegram_file():
    return os.path.join(os.path.dirname(state_file()), "telegram.json")


def telegram_config():
    """The bot's connection: what `Connect Telegram` saved, or KANVAS_TELEGRAM_TOKEN and KANVAS_TELEGRAM_USERS. {} when
    there is none."""
    token = os.environ.get("KANVAS_TELEGRAM_TOKEN", "").strip()
    users = [int(u) for u in os.environ.get("KANVAS_TELEGRAM_USERS", "").replace(" ", "").split(",") if u.isdigit()]
    if token and users:
        return {"token": token, "users": users, "board": None, "bot": None, "pairing": None}
    try:
        with open(telegram_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("token"), str):
        return {}
    data["users"] = [u for u in data.get("users") or [] if isinstance(u, int)]
    return data


def save_telegram_config(config):
    os.makedirs(os.path.dirname(telegram_file()), exist_ok=True)
    write_text_atomic(telegram_file(), json.dumps(config, indent=1) + "\n")
    try:
        os.chmod(telegram_file(), 0o600)
    except OSError:
        pass


def new_pairing():
    """A code to send to the bot from the Telegram account that should be heard. It works once, for ten minutes."""
    return {"code": f"{secrets.randbelow(1000000):06d}", "expires": time.time() + PAIRING_MINUTES * 60}


def telegram_api(token):
    """A function (method, params) -> result over the Telegram Bot API. The token never appears in an error."""
    base = os.environ.get("KANVAS_TELEGRAM_API") or TELEGRAM_API

    def call(method, params=None, timeout=40):
        request = urllib.request.Request(f"{base}/bot{token}/{method}", data=json.dumps(params or {}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TelegramError(str(e).replace(token, "<token>"))
        if not data.get("ok"):
            raise TelegramError(str(data.get("description") or "Telegram refused the request").replace(token, "<token>"))
        return data["result"]

    def download(file_path):
        with urllib.request.urlopen(f"{base}/file/bot{token}/{file_path}", timeout=60) as response:
            return response.read(TELEGRAM_MAX_FILE + 1)

    call.download = download
    return call


class TelegramBridge:
    def __init__(self, path, api, users, say=print, pairing=None, on_pair=None):
        self.path, self.api, self.users, self.say = path, api, set(users), say
        self.pairing, self.on_pair = pairing, on_pair
        self.offset = 0
        self.ignored = 0
        self.stopping = False

    def try_pair(self, message):
        """The owner sends the code Factory showed on the board: from then on this Telegram account is heard."""
        chat, sender = message.get("chat") or {}, message.get("from") or {}
        code = str(message.get("text") or "").strip()
        pairing = self.pairing
        if (not pairing or time.time() > pairing["expires"] or chat.get("type") != "private" or not sender.get("id")
                or sender.get("id") != chat.get("id") or not hmac.compare_digest(code, pairing["code"])):
            return False
        self.users.add(sender["id"])
        self.pairing = None
        if self.on_pair:
            self.on_pair(sender["id"])
        self.reply(chat["id"], "✅ Conectado. Desde ahora te escucho solo a ti. Mandame una idea o una incidencia.\n\n" + TELEGRAM_HELP)
        return True

    def reply(self, chat, text, buttons=None):
        params = {"chat_id": chat, "text": text[:3900]}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}
        try:
            self.api("sendMessage", params)
        except TelegramError as e:
            self.say(f"Could not answer in Telegram: {e}")

    def buttons(self, task_id, ready):
        return ([("✅ Aprobar", f"ap:{task_id}")] if ready else []) + [("🗑 Descartar", f"dr:{task_id}")]

    def waiting_thread(self):
        """The idea the owner is probably still talking about: the last one from Telegram that the product owner has
        questions on, or has not finished, touched in the last half hour."""
        best = None
        for t in inbox_tasks(load_board(self.path)):
            info = inbox_of(t)
            if info.get("source") != "telegram" or info.get("ready"):
                continue
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(info.get("updated")).replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                continue
            if age <= INBOX_STALE_MINUTES * 60 and (best is None or age < best[0]):
                best = (age, t["id"])
        return best[1] if best else None

    def save_photo(self, message):
        """The largest photo of a message, kept in the inbox folder; returns its path or None."""
        photos = message.get("photo") or []
        if not photos:
            return None
        info = self.api("getFile", {"file_id": photos[-1]["file_id"]})
        data = self.api.download(info["file_path"])
        if len(data) > TELEGRAM_MAX_FILE:
            raise TelegramError("That photo is too big")
        board = load_board(self.path)
        config = automation_config(board, self.path)
        root = config["root"] if config else os.path.dirname(self.path)
        folder = os.path.join(root, ".kanvas", "inbox")
        os.makedirs(folder, exist_ok=True)
        kept = os.path.join(folder, f"{int(time.time())}-{os.urandom(3).hex()}.jpg")
        with open(kept, "wb") as f:
            f.write(data)
        return kept

    def idea(self, chat, text, new=False, attachments=()):
        task_id = None if new else self.waiting_thread()
        try:
            self.api("sendChatAction", {"chat_id": chat, "action": "typing"})
        except TelegramError:
            pass
        try:
            result = inbox_send(self.path, text, "telegram", task_id, attachments)
        except KanvasError as e:
            return self.reply(chat, f"No pude con eso: {e}")
        suffix = ""
        if result["duplicates"]:
            suffix = f"\n(Parecida a {', '.join(result['duplicates'])})"
        self.reply(chat, f"{result['task']} · {result['kind']}\n{result['reply']}{suffix}", self.buttons(result["task"], result["ready"]))

    def command(self, chat, text):
        word, _, rest = text.partition(" ")
        word, rest = word.split("@")[0].lower(), rest.strip()
        if word in ("/start", "/help"):
            return self.reply(chat, TELEGRAM_HELP)
        if word == "/list":
            waiting = inbox_tasks(load_board(self.path))
            return self.reply(chat, "\n".join(f"{t['id']} · {inbox_of(t).get('status')} · {t['title']}" for t in waiting) or "No hay ideas pendientes.")
        if word in ("/approve", "/drop"):
            try:
                if word == "/approve":
                    inbox_approve(self.path, rest.upper(), "Telegram")
                    return self.reply(chat, f"✅ {rest.upper()} aprobado: pasa a To do. Falta su plan en el tablero.")
                return self.reply(chat, f"🗑 {inbox_drop(self.path, rest.upper())} descartado.")
            except KanvasError as e:
                return self.reply(chat, str(e))
        if word == "/new":
            return self.idea(chat, rest, new=True) if rest else self.reply(chat, "Escribe la idea despues de /new, o simplemente mandamela.")
        self.reply(chat, "No conozco ese comando. /help")

    def button(self, query):
        chat = query["message"]["chat"]["id"]
        action, _, task_id = str(query.get("data") or "").partition(":")
        try:
            if action == "ap":
                inbox_approve(self.path, task_id, "Telegram")
                answer = f"✅ {task_id} aprobado: pasa a To do. Falta su plan en el tablero."
            elif action == "dr":
                answer = f"🗑 {inbox_drop(self.path, task_id)} descartado."
            else:
                answer = "No entiendo ese boton."
        except KanvasError as e:
            answer = str(e)
        try:
            self.api("answerCallbackQuery", {"callback_query_id": query["id"]})
        except TelegramError:
            pass
        self.reply(chat, answer)

    def handle(self, update):
        """One update from Telegram. Only the owner's own private chat is heard."""
        query = update.get("callback_query")
        message = update.get("message")
        if message and self.try_pair(message):
            return
        sender = (query or message or {}).get("from") or {}
        chat = ((query or {}).get("message") or message or {}).get("chat") or {}
        if sender.get("id") not in self.users or chat.get("type") != "private" or sender.get("id") != chat.get("id"):
            self.ignored += 1
            return
        if query:
            return self.button(query)
        text = str(message.get("text") or message.get("caption") or "").strip()
        attachments = []
        try:
            photo = self.save_photo(message)
        except (TelegramError, KeyError, OSError) as e:
            return self.reply(chat["id"], f"No pude guardar la foto: {e}")
        if photo:
            attachments.append(photo)
        if text.startswith("/") and not attachments:
            return self.command(chat["id"], text)
        if not text and not attachments:
            return self.reply(chat["id"], "Solo entiendo texto y fotos. /help")
        self.idea(chat["id"], text, attachments=attachments)

    def run(self, once=False):
        """Listens until stopped. A dropped connection is retried."""
        while not self.stopping:
            try:
                updates = self.api("getUpdates", {"offset": self.offset, "timeout": 0 if once else 25,
                                                  "allowed_updates": ["message", "callback_query"]}, timeout=35)
            except TelegramError as e:
                self.say(f"Telegram: {e}")
                if once:
                    return
                time.sleep(5)
                continue
            for update in updates:
                self.offset = max(self.offset, int(update.get("update_id") or 0) + 1)
                try:
                    self.handle(update)
                except Exception as e:
                    self.say(f"Telegram update failed: {type(e).__name__}: {e}")
            if once:
                return


def cmd_telegram(args):
    path = board_path(args)
    config = telegram_config()
    token = config.get("token", "")
    users = [str(u) for u in config.get("users") or []]
    if not token:
        raise KanvasError("Connect Telegram first: open the board's menu, Connect Telegram, and paste the token BotFather gave you "
                          "(or set KANVAS_TELEGRAM_TOKEN and KANVAS_TELEGRAM_USERS in the environment of this terminal)")
    if not users:
        raise KanvasError("No Telegram user is paired yet: open the board's menu, Connect Telegram, and send the code it shows "
                          "to your bot (or set KANVAS_TELEGRAM_USERS to your numeric Telegram user id)")
    api = telegram_api(token)
    me = api("getMe")
    print(f"Telegram bot @{me.get('username')} listening for {len(users)} user(s) on {board_name(path)}. Ctrl+C stops it.", flush=True)
    try:
        TelegramBridge(path, api, [int(u) for u in users], lambda m: print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)).run(args.once)
    except KeyboardInterrupt:
        print("Stopped.")


def cmd_inbox(args):
    path = board_path(args)
    if args.pair:
        token = new_inbox_token()
        print("Open this on the phone once; it keeps the token. Anyone with it can send ideas to this board, and a new "
              "pairing replaces the old one:\n")
        urls = [f"{base}{quote(board_name(path))}/inbox#token={token}" for base in (phone_bases() or ["http://127.0.0.1:%d/" % DEFAULT_PORT])]
        print("\n".join(urls))
        return
    if args.approve:
        inbox_approve(path, args.approve, "the terminal")
        print(f"Approved the definition of {args.approve}; it is To do and waits for its construction plan")
        return
    if args.drop:
        print(f"Dropped {inbox_drop(path, args.drop)}")
        return
    if not args.text:
        board = load_board(path)
        waiting = inbox_tasks(board)
        if not waiting:
            print("The inbox is empty.")
        for t in waiting:
            info = inbox_of(t)
            print(f"{t['id']}  {info.get('status') or '-':8}  {info.get('kind') or '-':8}  {t['title']}")
        return
    result = inbox_send(path, read_input(args.text, False, None, "idea"), "terminal", args.task)
    print(f"{result['task']}: {result['reply']}")
    if result["ready"]:
        print(f"\nReady. Approve it with `kanvas inbox --approve {result['task']}`.")


def cmd_review(args):
    path = board_path(args)
    board = load_board(path)
    task = find_task(board, args.id)
    if task["state"] != "review":
        raise KanvasError(f"{task['id']} is {view_state(task, task_map(board))}; only tasks in review can be reviewed")
    outcome, text = run_review(path, task["id"], force=True)
    if outcome == "failed":
        raise KanvasError(f"The review of {task['id']} failed: {text}")
    print({"approved": f"The reviewer approved {task['id']}: ",
           "changes": f"The reviewer asked for changes on {task['id']} (review -> doing): ",
           "left": f"The reviewer left {task['id']} for you: ",
           "stale": f"{task['id']}: "}[outcome] + text)


def usage_runs(board, config):
    """One row per run log in the runs folder: the task, its agent role and model, turns, cost and tokens. A log holds
    the last round of its task."""
    rows = []
    folder = config["runs"]
    tasks = {automation_slug(t["id"]): t for t in board["tasks"]}
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not name.endswith(".jsonl"):
            continue
        result = None
        try:
            with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "result":
                        result = event
        except OSError:
            continue
        if result is None:
            continue
        stem, reviewer = name[:-len(".jsonl")], None
        for suffix, role in (("-review-strong", "reviewer-strong"), ("-review", "reviewer"), ("-design", "designer"), ("-po", "po")):
            if stem.endswith(suffix):
                stem, reviewer = stem[:-len(suffix)], role
                break
        task = tasks.get(stem)
        run = ((task or {}).get("work") or {}).get("automation") or {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        models = result.get("modelUsage") if isinstance(result.get("modelUsage"), dict) else {}
        rows.append({
            "task": task["id"] if task else stem,
            "agent": reviewer or run.get("agent") or "-",
            "model": ", ".join(models) or "-",
            "turns": result.get("num_turns") or 0,
            "cost": float(result.get("total_cost_usd") or 0),
            "output": usage.get("output_tokens") or 0,
            "cache_read": usage.get("cache_read_input_tokens") or 0,
            "cache_write": usage.get("cache_creation_input_tokens") or 0,
            "seconds": round((result.get("duration_ms") or 0) / 1000),
        })
    return rows


def cmd_usage(args):
    path = board_path(args)
    board = load_board(path)
    config = automation_config(board, path)
    if config is None:
        raise KanvasError("Automation is not enabled for this board, so there are no runs to measure")
    rows = usage_runs(board, config)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No finished runs yet.")
        return
    width = max(len(r["task"]) for r in rows + [{"task": "TASK"}])
    agent_w = max(len(r["agent"]) for r in rows + [{"agent": "AGENT"}])
    model_w = max(len(r["model"]) for r in rows + [{"model": "MODEL"}])
    print(f"{'TASK':<{width}}  {'AGENT':<{agent_w}}  {'MODEL':<{model_w}}  {'TURNS':>5}  {'COST':>7}  {'OUTPUT':>7}  "
          f"{'CACHE READ':>10}  {'CACHE WRITE':>11}  {'TIME':>5}")
    for r in rows:
        print(f"{r['task']:<{width}}  {r['agent']:<{agent_w}}  {r['model']:<{model_w}}  {r['turns']:>5}  "
              f"${r['cost']:>6.2f}  {r['output']:>7,}  {r['cache_read']:>10,}  {r['cache_write']:>11,}  {r['seconds']:>4}s")
    total = sum(r["cost"] for r in rows)
    print(f"\n{len(rows)} runs, ${total:.2f} in total, ${total / len(rows):.2f} each, "
          f"{sum(r['turns'] for r in rows) / len(rows):.1f} turns each.")
    by_model = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r["cost"])
    for model, costs in sorted(by_model.items(), key=lambda kv: -sum(kv[1])):
        print(f"  {model}: {len(costs)} runs, ${sum(costs):.2f}")


def cmd_note(args):
    path = board_path(args)
    text = read_input(args.text, args.stdin, args.file, "note")
    if not text:
        raise KanvasError("Note text is empty")

    def op(board):
        t = find_task(board, args.id)
        if t["state"] in ("develop", "done", "production"):
            raise KanvasError(f"{t['id']} is {t['state']}; propose a follow-up task instead")
        waited, requested = open_question(t), change_request(t)
        add_note(t, text, actor(args))
        return t, waited, requested

    t, waited, requested = mutate(path, op)
    count = len(t["notes"])
    print(f"Added a note to {t['id']} ({count} note{'' if count == 1 else 's'})")
    if waited and not open_question(t):
        print(f"{t['id']} no longer waits for the human.")
    if requested and not change_request(t):
        print(f"This note answers the human's request for changes on {t['id']}.")


def cmd_ask(args):
    path = board_path(args)
    text = read_input(args.text, args.stdin, args.file, "question")
    if text.startswith(QUESTION):
        text = text[len(QUESTION):].strip()
    if not text:
        raise KanvasError("The question is empty")

    def op(board):
        t = find_task(board, args.id)
        if t["state"] != "doing":
            raise KanvasError(f"{t['id']} is {view_state(t, task_map(board))}; ask on a task you are working on, "
                              "or only in the conversation")
        add_note(t, f"{QUESTION} {text}", actor(args))
        return t

    t = mutate(path, op)
    print(f"{t['id']} now shows the human that you wait for their reply. Ask the same question in the conversation "
          f"and stop. When they reply, record the answer with `{command_line('note ' + t['id'])}`, which ends the wait.")


def cmd_propose(args):
    path = board_path(args)
    desc = read_input(args.desc, args.stdin, args.file, "description")

    def op(board):
        group = None if args.group in ("-", "") else find_group(board, args.group)
        return add_task(board, args.title, group, desc, args.deps or [], by=actor(args))

    t = mutate(path, op)
    deps = f", depends on {', '.join(t['deps'])}" if t["deps"] else ""
    print(f"Proposed {t['id']}: {t['title']}{deps}. A human must approve it before work starts.")


def cmd_add_group(args):
    path = board_path(args)
    g, created = mutate(path, lambda board: add_group(board, args.name, args.prefix))
    if created:
        print(f"Created group {g['name']} (task IDs {g['id']}-01, {g['id']}-02, ...)")
    else:
        print(f"Group {g['name']} [{g['id']}] already exists")


def resolve_ref(board, ref, batch_titles):
    ref = str(ref).strip()
    t = task_map(board).get(ref.upper())
    if t:
        return t["id"]
    if ref.lower() in batch_titles:
        return batch_titles[ref.lower()]
    matches = [t for t in board["tasks"] if t["title"].lower() == ref.lower()]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        raise KanvasError(f"Dependency '{ref}' matches several tasks; use an ID ({', '.join(t['id'] for t in matches)})")
    raise KanvasError(f"Dependency '{ref}' not found (use a task ID, or the title of an earlier task in this batch)")


def batch_items(spec):
    """The groups of a batch as (name, prefix) and its tasks as dicts, after checking the shape of every field."""
    if isinstance(spec, list):
        spec = {"tasks": spec}
    if not isinstance(spec, dict):
        raise KanvasError('Batch must be a JSON object: {"groups": [...], "tasks": [...]}')
    groups, items = spec.get("groups", []), spec.get("tasks", [])
    if not isinstance(groups, list):
        raise KanvasError('Batch "groups" must be a list of group names or {"name": ..., "prefix": ...} objects')
    if not isinstance(items, list):
        raise KanvasError('Batch "tasks" must be a list of task objects, such as {"title": ..., "group": ...}')
    if not groups and not items:
        raise KanvasError('The batch has no groups or tasks: give {"groups": [...], "tasks": [...]}')
    out_groups, out_tasks = [], []
    for i, g in enumerate(groups, 1):
        if isinstance(g, str):
            g = {"name": g}
        if not isinstance(g, dict):
            raise KanvasError(f'Group #{i} in the batch must be a name or a {{"name": ..., "prefix": ...}} object')
        name, prefix = g.get("name"), g.get("prefix")
        if not isinstance(name, str) or not name.strip():
            raise KanvasError(f'Group #{i} in the batch needs a "name" (a string)')
        if prefix is not None and not isinstance(prefix, str):
            raise KanvasError(f'Group \'{name}\': "prefix" must be a string, such as "QA"')
        out_groups.append((name, prefix))
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise KanvasError(f'Task #{i} in the batch must be an object, such as {{"title": ..., "group": ...}}')
        title = item.get("title")
        if title is not None and not isinstance(title, str):
            raise KanvasError(f'Task #{i} in the batch: "title" must be a string')
        if not (title or "").strip():
            raise KanvasError(f"Task #{i} in the batch has no title")
        desc_key = "description" if "description" in item and "desc" not in item else "desc"
        deps_key = "depends_on" if "depends_on" in item and "deps" not in item else "deps"
        desc, group, deps = item.get(desc_key), item.get("group"), item.get(deps_key)
        if desc is not None and not isinstance(desc, str):
            raise KanvasError(f"Task '{title}': \"{desc_key}\" must be a string")
        if group is not None and not isinstance(group, str):
            raise KanvasError(f"Task '{title}': \"group\" must be a string, the name or prefix of a group")
        if isinstance(deps, str):
            deps = [deps]
        if deps is not None and not (isinstance(deps, list) and all(isinstance(r, str) for r in deps)):
            raise KanvasError(f"Task '{title}': \"{deps_key}\" must be a task ID or title, or a list of them")
        out_tasks.append({"title": title, "group": group or "", "desc": desc or "", "deps": deps or []})
    return out_groups, out_tasks


def cmd_batch(args):
    path = board_path(args)
    if not args.file and sys.stdin.isatty():
        raise KanvasError("Pipe the batch JSON into stdin, or pass --file PATH")
    raw = read_input(None, use_stdin=not args.file, file_path=args.file, what="batch")
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KanvasError(f"Batch is not valid JSON: {e}")
    groups, items = batch_items(spec)

    def op(board):
        new_groups, new_tasks, titles = [], [], {}
        for name, prefix in groups:
            group, created = add_group(board, name, prefix)
            if created:
                new_groups.append(group)
        for item in items:
            try:
                group = find_group(board, item["group"]) if item["group"] else None
                deps = [resolve_ref(board, r, titles) for r in item["deps"]]
            except KanvasError as e:
                raise KanvasError(f"Task '{item['title']}': {e}")
            t = add_task(board, item["title"], group, item["desc"], deps, by=actor(args))
            titles[t["title"].lower()] = t["id"]
            new_tasks.append(t)
        return new_groups, new_tasks

    new_groups, new_tasks = mutate(path, op)
    for g in new_groups:
        print(f"+ group {g['name']} [{g['id']}]")
    for t in new_tasks:
        deps = f"  (after {', '.join(t['deps'])})" if t["deps"] else ""
        print(f"+ {t['id']}  {t['title']}{deps}")
    one = len(new_tasks) == 1
    print(f"Proposed {len(new_tasks)} task{'' if one else 's'}. A human must approve {'it' if one else 'them'} before work starts.")


def cmd_depend(args):
    path = board_path(args)

    def op(board):
        t = find_task(board, args.id)
        if is_complete(t):
            raise KanvasError(f"{t['id']} is {t['state']}; dependencies can no longer change")
        before = yolo_cover(board)
        added = []
        for ref in args.on:
            d = find_task(board, ref)
            if d["id"] in t["deps"]:
                continue
            if depends_on(board, d["id"], t["id"]):
                raise KanvasError(f"{t['id']} cannot depend on {d['id']}: that would create a circular dependency")
            t["deps"].append(d["id"])
            added.append(d["id"])
        cover = yolo_cover(board)
        joined = [x["id"] for x in board["tasks"]
                  if x["state"] == "proposed" and x["id"].upper() in cover and x["id"].upper() not in before]
        return t, added, view_state(t, task_map(board)), joined

    t, added, vs, joined = mutate(path, op)
    if not added:
        print(f"{t['id']} already depends on {', '.join(args.on)}")
        return
    print(f"{t['id']} now depends on {', '.join(added)}" + (" (blocked until they are done)" if vs == "blocked" else ""))
    if joined:
        print(f"YOLO does not approve proposals linked into its chain: ask the human to approve {', '.join(joined)} in the board.")


def find_proposal(board, ref):
    """A task agents may still change: one that is still proposed."""
    t = find_task(board, ref)
    if t["state"] != "proposed":
        raise KanvasError(f"{t['id']} is approved ({t['state']}); only a human can change it now, in the board")
    return t


def cmd_edit(args):
    path = board_path(args)
    has_desc = args.new_desc is not None or args.stdin or bool(args.file)
    desc = read_input(args.new_desc, args.stdin, args.file, "description") if has_desc else None
    note = read_input(args.message, what="note") if args.message else ""
    if args.title is None and args.group is None and not has_desc:
        raise KanvasError("Nothing to change: give --title, --group, or a description (--desc, --stdin or --file)")

    def op(board):
        t = find_proposal(board, args.id)
        requested = change_request(t)
        changed = []
        if args.title is not None:
            title = " ".join(args.title.split())
            if not title:
                raise KanvasError("Task title is empty")
            if title != t["title"]:
                t["title"] = title
                changed.append("title")
        if args.group is not None:
            gid = None if args.group in ("-", "") else find_group(board, args.group)["id"]
            if gid != t.get("group"):
                t["group"] = gid
                changed.append("group")
        if desc is not None and desc != t.get("desc", ""):
            t["desc"] = desc
            changed.append("description")
        if note:
            add_note(t, note, actor(args))
        return t, changed, requested

    t, changed, requested = mutate(path, op)
    what = " and ".join([", ".join(changed[:-1]), changed[-1]] if len(changed) > 1 else changed) or "nothing (it already read that way)"
    print(f"Revised {t['id']}: {what}{', with a note' if note else ''}")
    if requested and change_request(t):
        print(f"The human's request for changes on {t['id']} stays open until you say what you changed: "
              f"`{command_line('note ' + t['id'])} \"...\"`, or -m with your next edit.")
    elif requested:
        print(f"This answers the human's request for changes on {t['id']}.")


def cmd_withdraw(args):
    path = board_path(args)

    def op(board):
        gone = []
        for ref in args.ids:
            t = find_proposal(board, ref)
            if t["id"] not in gone:
                gone.append(t["id"])
        keys = {g.upper() for g in gone}
        for t in board["tasks"]:
            if t["id"].upper() in keys:
                continue
            waiting = [d for d in t.get("deps", []) if d.upper() in keys]
            if waiting and t["state"] != "proposed":
                raise KanvasError(f"{t['id']} is approved and depends on {', '.join(waiting)}; "
                                  "ask the human to remove that dependency first")
        rewired = []
        for t in board["tasks"]:
            kept = [d for d in t.get("deps", []) if d.upper() not in keys]
            if len(kept) != len(t.get("deps", [])):
                t["deps"] = kept
                rewired.append(t["id"])
        board["tasks"] = [t for t in board["tasks"] if t["id"].upper() not in keys]
        rewired = [r for r in rewired if r.upper() not in keys]
        return gone, rewired

    gone, rewired = mutate(path, op)
    print(f"Withdrew {', '.join(gone)}" + (f"; {', '.join(rewired)} no longer depend on them" if rewired else ""))


def cmd_undepend(args):
    path = board_path(args)

    def op(board):
        t = find_proposal(board, args.id)
        keys = {str(r).strip().upper() for r in args.on}
        removed = [d for d in t["deps"] if d.upper() in keys]
        missing = [r for r in args.on if str(r).strip().upper() not in {d.upper() for d in t["deps"]}]
        if missing:
            raise KanvasError(f"{t['id']} does not depend on {', '.join(missing)}")
        t["deps"] = [d for d in t["deps"] if d.upper() not in keys]
        return t, removed

    t, removed = mutate(path, op)
    print(f"{t['id']} no longer depends on {', '.join(removed)}")


# ---------------------------------------------------------------------------
# Setup, migration and sharing
# ---------------------------------------------------------------------------


AGENTS_LINE = "Plan and track work on the Factory board; follow .agents/skills/kanvas/SKILL.md."


def find_git_top(start=None):
    """Return the top folder of the git repository around `start`, or None."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        # A .git file marks worktrees and submodules.
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def project_folder(start=None):
    """Return the folder to set up when none is given, and the reason when it is not `start` itself.

    An existing board wins, so `init` never creates a second board above it.
    """
    here = os.path.abspath(start or os.getcwd())
    board = find_board(here)
    if board:
        found, reason = os.path.dirname(board), f"the folder of the existing {BOARD_FILE}"
    else:
        found, reason = find_git_top(here), "the top folder of this git repository"
    if not found or found == here:
        return here, None
    return found, reason


def points_to_skill(folder):
    """True if an AGENTS.md or CLAUDE.md in `folder` already sends agents to the Factory skill."""
    for name in os.listdir(folder):
        if name.lower() in ("agents.md", "claude.md"):
            try:
                with open(os.path.join(folder, name), "rb") as f:
                    if b".agents/skills/kanvas/SKILL.md" in f.read():
                        return True
            except OSError:
                pass
    return False


def cmd_init(args):
    if args.dir is None:
        init_project(*project_folder())
    else:
        init_project(os.path.abspath(args.dir))


def init_project(target, reason=None, next_steps=True):
    """Set up Factory in `target`, print what was done, and return the board's path."""
    if not os.path.isdir(target):
        raise KanvasError(f"Directory does not exist: {target}")
    skill_md = next(
        (p for p in (os.path.join(HERE, "skill", "SKILL.md"), os.path.join(HERE, "SKILL.md")) if os.path.isfile(p)),
        None,
    )
    if not skill_md or not os.path.isfile(HTML_FILE):
        raise KanvasError("kanvas.html and SKILL.md must sit next to kanvas.py (or in skill/)")

    # The skill folders go first: agent sandboxes such as Codex's keep .agents/ read-only,
    # and a refused write should leave the project as it was.
    report = []
    skill_dir = os.path.join(target, ".agents", "skills", "kanvas")
    # Claude Code reads skills only from .claude/skills; the other agents read .agents/skills.
    # Older versions also copied the skill into .cursor and .gemini, and those copies stay current.
    mirrors = [".claude"] + [
        d for d in (".cursor", ".gemini") if os.path.isfile(os.path.join(target, d, "skills", "kanvas", "SKILL.md"))
    ]
    try:
        os.makedirs(skill_dir, exist_ok=True)
        for src, name in ((os.path.abspath(__file__), "kanvas.py"), (HTML_FILE, "kanvas.html"), (skill_md, "SKILL.md")):
            dst = os.path.join(skill_dir, name)
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copyfile(src, dst)
        report.append("installed .agents/skills/kanvas/ (kanvas.py, kanvas.html, SKILL.md)")
        for base in mirrors:
            dst_dir = os.path.join(target, base, "skills", "kanvas")
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copyfile(skill_md, os.path.join(dst_dir, "SKILL.md"))
            report.append(f"installed {base}/skills/kanvas/SKILL.md")
    except OSError as e:
        raise KanvasError(
            f"Could not write {os.path.relpath(e.filename or skill_dir, target)}: {e.strerror or e}. "
            "Agent sandboxes often keep .agents/ read-only; run "
            f"`{command_line('init')}` outside the sandbox, or ask the user to run it."
        ) from None

    board_file = os.path.join(target, BOARD_FILE)
    if os.path.exists(board_file):
        report.insert(0, f"kept      {BOARD_FILE}")
    else:
        write_text_atomic(board_file, dumps(new_board(os.path.basename(target))))
        report.insert(0, f"created   {BOARD_FILE}")

    if not any(f.lower() == "agents.md" for f in os.listdir(target)):
        with open(os.path.join(target, "AGENTS.md"), "x", encoding="utf-8", newline="\n") as f:
            f.write(f"# Agent instructions\n\n{AGENTS_LINE}\n")
        report.append("created   AGENTS.md")

    print(f"Factory {VERSION} in {target}" + (f", {reason}" if reason else "") + "\n")
    for line in report:
        print(f"  {line}")
    legacy = sorted(f for f in os.listdir(target) if f.endswith(".canvas"))
    if legacy and not load_board(board_file)["tasks"]:
        migrate = project_command(f'import "{legacy[0]}"')
        print(f"\nFound {', '.join(legacy)}. Migrate a Kanvas 0.x Obsidian board with:")
        print(f"  {migrate}")
    if next_steps:
        print("\nOpen the board:")
        print(f"  {project_command('serve')}")
    if not points_to_skill(target):
        print("\nSo agents know about the board in every session, add this line to AGENTS.md or CLAUDE.md:")
        print(f"  {AGENTS_LINE}")
    print("\nNo existing file was modified.")
    return board_file


CANVAS_STATES = {"6": "proposed", "1": "todo", "0": "todo", "2": "doing", "5": "review", "4": "done"}
CANVAS_TASK_RE = re.compile(r"^##\s+([A-Za-z][A-Za-z0-9]*)-(\d+)\s+(.*)$")
CANVAS_SKIP_IDS = {"legend", "canvas-errors", "canvas-warnings", "canvas-lint", "canvas-status"}


def import_canvas(canvas, title=""):
    """Convert an Obsidian Canvas board from Kanvas 0.x into a Factory board."""
    nodes = canvas.get("nodes", [])
    group_nodes = [n for n in nodes if n.get("type") == "group"]

    def container(n):
        cx = n.get("x", 0) + n.get("width", 0) / 2
        cy = n.get("y", 0) + n.get("height", 0) / 2
        best, area = None, float("inf")
        for g in group_nodes:
            gx, gy, gw, gh = g.get("x", 0), g.get("y", 0), g.get("width", 0), g.get("height", 0)
            if gx <= cx <= gx + gw and gy <= cy <= gy + gh and gw * gh < area:
                best, area = g, gw * gh
        return best

    items = {}
    for n in nodes:
        if n.get("type") != "text" or n.get("id") in CANVAS_SKIP_IDS:
            continue
        text = (n.get("text") or "").replace("\r\n", "\n")
        first, _, rest = text.partition("\n")
        m = CANVAS_TASK_RE.match(first.strip())
        if not m and (not first.startswith("## ") or n.get("color", "") in ("", "0")):
            continue
        g = container(n)
        items[n["id"]] = {
            "node": n["id"],
            "canvas_group": g["id"] if g else None,
            "id": f"{m.group(1).upper()}-{int(m.group(2)):02d}" if m else None,
            "prefix": m.group(1).upper() if m else None,
            "title": (m.group(3) if m else first.lstrip("#")).strip(),
            "desc": rest.strip(),
            "state": CANVAS_STATES.get(str(n.get("color", "1")), "todo"),
            "deps": [],
        }
    for e in canvas.get("edges", []):
        src, dst = e.get("fromNode"), e.get("toNode")
        if src in items and dst in items and src != dst and src not in items[dst]["deps"]:
            items[dst]["deps"].append(src)
    depth = chain_depths({k: it["deps"] for k, it in items.items()})

    def group_rank(g):
        member_depths = [depth[k] for k, it in items.items() if it["canvas_group"] == g["id"]]
        return (min(member_depths) if member_depths else float("inf"), g.get("x", 0), g.get("y", 0))

    # Upstream groups first, so the board reads left to right.
    board = new_board(title)
    groups = {}
    for gnode in sorted(group_nodes, key=group_rank):
        counts = Counter(it["prefix"] for it in items.values() if it["canvas_group"] == gnode["id"] and it["prefix"])
        prefix = counts.most_common(1)[0][0] if counts else None
        if prefix and (not PREFIX_RE.match(prefix) or any(g["id"] == prefix for g in board["groups"])):
            prefix = None
        groups[gnode["id"]], _ = add_group(board, gnode.get("label") or "Group", prefix)
    rank = {g["id"]: i for i, g in enumerate(board["groups"])}

    def group_id(it):
        g = groups.get(it["canvas_group"])
        return g["id"] if g else None

    def sort_key(it):
        head, _, num = (it["id"] or "~-0").rpartition("-")
        return (rank.get(group_id(it), len(rank)), depth[it["node"]], head, int(num))

    ordered = sorted(items.values(), key=sort_key)
    used = set()
    for it in ordered:
        if it["id"] in used:
            it["id"] = None
        elif it["id"]:
            used.add(it["id"])
    for it in ordered:
        if not it["id"]:
            prefix = group_id(it) or UNGROUPED_PREFIX
            taken = [int(u.rpartition("-")[2]) for u in used if u.rpartition("-")[0] == prefix]
            it["id"] = f"{prefix}-{max(taken + [0]) + 1:02d}"
            used.add(it["id"])
    ids = {it["node"]: it["id"] for it in ordered}
    for it in ordered:
        board["tasks"].append({
            "id": it["id"], "title": it["title"] or it["id"], "state": it["state"],
            "group": group_id(it), "deps": [ids[d] for d in it["deps"]], "desc": it["desc"], "notes": [],
        })
    return board


def cmd_import(args):
    try:
        canvas = json.loads(read_text_file(args.canvas))
    except (OSError, json.JSONDecodeError) as e:
        raise KanvasError(f"Cannot read {args.canvas}: {e}")
    out = os.path.abspath(args.output or os.path.join(os.path.dirname(os.path.abspath(args.canvas)), BOARD_FILE))
    title = os.path.splitext(os.path.basename(args.canvas))[0]
    board = import_canvas(canvas, title)
    if os.path.exists(out) and not args.force:
        existing = load_board(out)
        if existing["tasks"] or existing["groups"]:
            raise KanvasError(f"{rel(out)} already has tasks; pass --force to replace it")
    write_text_atomic(out, dumps(board))
    tasks = task_map(board)
    states = Counter(view_state(t, tasks) for t in board["tasks"])
    summary = ", ".join(f"{v} {k}" for k, v in states.items())
    print(f"Imported {len(board['tasks'])} tasks in {len(board['groups'])} groups into {rel(out)} ({summary})")
    print(f"{rel(args.canvas)} was left untouched; delete it once you are happy with the result.")


def cmd_export(args):
    path = board_path(args)
    board = load_board(path)
    html = read_text_file(HTML_FILE)
    if SNAPSHOT_TAG not in html:
        raise KanvasError("kanvas.html has no snapshot slot")
    data = json.dumps(canonical(board), ensure_ascii=False).replace("<", "\\u003c")
    tag = SNAPSHOT_TAG.replace('json">', f'json" data-at="{now_iso()}" data-name="{escape(board_name(path))}">{data}', 1)
    out = os.path.abspath(args.output)
    write_text_atomic(out, html.replace(SNAPSHOT_TAG, tag, 1))
    print(f"Wrote {rel(out)}: a read-only snapshot of {len(board['tasks'])} tasks. Open or share it as is.")


# ---------------------------------------------------------------------------
# QR codes, so a phone can open a board's address
# ---------------------------------------------------------------------------

# Error correction level M, by version: codewords per block, and blocks.
QR_ECC_PER_BLOCK = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
                    26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28)
QR_BLOCKS = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
             17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49)


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_remainder(data, degree):
    divisor, root = [0] * (degree - 1) + [1], 1
    for _ in range(degree):
        for j in range(degree):
            divisor[j] = _gf_mul(divisor[j], root)
            if j + 1 < degree:
                divisor[j] ^= divisor[j + 1]
        root = _gf_mul(root, 2)
    rem = [0] * degree
    for b in data:
        factor = b ^ rem.pop(0)
        rem.append(0)
        for i, coef in enumerate(divisor):
            rem[i] ^= _gf_mul(coef, factor)
    return rem


def _raw_modules(ver):
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        align = ver // 7 + 2
        n -= (25 * align - 10) * align - 55
        if ver >= 7:
            n -= 36
    return n


def qr_matrix(text):
    """The QR code of `text` (byte mode, error correction M) as rows of booleans, True for dark."""
    data = text.encode("utf-8")
    for ver in range(1, 41):
        capacity = _raw_modules(ver) // 8 - QR_ECC_PER_BLOCK[ver] * QR_BLOCKS[ver]
        count_bits = 8 if ver < 10 else 16
        if 4 + count_bits + 8 * len(data) <= capacity * 8:
            break
    else:
        raise KanvasError("Too long for a QR code")
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in reversed(range(count_bits))]
    bits += [(b >> i) & 1 for b in data for i in reversed(range(8))]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    words += [(0xEC, 0x11)[i % 2] for i in range(capacity - len(words))]

    # Split into blocks, add error correction, and interleave.
    nblocks, ecc = QR_BLOCKS[ver], QR_ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    short, short_len = nblocks - raw % nblocks, raw // nblocks
    blocks, k = [], 0
    for i in range(nblocks):
        part = words[k:k + short_len - ecc + (0 if i < short else 1)]
        k += len(part)
        blocks.append(part + ([0] if i < short else []) + _rs_remainder(part, ecc))
    stream = [blk[i] for i in range(len(blocks[0])) for j, blk in enumerate(blocks)
              if i != short_len - ecc or j >= short]

    size = ver * 4 + 17
    dark = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x, y, on):
        dark[y][x], fixed[y][x] = on, True

    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    if ver > 1:
        align = ver // 7 + 2
        step = (ver * 8 + align * 3 + 5) // (align * 4 - 4) * 2
        pos = [6] + sorted(size - 7 - i * step for i in range(align - 1))
        for ax in pos:
            for ay in pos:
                if (ax, ay) in ((6, 6), (6, pos[-1]), (pos[-1], 6)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = ver << 12 | rem
        for i in range(18):
            a, b, on = size - 11 + i % 3, i // 3, (vbits >> i) & 1 == 1
            put(a, b, on)
            put(b, a, on)

    def put_format(mask):
        rem = mask  # level M is 0
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        f = (mask << 10 | rem) ^ 0x5412
        bit = lambda i: (f >> i) & 1 == 1
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6))
        put(8, 8, bit(7))
        put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, size - 15 + i, bit(i))
        put(8, size - 8, True)

    put_format(0)
    i, right = 0, size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if (right + 1) & 2 == 0 else vert
                if not fixed[y][x] and i < len(stream) * 8:
                    dark[y][x] = (stream[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2

    masks = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)

    def masked(m):
        put_format(m)
        return [[dark[y][x] ^ (not fixed[y][x] and masks[m](x, y)) for x in range(size)] for y in range(size)]

    def penalty(g):
        score = 0
        for line in g + [list(col) for col in zip(*g)]:
            run = 1
            for a, b in zip(line, line[1:] + [None]):
                if a == b:
                    run += 1
                else:
                    score += run - 2 if run >= 5 else 0
                    run = 1
        score += 3 * sum(g[y][x] == g[y][x + 1] == g[y + 1][x] == g[y + 1][x + 1]
                         for y in range(size - 1) for x in range(size - 1))
        share = sum(map(sum, g)) * 20 // (size * size)
        return score + 10 * abs(share - 10)

    best = min(range(8), key=lambda m: penalty(masked(m)))
    return masked(best)


def qr_svg(text):
    m = qr_matrix(text)
    n = len(m) + 8
    path = "".join(f"M{x + 4} {y + 4}h1v1h-1z" for y, row in enumerate(m) for x, on in enumerate(row) if on)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')


def qr_text(text):
    """The QR code of `text` for a terminal, two rows per line, dark on light whatever the terminal's colours."""
    m = qr_matrix(text)
    n = len(m)
    rows = [[False] * (n + 4)] * 2 + [[False] * 2 + r + [False] * 2 for r in m] + [[False] * (n + 4)] * 3
    chars = {(False, False): " ", (True, False): "\u2580", (False, True): "\u2584", (True, True): "\u2588"}
    return "\n".join("\x1b[30;107m" + "".join(chars[a, b] for a, b in zip(rows[y], rows[y + 1])) + "\x1b[0m"
                     for y in range(0, len(rows) - 1, 2))


# ---------------------------------------------------------------------------
# Local server for the browser board
# ---------------------------------------------------------------------------


def board_name(path):
    """The name a board goes by in the URL: its project folder's, made safe for a path."""
    folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
    return re.sub(r"[^\w.-]+", "-", folder).strip("-.") or "board"


def same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def page(title, body):
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{escape(title)}</title><style>:root {{ color-scheme: light dark; font: 15px/1.5 system-ui, sans-serif; }}"
            " body { max-width: 640px; margin: 56px auto; padding: 0 20px; } h1 { font-size: 22px; }"
            " a.board { display: block; margin: 8px 0; padding: 10px 14px; border: 1px solid #8885; border-radius: 9px; color: inherit; text-decoration: none; }"
            " a.board:hover { border-color: #6366f1; } small { display: block; opacity: .6; } code { font-size: 13px; }</style>"
            f"<body>{body}</body></html>").encode("utf-8")


LOCAL_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})
DEFAULT_PORT = 8765


def state_file():
    """Where Factory remembers the open boards between runs: the user's own application state folder."""
    base = os.environ.get("KANVAS_STATE_DIR")
    if not base:
        if os.name == "nt":
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"), "Kanvas")
        elif sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Application Support/Kanvas")
        else:
            base = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "kanvas")
    return os.path.join(base, "boards.json")


class AutomationDispatcher:
    """Watches open boards and turns newly-started tasks into isolated Claude Code runs."""

    def __init__(self, server):
        self.server = server
        self.stop_event = threading.Event()
        self.active = set()
        self.tried = set()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._loop, name="kanvas-automation", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        while not self.stop_event.wait(1):
            with self.server.boards_lock:
                paths = list(self.server.boards.values())
            for path in paths:
                try:
                    self.scan(path)
                except (KanvasError, OSError, ValueError) as e:
                    if not self.server.quiet:
                        print(f"Automation skipped {path}: {e}", file=sys.stderr, flush=True)

    def scan(self, path):
        board = load_board(path)
        config = automation_config(board, path)
        if config is None:
            return
        cleaned = self.__dict__.setdefault("cleaned", set())
        if path not in cleaned:
            cleaned.add(path)
            clear_busy_marks(path)
            board = load_board(path)
        self._settle_orphan_stops(path, board)
        cover, tasks = yolo_cover(board), task_map(board)
        for task in list(board["tasks"]):
            work = task.get("work") if isinstance(task.get("work"), dict) else {}
            run = work.get("automation") if isinstance(work.get("automation"), dict) else {}
            if (config["curator"]["enabled"] and CURATOR_REPOSITORY in config["repositories"] and task.get("state") == "done"
                    and not task.get("curated") and run.get("status") == "completed"
                    and task_repository_ids(task) and CURATOR_REPOSITORY not in task_repository_ids(task)):
                curate_task(path, task["id"])
                continue
            if (config["review"]["enabled"] and task.get("state") == "review" and run.get("status") == "completed"
                    and any(os.path.isdir(w.get("worktree") or "") for w in run.get("worktrees") or [])
                    and ((task.get("review") or {}).get("agent") or {}).get("status") == "pending"
                    and run.get("reviewed_round") != int(run.get("round") or 1)):
                self._start_review(path, task["id"])
                continue
            # YOLO writes the plan by itself for a defined task of the chain that is free to start. The human still
            # approves the design, if the task has one, and the pull request.
            if (task.get("state") == "todo" and not work and cover.get(task["id"].upper()) and task_repository_ids(task)
                    and view_state(task, tasks) == "todo" and isinstance(task.get("definition"), dict)
                    and task["definition"].get("approved")):
                self._start_auto_plan(path, task["id"])
                continue
            # A completed run moves its task to Review, so a completed run on a task in Doing was sent back.
            sent_back = run.get("status") == "completed"
            if (task.get("state") != "doing" or not work.get("plan") or not task_repository_ids(task)
                    or (run.get("status") and not sent_back)):
                continue
            if design_applies(task, config):
                design = design_of(task)
                if design.get("status") in (None, "", DESIGN_CHANGES):
                    self._start_design(path, task["id"])
                if design.get("status") != DESIGN_APPROVED:
                    continue
            token = (os.path.normcase(os.path.abspath(path)), task["id"].upper())
            with self.lock:
                if token in self.active:
                    continue
                self.active.add(token)

            def claim(board, tid=task["id"]):
                fresh = find_task(board, tid)
                fresh_work = fresh.get("work") if isinstance(fresh.get("work"), dict) else {}
                fresh_run = fresh_work.get("automation") if isinstance(fresh_work.get("automation"), dict) else {}
                again = fresh_run.get("status") == "completed"
                if fresh.get("state") != "doing" or (fresh_run.get("status") and not again):
                    return False
                rounds = int(fresh_run.get("round") or 1)
                queued = {"status": "queued", "updated": now_iso(), "round": rounds}
                start = int(fresh_run.get("cycle_start") or 1)
                if again:
                    # The rounds the reviewer asks for count towards the limit; a human sending the work back starts a new cycle.
                    if not sent_back_by_agent(fresh):
                        start = rounds + 1
                    done = rounds - start + 1
                    if done >= config["max_rounds"]:
                        message = (f"Stopped after {done} rounds (automation.max_rounds). Continue by hand, or raise "
                                   f"the limit and run `retry {fresh['id']}`.")
                        fresh_work["automation"] = {**fresh_run, "status": "failed", "updated": now_iso(),
                                                    "summary": message}
                        add_note(fresh, "Automation failed: " + message, "kanvas-runner")
                        fresh["work"] = fresh_work
                        return False
                    queued.update(round=rounds + 1, since=fresh_run.get("updated") or "", cycle_start=start)
                else:
                    if fresh_run.get("since"):
                        queued["since"] = fresh_run["since"]
                    if fresh_run.get("cycle_start"):
                        queued["cycle_start"] = fresh_run["cycle_start"]
                if fresh_run.get("pull_requests"):
                    queued["pull_requests"] = fresh_run["pull_requests"]
                if fresh_run.get("resumed"):
                    queued["resumed"] = fresh_run["resumed"]
                fresh_work["automation"] = queued
                fresh["work"] = fresh_work
                return True

            if not mutate(path, claim):
                with self.lock:
                    self.active.discard(token)
                continue

            def execute(board_path=path, tid=task["id"], key=token):
                try:
                    run_automation(board_path, tid)
                finally:
                    with self.lock:
                        self.active.discard(key)

            threading.Thread(target=execute, name=f"kanvas-{task['id']}", daemon=True).start()

    def _settle_orphan_stops(self, path, board):
        """A stop asked for a run this Factory is not running (it restarted since) has nobody to answer it: the run is over."""
        for task in board["tasks"]:
            work = task.get("work") if isinstance(task.get("work"), dict) else {}
            if not work.get("stop"):
                continue
            key = (os.path.normcase(os.path.abspath(path)), task["id"].upper())
            with self.lock:
                if key in self.active or ("review",) + key in self.active or ("design",) + key in self.active:
                    continue

            def settle(fresh, tid=task["id"]):
                t = find_task(fresh, tid)
                w = t.get("work") if isinstance(t.get("work"), dict) else {}
                w.pop("stop", None)
                w.pop("busy", None)
                run = w.get("automation") if isinstance(w.get("automation"), dict) else {}
                if run.get("status") in ("queued", "running"):
                    w["automation"] = {**run, "status": "failed", "updated": now_iso(),
                                       "summary": "Stopped by you; it was not running any more."}
                    add_note(t, "Automation failed: stopped by you; it was not running any more.", "kanvas-runner")
            mutate(path, settle)

    def _start_auto_plan(self, path, task_id):
        """YOLO: has the architect and the senior write the construction plan of a task the human already defined, and starts it.
        It tries once per task while the server runs; a failure leaves a note and the task in To do for the human."""
        token = ("plan", os.path.normcase(os.path.abspath(path)), task_id.upper())
        tried = self.__dict__.setdefault("tried", set())
        with self.lock:
            if token in self.active or token in tried:
                return
            self.active.add(token)

        def note_failure(reason):
            def add(board):
                t = find_task(board, task_id)
                if t.get("state") == "todo" and not t.get("work"):
                    add_note(t, f"YOLO could not write the technical plan: {reason}. Plan it by hand with Architect/Senior Grill me.", "factory")
            try:
                mutate(path, add)
            except Exception:
                pass

        def execute():
            retry = False
            try:
                name = next((n for n, p in self.server.boards.items()
                             if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(path))), None)
                if name is None:
                    return
                try:
                    job = self.server.start_assist(name, "plan", task_id, {"plan": ""})
                except AssistBusy:
                    retry = True
                    return
                while job["status"] == "running" and not self.stop_event.wait(2):
                    pass
                if job["status"] != "done":
                    raise KanvasError(job.get("error") or "the agents did not finish")
                plan = job["result"]["draft"]["plan"]
                settled = "; ".join(q["text"] if isinstance(q, dict) else str(q) for q in job["result"].get("questions") or [])

                def start(board):
                    t = find_task(board, task_id)
                    definition = t.get("definition") if isinstance(t.get("definition"), dict) else {}
                    if t.get("state") != "todo" or t.get("work") or not definition.get("approved"):
                        return None
                    at = now_iso()
                    t["work"] = {"by": "factory (YOLO)", "at": at, "plan": plan, "repositories": list(definition.get("repositories") or [])}
                    t.pop("review", None)
                    t["state"] = "doing"
                    add_note(t, plan + (f"\n\nOpen questions it settled with its best option: {settled}" if settled else ""), "factory")
                    return t
                mutate(path, start)
            except Exception as e:
                note_failure(str(e))
            finally:
                with self.lock:
                    self.active.discard(token)
                    if not retry:
                        tried.add(token)

        threading.Thread(target=execute, name=f"kanvas-plan-{task_id}", daemon=True).start()

    def _start_design(self, path, task_id):
        """Has the designer propose the UI/UX of a task that is about to start, unless it is already doing so."""
        token = ("design", os.path.normcase(os.path.abspath(path)), task_id.upper())
        with self.lock:
            if token in self.active:
                return
            self.active.add(token)

        def execute():
            try:
                run_design(path, task_id)
            finally:
                with self.lock:
                    self.active.discard(token)

        threading.Thread(target=execute, name=f"kanvas-design-{task_id}", daemon=True).start()

    def _start_review(self, path, task_id):
        """Has the reviewer look at a task that reached Review, unless it is already doing so. The task keeps no
        marker of a review in progress, so a review that a restart interrupted is simply started again."""
        token = ("review", os.path.normcase(os.path.abspath(path)), task_id.upper())
        with self.lock:
            if token in self.active:
                return
            self.active.add(token)

        def execute():
            try:
                run_review(path, task_id)
            finally:
                with self.lock:
                    self.active.discard(token)

        threading.Thread(target=execute, name=f"kanvas-review-{task_id}", daemon=True).start()


class BoardServer(ThreadingHTTPServer):
    """Serves every open board, each at /<name>/."""

    daemon_threads = True
    # On Windows SO_REUSEADDR lets two servers share a port.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, path=None, hosts=LOCAL_NAMES, quiet=False, share=None, remember=None):
        """`hosts` are the names allowed in the Host header, or None for any. A server made with
        `share` serves the boards of that one and cannot open boards itself. With `remember`, a
        file path, the open boards and whether phones may connect are kept there for the next start."""
        # TCPServer.__init__ calls server_close when the port is busy, before the rest is set up.
        self.phone_listener = None
        super().__init__(address, BoardHandler)
        self.hosts = hosts
        self.quiet = quiet
        self.remember = remember
        self.main = share or self
        self.opens_boards = share is None
        self.phone = []  # base URLs of the Tailscale listener, by name and by address
        self.phone_lock = threading.Lock()
        self.boards = share.boards if share else {}  # name -> path, in the order the boards were opened
        self.boards_lock = share.boards_lock if share else threading.Lock()
        self.dispatcher = share.dispatcher if share else AutomationDispatcher(self)
        self.assist = share.assist if share else {}  # job id -> the agents' help running or finished, oldest first
        self.assist_lock = share.assist_lock if share else threading.Lock()
        self.inbox = share.inbox if share else {}  # job id -> a turn of the product owner over an idea, running or finished
        self.telegram = None  # the bot's bridge while it runs, on the main server only
        self.telegram_lock = threading.Lock()
        if path:
            self.add_board(path)

    @property
    def check_host(self):
        return self.hosts is not None

    def open_to_phone(self):
        """Serve the boards on this computer's Tailscale address too, on the same port; return the
        base URLs a phone can open."""
        with self.phone_lock:
            if self.phone:
                return self.phone
            ip, name, magic = tailscale_self()
            names = tailscale_names(ip, name)
            port = self.server_address[1]
            try:
                listener = BoardServer((ip, port), hosts=frozenset(names), quiet=self.quiet, share=self)
            except OSError as e:
                raise KanvasError(f"Could not listen on the Tailscale address {ip}:{port}: {e}")
            threading.Thread(target=listener.serve_forever, daemon=True).start()
            self.phone_listener = listener
            # `tailscale serve` passes these names on to this computer's own address.
            if self.hosts is not None:
                self.hosts = self.hosts | names
            self.phone_names = names
            self.phone = [f"http://{h}:{port}/" for h in ([name] if magic else []) + [ip]]
        self.save_state()
        return self.phone

    def open_to_phone_soon(self, tries=20, wait=15):
        """Open to phones in the background, retrying while Tailscale is still starting, as after a login."""
        def run():
            for i in range(tries):
                try:
                    urls = self.open_to_phone()
                except KanvasError as e:
                    if i == tries - 1:
                        print(f"Could not open Factory to your phone: {e}", file=sys.stderr, flush=True)
                    time.sleep(wait)
                    continue
                if not self.quiet:
                    print(f"[{time.strftime('%H:%M:%S')}] opened to phones on Tailscale: {'  or  '.join(urls)}", flush=True)
                return
        threading.Thread(target=run, daemon=True).start()

    def close_to_phone(self):
        with self.phone_lock:
            self._stop_phone()
            if self.hosts is not None:
                self.hosts = self.hosts - getattr(self, "phone_names", set()) | LOCAL_NAMES
            self.phone = []
        self.save_state()

    def _stop_phone(self):
        if self.phone_listener:
            self.phone_listener.shutdown()
            self.phone_listener.server_close()
            self.phone_listener = None

    def server_close(self):
        if getattr(self, "opens_boards", False) and getattr(self, "dispatcher", None):
            self.dispatcher.stop()
        self._stop_phone()
        super().server_close()

    def server_bind(self):
        # HTTPServer.server_bind looks up the host's full name with socket.getfqdn,
        # which can take half a minute on macOS. Nothing here uses that name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def add_board(self, path, save=True):
        """Open the board at `path` and return its name. A board that is already open keeps its name."""
        path = os.path.abspath(path)
        with self.boards_lock:
            for name, known in self.boards.items():
                if same_file(known, path):
                    return name
            base = name = board_name(path)
            n = 2
            while name in self.boards or name == "api":
                name, n = f"{base}-{n}", n + 1
            self.boards[name] = path
        if save:
            self.save_state()
        return name

    def save_state(self):
        if not self.remember:
            return
        with self.boards_lock:
            state = {"boards": list(self.boards.values()), "phone": bool(self.phone)}
        try:
            os.makedirs(os.path.dirname(self.remember), exist_ok=True)
            write_text_atomic(self.remember, json.dumps(state, indent=2, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"Could not remember the open boards in {self.remember}: {e}", file=sys.stderr)

    def restore(self):
        """Open the boards remembered from the last run that are still boards; return whether phones could connect then."""
        try:
            state = json.loads(read_text_file(self.remember))
        except (OSError, ValueError):
            return False
        boards = state.get("boards") if isinstance(state, dict) else None
        for path in boards if isinstance(boards, list) else []:
            try:
                if isinstance(path, str) and "tasks" in json.loads(read_text_file(path)):
                    load_board(path)
                    self.add_board(path, save=False)
            except (KanvasError, OSError, ValueError):
                continue
        self.save_state()
        return isinstance(state, dict) and state.get("phone") is True

    def board_path(self, name):
        with self.boards_lock:
            return self.boards.get(name)

    def start_assist(self, name, kind, task_id, fields, phase="draft", answers=()):
        """Starts the agents of a dialog's help in the background and returns its job."""
        path = self.board_path(name)
        if kind not in ASSIST_KINDS:
            raise KanvasError(f"Expected the kind of help to be one of: {', '.join(ASSIST_KINDS)}")
        if phase not in ASSIST_PHASES or (phase == "questions" and kind != "definition"):
            raise KanvasError(f"There is no {phase} phase for the {kind} help")
        if not shutil.which("claude"):
            raise KanvasError("Claude Code (claude) is not installed or not on PATH; the agents need it")
        task = find_task(load_board(path), task_id) if kind != "task" else None
        if kind == "plan" and not (isinstance(task.get("definition"), dict) and task["definition"].get("approved")):
            raise KanvasError(f"{task['id']} has no approved product definition yet")
        with self.assist_lock:
            if sum(1 for j in self.assist.values() if j["status"] == "running") >= ASSIST_MAX_RUNNING:
                raise AssistBusy(f"{ASSIST_MAX_RUNNING} agent conversations are already running; wait for one to finish")
            job = {"id": os.urandom(8).hex(), "board": name, "kind": kind, "status": "running",
                   "phase": phase, "working": ASSIST_AGENTS[kind][0], "messages": [], "result": None, "error": "", "cost": 0.0, "turns": 0}
            self.assist[job["id"]] = job
            for old in [k for k, j in self.assist.items() if j["status"] != "running"][:-ASSIST_KEEP]:
                del self.assist[old]
        threading.Thread(target=self._run_assist, args=(job, path, kind, task_id, fields, phase, answers), daemon=True).start()
        return job

    def start_telegram(self, path=None):
        """Starts (or restarts) the Telegram bot of the saved connection on this server, in the background."""
        if not self.opens_boards:
            return
        config = telegram_config()
        if not config.get("token"):
            return
        board = path or config.get("board")
        if not board or not os.path.isfile(board):
            with self.boards_lock:
                board = next(iter(self.boards.values()), None)
        if not board:
            return
        pairing = config.get("pairing") if isinstance(config.get("pairing"), dict) else None

        def paired(user_id):
            fresh = telegram_config()
            if fresh.get("token") and user_id not in fresh["users"]:
                fresh["users"].append(user_id)
            fresh["pairing"] = None
            save_telegram_config(fresh)

        say = (lambda m: None) if self.quiet else (lambda m: print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True))
        bridge = TelegramBridge(board, telegram_api(config["token"]), config["users"], say, pairing, paired)
        with self.telegram_lock:
            if self.telegram:
                self.telegram.stopping = True
            self.telegram = bridge
        threading.Thread(target=bridge.run, name="kanvas-telegram", daemon=True).start()

    def stop_telegram(self):
        with self.telegram_lock:
            if self.telegram:
                self.telegram.stopping = True
            self.telegram = None

    def telegram_status(self):
        config = telegram_config()
        pairing = config.get("pairing") if isinstance(config.get("pairing"), dict) else None
        live = pairing and time.time() < pairing["expires"]
        return {"connected": bool(config.get("token")), "bot": config.get("bot"), "users": len(config.get("users") or []),
                "code": pairing["code"] if live else None, "running": bool(self.telegram)}

    def start_inbox(self, name, text, task_id, source):
        """Records an idea or a reply of the owner and has the product owner answer it in the background."""
        path = self.board_path(name)
        if source not in ("web", "telegram", "terminal"):
            source = "web"
        if not shutil.which("claude"):
            raise KanvasError("Claude Code (claude) is not installed or not on PATH; the product owner needs it")
        with self.assist_lock:
            if sum(1 for j in self.inbox.values() if j["status"] == "running") >= ASSIST_MAX_RUNNING:
                raise AssistBusy("The product owner is already answering other ideas; wait a moment")
        task = inbox_submit(path, text, source, task_id)
        job = {"id": os.urandom(8).hex(), "board": name, "task": task, "status": "running", "result": None, "error": ""}
        with self.assist_lock:
            self.inbox[job["id"]] = job
            for old in [k for k, j in self.inbox.items() if j["status"] != "running"][:-ASSIST_KEEP]:
                del self.inbox[old]

        def run():
            try:
                job["result"] = inbox_po_turn(path, task)
                job["status"] = "done"
            except Exception as e:
                job["error"], job["status"] = str(e), "failed"
        threading.Thread(target=run, daemon=True).start()
        return job

    def inbox_status(self, name, job_id):
        with self.assist_lock:
            job = self.inbox.get(job_id)
            return dict(job) if job and job["board"] == name else None

    def assist_status(self, name, job_id):
        with self.assist_lock:
            job = self.assist.get(job_id)
            return dict(job, messages=list(job["messages"])) if job and job["board"] == name else None

    def _run_assist(self, job, path, kind, task_id, fields, phase="draft", answers=()):
        def change(**fresh):
            with self.assist_lock:
                job.update(fresh)

        try:
            board, root, settings = assist_context(path)
            config = automation_config(board, path)
            repositories = {r["id"]: r["name"] for r in config["repositories"].values()} if config else {}
            roles = config["agents"] if config else agent_roles(None)
            task = find_task(board, task_id) if kind != "task" else None
            said, data = [], None
            for step, agent in enumerate(ASSIST_AGENTS[kind][:1] if phase == "questions" else ASSIST_AGENTS[kind]):
                change(working=agent)
                prompt = assist_prompt(kind, step, board, task, fields, root, repositories, roles, said, phase, answers)
                reply, cost, turns = assist_step(agent, prompt, root, settings)
                prose, found = split_assist_reply(reply)
                said.append(prose)
                data = found if found is not None else data
                with self.assist_lock:
                    job["messages"].append({"agent": agent, "text": prose or "(no text)"})
                    job["cost"] += cost
                    job["turns"] += turns
            result = clean_assist(kind, data, board, list(repositories), list(roles), phase, answers)
            if result is None:
                raise KanvasError("The agents did not return a draft the board could use")
            change(status="done", working="", result=result)
        except subprocess.TimeoutExpired:
            change(status="failed", working="", error="An agent took too long and was stopped")
        except (KanvasError, OSError, ValueError) as e:
            change(status="failed", working="", error=str(e))
        except Exception as e:
            change(status="failed", working="", error=f"{type(e).__name__}: {e}")

    def board_list(self):
        with self.boards_lock:
            boards = list(self.boards.items())
        out = []
        for name, path in boards:
            try:
                title = load_board(path)["title"] or name
            except (KanvasError, OSError):
                title = name
            out.append({"name": name, "title": title, "path": path})
        return out


# ---------------------------------------------------------------------------
# Help from the agents in the dialogs: the product owner for a new task and for its product definition, and the
# architect and the senior developer, talking, for its construction plan. They only read the project and draft the
# form; the human edits the draft and approves.

ASSIST_KINDS = ("task", "definition", "plan")
# Each help runs in up to two phases: "questions" asks the owner what is missing and waits for the answers, "draft" writes the
# form once they are given. The task help has no questions phase.
ASSIST_PHASES = ("questions", "draft")
# How urgent a task is, asked of the owner while the product owner refines it and kept in its product definition.
PRIORITIES = {"urgent": "Urgent", "high": "High", "normal": "Normal", "low": "Low"}
# Who speaks in each kind of help. The plan is a conversation: the architect proposes, the senior developer checks the
# proposal against the code, and the architect settles it with the final plan.
ASSIST_AGENTS = {"task": ("product-owner",), "definition": ("product-owner",),
                 "plan": ("architect", "senior-dev", "architect")}
ASSIST_TOOLS = ("Read", "Glob", "Grep")
ASSIST_MAX_RUNNING = 2
ASSIST_KEEP = 20
ASSIST_STEP_TIMEOUT = 300


class AssistBusy(KanvasError):
    pass


def assist_settings(value):
    """`automation.assist`: the limits of every step an agent takes in a dialog."""
    if value is None:
        return {"max_turns": 10, "max_budget_usd": None}
    if not isinstance(value, dict):
        raise KanvasError("automation.assist must be an object")
    return {"max_turns": _positive_int(value.get("max_turns"), 10, "automation.assist.max_turns"),
            "max_budget_usd": value.get("max_budget_usd")}


def assist_context(path):
    """The board, the project folder the agents read and their limits: those of `automation`, or the board's folder
    and the defaults when automation is off."""
    board = load_board(path)
    config = board.get("automation") if isinstance(board.get("automation"), dict) else {}
    root = os.path.abspath(os.path.join(os.path.dirname(path), config.get("project_root") or "."))
    return board, root, assist_settings(config.get("assist"))


def _clip(value, limit=4000):
    return value.strip()[:limit] if isinstance(value, str) else ""


def split_assist_reply(text):
    """The prose of an agent's reply and the JSON object that ends it, which is None when there is none."""
    found = list(re.finditer(r"```json\s*(.*?)```", text, re.S))
    if not found:
        return text.strip(), None
    last = found[-1]
    try:
        data = json.loads(last.group(1))
    except ValueError:
        data = None
    return (text[:last.start()] + text[last.end():]).strip(), data


def clean_answers(value):
    """The owner's answers to the agent's questions as [{"id", "q", "a"}], with every value clipped; unanswered ones are dropped."""
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and isinstance(item.get("a"), str) and item["a"].strip():
            out.append({"id": _clip(item.get("id"), 40), "q": _clip(item.get("q"), 400), "a": _clip(item["a"], 1500)})
    return out[:12]


def priority_question():
    """The question about urgency that Factory always asks, whatever else the product owner asks."""
    return {"id": "priority", "text": "How urgent is this?", "options": [PRIORITIES[k] for k in PRIORITIES], "default": PRIORITIES["normal"]}


def priority_of(answers):
    """The priority key the owner chose among their answers, or "" when they did not say."""
    chosen = next((a["a"].strip().lower() for a in answers if a.get("id") == "priority"), "")
    return next((k for k, v in PRIORITIES.items() if chosen in (k, v.lower())), "")


def clean_questions(data):
    """The questions of the product owner as [{"id", "text", "options"}], the urgency question first. An agent with nothing to
    ask still gets the urgency question, so the owner always has one thing to answer."""
    asked = []
    for q in (data.get("questions") if isinstance(data, dict) else None) or []:
        text = _clip(q.get("text") if isinstance(q, dict) else q, 400)
        if not text:
            continue
        options = [_clip(o, 120) for o in (q.get("options") if isinstance(q, dict) else None) or [] if isinstance(o, str) and o.strip()][:5]
        asked.append({"id": f"q{len(asked) + 1}", "text": text, "options": options})
    return [priority_question(), *asked[:6]]


def clean_assist(kind, data, board, repository_ids, role_names, phase="draft", answers=()):
    """The part of an agent's JSON the page can use, with every value checked; None when there is no draft."""
    if kind == "definition" and phase == "questions":
        return {"questions": clean_questions(data)} if isinstance(data, dict) else None
    if not isinstance(data, dict):
        return None
    if kind == "definition":
        questions = [_clip(q, 400) for q in data.get("questions") or [] if isinstance(q, str) and q.strip()][:8]
    else:
        # A task or a plan asks with likely answers the owner can tap; the page adds the box for any other answer.
        questions = clean_questions(data)[1:]
    out = {"questions": questions}
    draft = data.get("draft") if isinstance(data.get("draft"), dict) else data
    if kind == "task":
        groups = {g["id"] for g in board["groups"]}
        known = {t["id"] for t in board["tasks"]}
        out["draft"] = {"title": _clip(draft.get("title"), 200), "desc": _clip(draft.get("desc")),
                        "group": draft.get("group") if draft.get("group") in groups else ""}
        out["duplicates"] = [d for d in data.get("duplicates") or [] if isinstance(d, str) and d in known][:5]
        return out if out["draft"]["title"] else None
    if kind == "definition":
        agent = draft.get("agent")
        out["draft"] = {**{k: _clip(draft.get(k)) for k in ("outcome", "needs", "acceptance", "non_goals")},
                        "repositories": [r for r in draft.get("repositories") or [] if r in repository_ids],
                        "agent": agent if agent in role_names and not str(agent).startswith(("reviewer", "curator")) else DEFAULT_AGENT,
                        "priority": priority_of(answers)}
        return out if out["draft"]["outcome"] else None
    out["draft"] = {"plan": _clip(draft.get("plan"), 8000)}
    return out if out["draft"]["plan"] else None


def assist_prompt(kind, step, board, task, fields, root, repositories, roles, earlier, phase="draft", answers=()):
    """What one agent is asked in one step of a dialog's help. Every step starts a new session, so each prompt brings
    what that agent needs."""
    head = (f"You help the owner of the Factory board \"{board['title']}\" fill in a dialog. The project is in {root}: read "
            "what you need there (the documentation in the brain repository, modules and adr, and the repositories) and change nothing. Be brief and "
            "concrete, ask only what changes what gets built, and answer in the language the owner writes in.\n\n")
    ids = lambda: ", ".join(f"{k}: {v}" for k, v in repositories.items()) or "none"
    heard = "".join(f"- {a['q'] or a['id']}\n  Answer: {a['a']}\n" for a in answers)
    if kind == "task":
        groups = "\n".join(f"- {g['id']}: {g['name']}" for g in board["groups"]) or "(none)"
        open_tasks = [t for t in board["tasks"] if t["state"] not in ("develop", "done", "production")]
        listed = "\n".join(f"- {t['id']} [{t['state']}] {t['title']}" for t in open_tasks[:80]) or "(none)"
        return head + (
            f"The owner is creating a task and has written:\nTitle: {fields.get('title') or '(empty)'}\n"
            f"Description: {fields.get('desc') or '(empty)'}\n\nGroups:\n{groups}\n\nOpen tasks:\n{listed}\n\n"
            + (f"The owner answered your last questions:\n{heard}\nTake the answers as decided.\n\n" if heard else "") +
            "Turn it into a good task: a short title that says what changes, and a description in 2 to 5 lines of "
            "markdown that says what it achieves and what finished means. Choose the group that fits, or an empty one "
            "if none does, and list the open tasks it duplicates. Ask at most 5 questions.\n\n"
            "End your reply with one fenced json block and nothing after it:\n"
            "```json\n{\"title\": \"...\", \"group\": \"<group id or empty>\", \"desc\": \"...\", "
            "\"duplicates\": [\"<task id>\"], \"questions\": [{\"text\": \"...\", \"options\": [\"...\"]}]}\n```\n")
    definition = task.get("definition") if isinstance(task.get("definition"), dict) else {}
    notes = "\n".join(f"- {_clip(n.get('text'), 300)}" for n in (task.get("notes") or [])[-3:]) or "(none)"
    about = f"task {task['id']}: {task['title']}\nDescription: {_clip(task.get('desc'), 1500) or '(none)'}\nRecent notes:\n{notes}\n"
    if kind == "definition" and phase == "questions":
        return head + (
            f"The owner is defining the product need of {about}\nWhat the form holds so far:\n"
            f"Outcome: {fields.get('outcome') or '(empty)'}\nNeeds and constraints: {fields.get('needs') or '(empty)'}\n"
            f"Acceptance criteria: {fields.get('acceptance') or '(empty)'}\nNon-goals: {fields.get('non_goals') or '(empty)'}\n\n"
            "Look in the brain repository (modules and adr) for what already exists about this. Do not draft the definition "
            "yet: first ask the owner what is missing or ambiguous, so that their answers let you write it. Ask between 2 and 6 "
            "short questions, one thing each, and only what changes what gets built. When a question has a few likely answers, "
            "list them as options. Do not ask how urgent it is: Factory asks that itself.\n\n"
            "End your reply with one fenced json block and nothing after it:\n```json\n{\"questions\": "
            "[{\"text\": \"...\", \"options\": [\"...\"]}]}\n```\n")
    if kind == "definition":
        return head + (
            f"The owner is defining the product need of {about}\nWhat the form holds so far:\n"
            f"Outcome: {fields.get('outcome') or '(empty)'}\nNeeds and constraints: {fields.get('needs') or '(empty)'}\n"
            f"Acceptance criteria: {fields.get('acceptance') or '(empty)'}\nNon-goals: {fields.get('non_goals') or '(empty)'}\n\n"
            + (f"The owner answered your questions:\n{heard}\nReformulate the definition with those answers; ask again only what "
               "is still open.\n\n" if heard else "") +
            f"Repositories (id: name): {ids()}\n"
            f"Agent roles: {', '.join(r for r in roles if not r.startswith('reviewer'))}. The implementer is the default; choose "
            "complex only for security, multi-club isolation, permissions or data migrations.\n\n"
            "Look in the brain repository (modules and adr) for what already exists about this, then question what is missing or "
            "ambiguous and draft the form: the outcome for users or the business, needs and constraints, acceptance "
            "criteria a person can check, non-goals, the repositories the change may touch and the agent role.\n\n"
            "End your reply with one fenced json block and nothing after it:\n```json\n{\"questions\": [\"...\"], "
            "\"draft\": {\"outcome\": \"...\", \"needs\": \"...\", \"acceptance\": \"...\", \"non_goals\": \"...\", "
            "\"repositories\": [\"<repository id>\"], \"agent\": \"implementer\"}}\n```\n")
    spec = (f"{about}\nProduct definition:\nOutcome: {definition.get('outcome') or '(none)'}\n"
            f"Needs and constraints: {definition.get('needs') or '(none)'}\n"
            f"Acceptance criteria: {definition.get('acceptance') or '(none)'}\n"
            f"Non-goals: {definition.get('non_goals') or '(none)'}\n"
            f"Repositories it may change: {', '.join(definition.get('repositories') or []) or 'none chosen'} "
            f"(available: {ids()})\nThe owner's plan so far: {fields.get('plan') or '(empty)'}\n\n"
            + (f"The owner answered the questions of the last round:\n{heard}\nTake the answers as decided and ask again "
               "only what is still open.\n\n" if heard else ""))
    if step == 0:
        return head + ("The architect speaks first. " + spec +
                       "Propose the technical construction plan, grounded in the real documentation and code. Reply in plain "
                       "text, up to 350 words: the plan by repository and layer, data and API, isolation and security, "
                       "tests, rollout and rollback, and your assumptions and open questions. The senior developer reviews "
                       "your proposal next.\n")
    if step == 1:
        return head + ("The senior developer speaks second. " + spec + f"The architect proposed:\n{earlier[0]}\n\n"
                       "Check it against the real code. Correct what is wrong, name what the architect missed (risks, "
                       "files, reuse, mobile, tests) and what you would cut. Reply in plain text, up to 300 words, to the "
                       "architect.\n")
    return head + ("The architect speaks last. " + spec + f"Your proposal was:\n{earlier[0]}\n\nThe senior developer answered:\n"
                   f"{earlier[1]}\n\nSettle it: write the final construction plan, taking what is right in the review, and "
                   "the questions only the owner can answer.\n\nEnd your reply with one fenced json block and nothing "
                   "after it:\n```json\n{\"questions\": [{\"text\": \"...\", \"options\": [\"...\"]}], \"draft\": {\"plan\": \"<the final plan, in plain text>\"}}\n```\n"
                   "The plan must be complete on its own: for every open question take your best option. Each question must be "
                   "answerable by tapping, with 2 to 4 short options and the one you recommend first. Ask only what reading the "
                   "code and the documentation could not settle; never ask the owner what you can look up.\n")


def assist_step(agent, prompt, root, settings):
    """Runs one agent for one step, read only, and returns its reply, cost and turns."""
    command = [shutil.which("claude") or "claude", "-p", "--output-format", "json", "--agent", agent,
               "--no-session-persistence", "--permission-mode", "acceptEdits", "--strict-mcp-config",
               "--setting-sources", "project,local", "--tools", ",".join(ASSIST_TOOLS),
               "--max-turns", str(settings["max_turns"])]
    if settings["max_budget_usd"]:
        command += ["--max-budget-usd", str(settings["max_budget_usd"])]
    command += ["--allowedTools", *ASSIST_TOOLS]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(command, input=prompt, text=True, encoding="utf-8", errors="replace", capture_output=True,
                          cwd=root, env=agent_environment(), timeout=ASSIST_STEP_TIMEOUT, creationflags=flags)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    if isinstance(data, list):
        data = next((e for e in reversed(data) if isinstance(e, dict) and e.get("type") == "result"), None)
    if not isinstance(data, dict) or proc.returncode or data.get("is_error"):
        detail = (data.get("result") if isinstance(data, dict) else None) or proc.stderr.strip() or proc.stdout.strip()
        raise KanvasError(f"{agent} could not answer: {_clip(detail, 300) or 'no output'}")
    return str(data.get("result") or ""), float(data.get("total_cost_usd") or 0), int(data.get("num_turns") or 0)


def evidence_root(path):
    """The folder with one subfolder of evidence per task: `automation.evidence_root`, or `.kanvas/evidence` in the project
    folder. It does not need automation to be on, so evidence added by hand shows too."""
    try:
        config = load_board(path).get("automation")
    except (KanvasError, OSError):
        return None
    config = config if isinstance(config, dict) else {}
    root = os.path.abspath(os.path.join(os.path.dirname(path), config.get("project_root") or "."))
    return os.path.abspath(os.path.join(root, config.get("evidence_root") or ".kanvas/evidence"))


def evidence_folder(root, task_id):
    """The folder of one task's evidence under `root`, or None for an ID that could lead anywhere else."""
    if not root or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", task_id or ""):
        return None
    return os.path.join(root, task_id)


def evidence_files(folder):
    """The screenshots and videos of one task as [{"path", "kind", "size"}], a few folders deep and in name order.
    Hidden files and links are left out."""
    found = []
    if not folder or not os.path.isdir(folder):
        return found
    start = folder.rstrip(os.sep).count(os.sep)
    for here, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if here.rstrip(os.sep).count(os.sep) - start >= EVIDENCE_MAX_DEPTH:
            dirs[:] = []
        for name in sorted(names):
            kind = EVIDENCE_TYPES.get(os.path.splitext(name)[1].lower())
            full = os.path.join(here, name)
            if kind and not name.startswith(".") and os.path.isfile(full) and not os.path.islink(full):
                found.append({"path": os.path.relpath(full, folder).replace(os.sep, "/"), "kind": kind[0], "size": os.path.getsize(full)})
                if len(found) >= EVIDENCE_MAX_FILES:
                    return found
    return found


def evidence_file(folder, parts):
    """(real path, content type) of the screenshot or video at `parts` inside a task's folder, or None when that is
    anything else: another kind of file, a hidden or odd name, or a path that leaves the folder."""
    if not folder or not parts:
        return None
    if any(not p or p.startswith(".") or "\\" in p or ":" in p or "\0" in p for p in parts):
        return None
    kind = EVIDENCE_TYPES.get(os.path.splitext(parts[-1])[1].lower())
    if not kind:
        return None
    real, base = os.path.realpath(os.path.join(folder, *parts)), os.path.realpath(folder)
    try:
        inside = os.path.commonpath([real, base]) == base
    except ValueError:
        return None
    return (real, kind[1]) if inside and os.path.isfile(real) else None


class BoardHandler(BaseHTTPRequestHandler):
    server_version = "Factory/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        self._skip_body()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, json.dumps({"error": message}).encode("utf-8"))

    def _skip_body(self):
        """Reads a request body that the answer leaves unread. Closing the connection with the body unread makes
        Windows reset it, and the client can lose the answer."""
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= MAX_UPLOAD:
            self.rfile.read(length)

    def _redirect(self, location):
        self._send(302, headers={"Location": location})

    def _host_name(self):
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
        return name.rstrip(".")

    def _host_allowed(self):
        # Rejecting foreign Host headers blocks DNS-rebinding attacks from web pages.
        return not self.server.check_host or self._host_name() in self.server.hosts

    def _from_this_computer(self):
        """Whether the request comes from this computer by one of its local names, with no proxy between.
        Phones, other devices and `tailscale serve`, which passes requests on by this computer's Tailscale
        name and adds X-Forwarded headers, do not."""
        try:
            loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False
        forwarded = any(self.headers.get(h) for h in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host"))
        return loopback and not forwarded and self._host_name() in LOCAL_NAMES

    def _inbox_allowed(self):
        """The inbox costs tokens and opens ideas on the board, so it answers this computer and the phones that were paired
        with a token of their own, not every device that can reach Factory."""
        return self._from_this_computer() or inbox_token_ok(self.headers.get("X-Kanvas-Token"))

    def _may_manage(self):
        """Whether the request may open boards and open or close Factory to phones, which stay with
        the computer: only the main server does that, and only for this computer."""
        return self.server.opens_boards and self._from_this_computer()

    def _board_list(self):
        """The open boards, with where each one lives for this computer only."""
        boards = self.server.board_list()
        if self._from_this_computer():
            return boards
        return [{"name": b["name"], "title": b["title"]} for b in boards]

    def _route(self):
        """The request path as a list of parts: "/a/b/" gives ["a", "b", ""]."""
        return unquote(urlparse(self.path).path).split("/")[1:]

    def _read_json(self):
        """The request body, or None after answering with an error."""
        # Requiring JSON forces a CORS preflight, so other sites cannot write.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(415, "Expected application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._error(400, "Expected the length of the body in bytes in Content-Length")
        if length > MAX_UPLOAD:
            return self._error(413, "Too large")
        self._body_read = True
        try:
            return self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError:
            return self._error(400, "Expected UTF-8")

    def do_GET(self):
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        parts = self._route()
        if parts in ([""], ["index.html"]):
            boards = self._board_list()
            if len(boards) == 1:
                return self._redirect(f"/{quote(boards[0]['name'])}/")
            links = "".join(f'<a class="board" href="/{quote(b["name"])}/">{escape(b["title"])}'
                            + (f'<small>{escape(b["path"])}</small>' if "path" in b else "") + "</a>" for b in boards)
            body = (f"<h1>Factory</h1>{links}" if boards else "<h1>Factory</h1><p>No boards are open. Run <code>kanvas serve</code> in a project to open its board here.</p>")
            if boards:
                body += "<p><small>Run <code>kanvas serve</code> in a project to open its board here.</small></p>"
            return self._send(200, page("Factory", body), "text/html; charset=utf-8")
        if parts == ["api", "boards"]:
            info = {"kanvas": VERSION, "boards": self._board_list()}
            return self._send(200, json.dumps(info, ensure_ascii=False).encode("utf-8"))
        name, rest = parts[0], parts[1:]
        path = self.server.board_path(name)
        if path is None:
            if rest[:1] == ["api"]:
                return self._error(404, f"No board called {name} is open here; run `kanvas serve` in its project")
            body = (f"<h1>No board called {escape(name)} is open</h1><p>Run <code>kanvas serve</code> in its project, then reload this page."
                    ' If it was open here before, its project folder may have been moved or renamed.</p><p><a href="/">Open boards</a></p>')
            return self._send(404, page("Factory", body), "text/html; charset=utf-8")
        if not rest:
            return self._redirect(f"/{quote(name)}/")
        if rest == ["api", "phone"]:
            return self._phone(name)
        if rest in ([""], ["index.html"]):
            try:
                with open(HTML_FILE, "rb") as f:
                    body = f.read()
            except OSError:
                return self._error(500, "kanvas.html is missing next to kanvas.py")
            return self._send(200, body, "text/html; charset=utf-8")
        if rest == ["api", "board"]:
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                return self._error(404, "Board file is missing")
            tag = etag_of(data)
            if self.headers.get("If-None-Match") == tag:
                return self._send(304, headers={"ETag": tag})
            return self._send(200, data, headers={"ETag": tag})
        if len(rest) == 3 and rest[:2] == ["api", "evidence"]:
            files = evidence_files(evidence_folder(evidence_root(path), rest[2]))
            return self._send(200, json.dumps({"files": files}).encode("utf-8"))
        if len(rest) >= 3 and rest[0] == "evidence":
            return self._evidence(path, rest[1], rest[2:])
        if rest == ["api", "telegram"]:
            if not self._may_manage():
                return self._error(403, "Telegram is connected only from the computer running Factory")
            return self._send(200, json.dumps(self.server.main.telegram_status()).encode("utf-8"))
        if rest in (["inbox"], ["inbox", ""]):
            return self._send(200, INBOX_PAGE.encode("utf-8"), "text/html; charset=utf-8", {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
        if rest[:2] == ["api", "inbox"]:
            if not self._inbox_allowed():
                return self._error(403, "The inbox answers this computer, or a phone paired with `kanvas inbox --pair`")
            if rest == ["api", "inbox"]:
                items = [inbox_summary(t) for t in inbox_tasks(load_board(path))]
                return self._send(200, json.dumps({"items": items}, ensure_ascii=False).encode("utf-8"))
            if len(rest) == 4 and rest[2] == "job":
                job = self.server.inbox_status(name, rest[3])
                if job is None:
                    return self._error(404, "Not found")
                return self._send(200, json.dumps({k: job[k] for k in ("status", "task", "result", "error")}, ensure_ascii=False).encode("utf-8"))
            return self._error(404, "Not found")
        if len(rest) == 3 and rest[:2] == ["api", "design"]:
            try:
                design = design_of(find_task(load_board(path), rest[2]))
                wanted = parse_qs(urlparse(self.path).query).get("round", [""])[0]
                mockup = design.get("mockup")
                if wanted.isdigit():
                    mockup = next((x.get("mockup") for x in design_history(design)
                                   if x.get("type") == "proposal" and x.get("round") == int(wanted)), None)
            except KanvasError:
                mockup = None
            if not isinstance(mockup, str) or not mockup:
                return self._error(404, "Not found")
            return self._send(200, mockup.encode("utf-8"), "text/html; charset=utf-8", MOCKUP_HEADERS)
        if len(rest) == 3 and rest[:2] == ["api", "assist"]:
            job = self.server.assist_status(name, rest[2])
            if job is None:
                return self._error(404, "Not found")
            return self._send(200, json.dumps(job, ensure_ascii=False).encode("utf-8"))
        return self._error(404, "Not found")

    def _evidence(self, board, task_id, parts):
        """One screenshot or video of a task. Videos need byte ranges so that the browser can skip through them."""
        found = evidence_file(evidence_folder(evidence_root(board), task_id), parts)
        if not found:
            return self._error(404, "Not found")
        file, ctype = found
        try:
            size = os.path.getsize(file)
            if size > EVIDENCE_MAX_BYTES:
                return self._error(413, "Too large")
            with open(file, "rb") as f:
                data = f.read()
        except OSError:
            return self._error(404, "Not found")
        headers = {"Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff"}
        wanted = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if wanted and (wanted.group(1) or wanted.group(2)):
            first, last = wanted.groups()
            if first:
                start, end = int(first), min(int(last) if last else size - 1, size - 1)
            else:
                start, end = max(size - int(last), 0), size - 1
            if start > end or start >= size:
                return self._send(416, headers={"Content-Range": f"bytes */{size}"})
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            return self._send(206, data[start:end + 1], ctype, headers)
        return self._send(200, data, ctype, headers)

    def _phone(self, name):
        """The board's addresses for phones on Tailscale, with a QR code of the first; none while closed to phones.
        `manage` says whether this request may open or close Factory to phones."""
        urls = [f"{base}{quote(name)}/" for base in self.server.main.phone]
        info = {"urls": urls, "qr": qr_svg(urls[0]) if urls else None, "manage": self._may_manage()}
        self._send(200, json.dumps(info).encode("utf-8"))

    def do_POST(self):
        """Opens another project's board here, for `kanvas serve` in that project, or opens the boards to phones."""
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        parts = self._route()
        if parts[1:] == ["api", "phone"] and self.server.board_path(parts[0]) is not None:
            if not self._may_manage():
                return self._error(403, "Only the computer running Factory can open it to phones")
            if self._read_json() is None:
                return
            try:
                fresh = not self.server.phone
                urls = self.server.open_to_phone()
            except KanvasError as e:
                return self._error(503, str(e))
            if fresh and not self.server.quiet:
                print(f"[{time.strftime('%H:%M:%S')}] opened to phones on Tailscale: {'  or  '.join(urls)}", flush=True)
            return self._phone(parts[0])
        if parts[1:] == ["api", "telegram"] and self.server.board_path(parts[0]) is not None:
            if not self._may_manage():
                return self._error(403, "Telegram is connected only from the computer running Factory")
            text = self._read_json()
            if text is None:
                return
            main = self.server.main
            try:
                request = json.loads(text)
                action = request.get("action")
                config = telegram_config()
                if action == "connect":
                    token = str(request.get("token") or "").strip()
                    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
                        raise KanvasError("That does not look like a bot token: it is like 123456:ABC-DEF…, from BotFather")
                    me = telegram_api(token)("getMe", timeout=15)
                    same = config.get("token") == token
                    save_telegram_config({"token": token, "bot": me.get("username"), "board": self.server.board_path(parts[0]),
                                          "users": config.get("users", []) if same else [], "pairing": new_pairing()})
                    main.start_telegram()
                elif action == "pair":
                    if not config.get("token"):
                        raise KanvasError("Connect a bot first")
                    config["pairing"] = new_pairing()
                    save_telegram_config(config)
                    main.start_telegram()
                elif action == "disconnect":
                    main.stop_telegram()
                    try:
                        os.remove(telegram_file())
                    except OSError:
                        pass
                else:
                    raise KanvasError("Expected connect, pair or disconnect")
            except (KanvasError, ValueError, AttributeError) as e:
                return self._error(400, str(e) or "Expected a JSON object")
            return self._send(200, json.dumps(main.telegram_status()).encode("utf-8"))
        if parts[1:3] == ["api", "inbox"] and self.server.board_path(parts[0]) is not None:
            if not self._inbox_allowed():
                return self._error(403, "The inbox answers this computer, or a phone paired with `kanvas inbox --pair`")
            text = self._read_json()
            if text is None:
                return
            try:
                request = json.loads(text)
                if len(parts) == 3:
                    job = self.server.start_inbox(parts[0], str(request.get("text") or ""), request.get("task") or None, request.get("source"))
                    return self._send(202, json.dumps({"job": job["id"], "task": job["task"]}).encode("utf-8"))
                if len(parts) == 5 and parts[4] in ("approve", "drop"):
                    path = self.server.board_path(parts[0])
                    if parts[4] == "approve":
                        inbox_approve(path, parts[3], "the inbox page")
                    else:
                        inbox_drop(path, parts[3])
                    return self._send(200, b"{}")
            except AssistBusy as e:
                return self._error(429, str(e))
            except (KanvasError, ValueError, AttributeError) as e:
                return self._error(400, str(e) or "Expected a JSON object")
            return self._error(404, "Not found")
        if parts[1:] == ["api", "assist"] and self.server.board_path(parts[0]) is not None:
            if not self._from_this_computer():
                return self._error(403, "The agents answer only requests from the computer running Factory")
            text = self._read_json()
            if text is None:
                return
            try:
                request = json.loads(text)
                fields = {str(k): _clip(v) for k, v in (request.get("fields") or {}).items() if isinstance(v, str)}
                job = self.server.start_assist(parts[0], request.get("kind"), request.get("id"), fields,
                                               request.get("phase") or "draft", clean_answers(request.get("answers")))
            except AssistBusy as e:
                return self._error(429, str(e))
            except (KanvasError, ValueError, AttributeError) as e:
                return self._error(400, str(e) or "Expected a JSON object")
            return self._send(202, json.dumps({"job": job["id"]}).encode("utf-8"))
        if parts != ["api", "boards"]:
            return self._error(404, "Not found")
        if not self._may_manage():
            return self._error(403, "Boards open only from the computer running Factory")
        text = self._read_json()
        if text is None:
            return
        try:
            path = json.loads(text).get("path")
            if not isinstance(path, str) or not os.path.isabs(path):
                raise KanvasError("Expected the board's absolute path")
            # A board always has its task list, so another JSON file is not taken for one.
            if "tasks" not in json.loads(read_text_file(path)):
                raise KanvasError(f"{path} is not a Factory board")
            load_board(path)
        except (KanvasError, ValueError, AttributeError, OSError) as e:
            return self._error(400, str(e))
        name = self.server.add_board(path)
        if not self.server.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] opened {name}: {path}", flush=True)
        info = {"name": name, "url": f"/{quote(name)}/"}
        if self.server.phone:
            info["phone"] = [f"{base}{quote(name)}/" for base in self.server.phone]
        self._send(200, json.dumps(info, ensure_ascii=False).encode("utf-8"))

    def do_DELETE(self):
        """Closes Factory to phones again."""
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        parts = self._route()
        if parts[1:] != ["api", "phone"] or self.server.board_path(parts[0]) is None:
            return self._error(404, "Not found")
        if not self._may_manage():
            return self._error(403, "Only the computer running Factory can close it to phones")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(415, "Expected application/json")
        if self.server.phone:
            self.server.close_to_phone()
            if not self.server.quiet:
                print(f"[{time.strftime('%H:%M:%S')}] closed to phones", flush=True)
        self._phone(parts[0])

    def do_PUT(self):
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        parts = self._route()
        path = self.server.board_path(parts[0]) if parts[1:] == ["api", "board"] else None
        if path is None:
            return self._error(404, "Not found")
        text = self._read_json()
        if text is None:
            return
        expected = self.headers.get("If-Match")
        if not expected:
            return self._error(428, "Expected If-Match with the ETag of the board this changes; load the board first")
        try:
            board = parse_board(text)
            # parse_board fills in a missing task list, which would empty the board.
            if "tasks" not in json.loads(text):
                raise KanvasError("Expected a board with its task list")
        except KanvasError as e:
            return self._error(400, str(e))
        errors = structural_errors(board)
        if errors:
            return self._error(400, "; ".join(errors))
        data = dumps(board).encode("utf-8")
        try:
            with locked(path):
                with open(path, "rb") as f:
                    current = f.read()
                current_tag = etag_of(current)
                if expected != current_tag:
                    return self._send(409, current, headers={"ETag": current_tag})
                if data != current:
                    write_text_atomic(path, data.decode("utf-8"))
                    if not self.server.quiet:
                        print(f"[{time.strftime('%H:%M:%S')}] saved {parts[0]} from the browser", flush=True)
        except (KanvasError, OSError) as e:
            # The reason names the board's folder or its lock file.
            return self._error(503, str(e) if self._from_this_computer() else "The computer running Factory could not save the board")
        self._send(200, data, headers={"ETag": etag_of(data)})


def start_server(path, host="127.0.0.1", port=DEFAULT_PORT, quiet=False, remember=None):
    """Serve the board at `path` (or none) on the first free port from `port` on. With `remember`,
    the boards open last time come back first, and `phone_wanted` says whether phones could connect."""
    hosts = LOCAL_NAMES if host in ("127.0.0.1", "localhost") else None
    last_error = None
    for p in range(port, port + 20):
        try:
            server = BoardServer((host, p), None, hosts, quiet, remember=remember)
            server.phone_wanted = server.restore() if remember else False
            if path:
                server.add_board(path)
            return server
        except OSError as e:
            busy = (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", -1), getattr(errno, "WSAEACCES", -1))
            if e.errno not in busy:
                raise
            last_error = e
    raise KanvasError(f"No free port between {port} and {port + 19}: {last_error}")


# Proxy settings must not send requests for this computer elsewhere.
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def port_free(host, port):
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_running(port, host="127.0.0.1"):
    """Return (url, version) of a Factory already serving boards on one of the ports `serve` tries, or None."""
    for p in range(port, port + 20):
        # A Factory may hold a later port after the ones below it were freed. Only taken ports are
        # probed, since connecting to a closed port takes seconds on Windows.
        if port_free(host, p):
            continue
        try:
            with LOCAL.open(f"http://{host}:{p}/api/boards", timeout=1) as r:
                info = json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(info, dict) and isinstance(info.get("kanvas"), str):
            return f"http://{host}:{p}/", info["kanvas"]
    return None


def join_running(url, path):
    """Open the board at `path` in the Factory running at `url`; return the board's URL there,
    and its URLs for phones on Tailscale (empty without Tailscale)."""
    body = json.dumps({"path": os.path.abspath(path)}).encode("utf-8")
    req = urllib.request.Request(url + "api/boards", body, {"Content-Type": "application/json"}, method="POST")
    try:
        with LOCAL.open(req, timeout=5) as r:
            info = json.loads(r.read().decode("utf-8"))
            return url + info["url"].lstrip("/"), info.get("phone") or []
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read().decode("utf-8"))["error"]
        except (ValueError, KeyError, OSError):
            message = str(e)
        raise KanvasError(f"The Factory running at {url} did not open the board: {message}")
    except (OSError, ValueError, KeyError) as e:
        raise KanvasError(f"The Factory running at {url} did not answer: {e}")


TAILSCALE_PATHS = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tailscale", "tailscale.exe"),
)


def tailscale_self():
    """This computer on Tailscale: (IPv4 address, DNS name or "", whether MagicDNS gives names)."""
    exe = shutil.which("tailscale") or next((p for p in TAILSCALE_PATHS if os.path.isfile(p)), None)
    if not exe:
        raise KanvasError("Tailscale is not installed here. Get it from https://tailscale.com/download, "
                          "sign in on this computer and on your phone, then run this again.")
    try:
        proc = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=15)
        status = json.loads(proc.stdout.decode("utf-8", "replace"))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise KanvasError("Tailscale did not say this computer's address. Is it running? Try `tailscale status`.")
    if status.get("BackendState") != "Running":
        raise KanvasError(f"Tailscale is not connected ({status.get('BackendState') or 'unknown state'}). "
                          "Connect it (`tailscale up`, or the Tailscale app), then run this again.")
    me = status.get("Self") or {}
    ips = [ip for ip in me.get("TailscaleIPs") or [] if "." in ip]
    if not ips:
        raise KanvasError("Tailscale gave this computer no IPv4 address; check `tailscale status`.")
    name = (me.get("DNSName") or "").rstrip(".").lower()
    magic = bool((status.get("CurrentTailnet") or {}).get("MagicDNSEnabled")) and bool(name)
    return ips[0], name, magic


def tailscale_names(ip, name):
    """What a phone may type to reach this computer: its address, full name and short name."""
    return {n for n in (ip, name, name.split(".")[0]) if n}


def phone_line(urls):
    return f"On your phone (Tailscale): {urls[0]}" + (f"  or  {urls[1]}" if len(urls) > 1 else "")


def print_phone(urls):
    print(phone_line(urls))
    if sys.stdout.isatty():
        print("Scan it with your phone's camera:")
        print(qr_text(urls[0]))
    print("Anyone on your Tailscale network can open and edit your boards.")


def offer_board():
    """Ask the person at the terminal to set up Factory; return the new board's path, or None."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    target, reason = project_folder()
    try:
        answer = input(f"No {BOARD_FILE} here. Create one in {target}? [Y/n] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if answer.strip().lower() not in ("", "y", "yes"):
        return None
    print()
    path = init_project(target, reason, next_steps=False)
    print()
    return path


def cmd_serve(args):
    if args.saved:
        path = None
    else:
        try:
            path = board_path(args)
        except KanvasError:
            if getattr(args, "board", None) or os.environ.get("KANVAS_BOARD"):
                raise
            path = offer_board()
            if not path:
                raise
    if not os.path.isfile(HTML_FILE):
        raise KanvasError("kanvas.html is missing next to kanvas.py")
    local = args.host in ("127.0.0.1", "localhost")
    if args.tailscale and not local:
        raise KanvasError("Use --tailscale or --host, not both: --tailscale serves on this computer and on its Tailscale address.")
    # One Factory serves the boards of every project; a second `serve` hands its board to it and returns.
    running = find_running(args.port, args.host) if args.port and local else None
    if running and not path:
        print(f"Factory already runs at {running[0]}")
        return
    if running:
        url, version = running
        board_url, phone_urls = join_running(url, path)
        print(f"Factory board: {path}")
        if hint := agent_copy_hint(path):
            print(hint)
        print(f"Opened in the Factory already running: {board_url}")
        if phone_urls:
            print_phone(phone_urls)
        elif args.tailscale:
            print("That Factory is not open to your phone. Stop it (Ctrl+C in its terminal) and run `kanvas serve --tailscale` there.")
        if version != VERSION:
            print(f"That Factory is {version} and this command is {VERSION}; to switch, stop it (Ctrl+C in its terminal) and run `kanvas serve` again.")
        if not args.no_open:
            webbrowser.open(board_url)
        return
    server = start_server(path, args.host, args.port, remember=state_file() if local else None)
    server.start_telegram()
    if args.tailscale:
        try:
            server.open_to_phone()
        except KanvasError:
            server.server_close()
            raise
    elif server.phone_wanted:
        server.open_to_phone_soon()
    host, port = server.server_address[:2]
    base_url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    if path:
        name = quote(server.add_board(path))
        board_url = f"{base_url}{name}/"
        print(f"Factory board: {path}")
        if hint := agent_copy_hint(path):
            print(hint)
    else:
        name, board_url = "", base_url
    others = [b["name"] for b in server.board_list() if b["name"] != unquote(name)]
    print(f"Open {board_url}  (Ctrl+C to stop)")
    if others:
        print(f"Also open from last time: {', '.join(others)}")
    if server.phone:
        print_phone([f"{base}{name}/" if name else base for base in server.phone])
    print("Boards of other projects open here too: run `kanvas serve` in them.")
    if not server.check_host:
        print("Warning: listening beyond localhost; anyone who can reach this port can edit the board.")
    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, (board_url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# Background service: Factory starts when you log in
# ---------------------------------------------------------------------------

SERVICE_NAME = "kanvas"  # systemd unit, launchd label and Windows task


def service_command():
    """The command a login service runs: this Factory, serving the boards open last time."""
    if INSTALLED and f"{os.sep}archive-v" in sys.prefix:
        raise KanvasError("This kanvas runs from uvx's temporary cache, which a service cannot rely on. "
                          "Install it with `uv tool install kanvas-board`, then run `kanvas service install`.")
    args = ["serve", "--saved", "--no-open"]
    if os.name == "nt":
        windowless = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        return [windowless if os.path.isfile(windowless) else sys.executable, os.path.abspath(__file__)] + args
    installed = shutil.which("kanvas") if INSTALLED else None
    return ([installed] if installed else [sys.executable, os.path.abspath(__file__)]) + args


def run_quiet(cmd):
    """Run a system command; return (exit code, its output)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()


def systemd_unit(cmd):
    quoted = " ".join('"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"' for a in cmd)
    return ("[Unit]\nDescription=Factory task boards\n\n"
            f"[Service]\nExecStart={quoted}\nEnvironment=PYTHONUNBUFFERED=1\nRestart=on-failure\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n")


def launchd_plist(cmd):
    import plistlib
    log = os.path.expanduser("~/Library/Logs/Kanvas.log")
    return plistlib.dumps({"Label": SERVICE_NAME, "ProgramArguments": cmd, "RunAtLoad": True,
                           "KeepAlive": {"SuccessfulExit": False}, "StandardOutPath": log, "StandardErrorPath": log})


def windows_task(cmd):
    from xml.sax.saxutils import escape as xml
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    args = subprocess.list2cmdline(cmd[1:])
    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            "<RegistrationInfo><Description>Factory task boards</Description></RegistrationInfo>"
            f"<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{xml(user)}</UserId></LogonTrigger></Triggers>"
            '<Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>'
            "<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"
            "<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>"
            f'<Actions Context="Author"><Exec><Command>{xml(cmd[0])}</Command><Arguments>{xml(args)}</Arguments></Exec></Actions></Task>')


def service_paths():
    if os.name == "nt":
        return None
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/LaunchAgents/{SERVICE_NAME}.plist")
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "systemd", "user", f"{SERVICE_NAME}.service")


def service_install(tailscale, wait=10):
    cmd = service_command()
    if tailscale:
        # The service opens to phones once Tailscale is up, as it would after opening from the board.
        state = state_file()
        try:
            current = json.loads(read_text_file(state))
        except (OSError, ValueError):
            current = {}
        current = current if isinstance(current, dict) else {}
        os.makedirs(os.path.dirname(state), exist_ok=True)
        write_text_atomic(state, json.dumps(dict(current, phone=True), indent=2, ensure_ascii=False) + "\n")
    path = service_paths()
    if os.name == "nt":
        xml_path = os.path.join(os.path.dirname(state_file()), "kanvas-task.xml")
        os.makedirs(os.path.dirname(xml_path), exist_ok=True)
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(windows_task(cmd))
        stop = ["schtasks", "/End", "/TN", SERVICE_NAME]
        steps = [["schtasks", "/Create", "/TN", SERVICE_NAME, "/XML", xml_path, "/F"], ["schtasks", "/Run", "/TN", SERVICE_NAME]]
        where = "a Task Scheduler task that runs when you log in"
    elif sys.platform == "darwin":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(launchd_plist(cmd))
        domain = f"gui/{os.getuid()}"
        stop = ["launchctl", "bootout", f"{domain}/{SERVICE_NAME}"]
        steps = [["launchctl", "bootstrap", domain, path]]
        where = path
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_text_atomic(path, systemd_unit(cmd))
        stop = ["systemctl", "--user", "stop", f"{SERVICE_NAME}.service"]
        steps = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"]]
        where = path
    # Once the service is stopped, a Factory that still answers was started with `kanvas serve`.
    # The service's own `serve --saved` then finds it and exits, and nothing restarts a clean exit.
    run_quiet(stop)
    by_hand = find_running(DEFAULT_PORT)
    for step in steps:
        code, out = run_quiet(step)
        if code:
            raise KanvasError(f"`{' '.join(step)}` failed: {out or f'exit code {code}'}")
    print(f"Factory now starts when you log in ({where}).")
    print("It serves the boards you open with `kanvas serve`, and remembers them across restarts.")
    if tailscale:
        print("It also opens them to your phone over Tailscale, as soon as Tailscale is connected.")
    if by_hand:
        again = "kanvas service install" + (" --tailscale" if tailscale else "")
        print(f"Factory already runs at {by_hand[0]}, started with `kanvas serve`; the service leaves your boards to it and stops.")
        print("Once that Factory stops, nothing serves them until you log in again. To hand them to the service now, "
              f"stop it (Ctrl+C in its terminal) and run `{again}` again.")
    else:
        deadline = time.monotonic() + wait
        running = find_running(DEFAULT_PORT)
        while not running and time.monotonic() < deadline:
            time.sleep(0.25)
            running = find_running(DEFAULT_PORT)
        print(f"Open {running[0]}" if running else "It has not answered yet; `kanvas service status` says when it runs.")
    if os.name != "nt" and sys.platform != "darwin":
        print(f"To start it at boot, before you log in: loginctl enable-linger {os.environ.get('USER') or '$USER'}")


def service_uninstall():
    path = service_paths()
    if os.name == "nt":
        run_quiet(["schtasks", "/End", "/TN", SERVICE_NAME])
        code, out = run_quiet(["schtasks", "/Delete", "/TN", SERVICE_NAME, "/F"])
        removed = code == 0
    elif sys.platform == "darwin":
        run_quiet(["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_NAME}"])
        removed = os.path.exists(path)
        if removed:
            os.remove(path)
    else:
        run_quiet(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"])
        removed = os.path.exists(path)
        if removed:
            os.remove(path)
        run_quiet(["systemctl", "--user", "daemon-reload"])
    print("Factory no longer starts when you log in." if removed else "Factory was not set to start when you log in.")


def service_status():
    path = service_paths()
    if os.name == "nt":
        installed = run_quiet(["schtasks", "/Query", "/TN", SERVICE_NAME])[0] == 0
    else:
        installed = os.path.exists(path)
    print("Starts when you log in: " + ("yes" if installed else "no (`kanvas service install` sets that up)"))
    running = find_running(DEFAULT_PORT)
    print(f"Running: yes, at {running[0]}" if running else "Running: no")
    try:
        state = json.loads(read_text_file(state_file()))
    except (OSError, ValueError):
        state = {}
    boards = state.get("boards") or [] if isinstance(state, dict) else []
    print(f"Remembered boards ({state_file()}):" if boards else "Remembered boards: none yet")
    for b in boards:
        print(f"  {b}")


def cmd_service(args):
    {"install": lambda: service_install(args.tailscale), "uninstall": service_uninstall, "status": service_status}[args.action]()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

EPILOG = """\
workflow:  proposed -> todo -> doing -> review -> develop -> done -> production
  Product definition (PO/TL) gates todo; a technical plan (Architect/Senior)
  gates doing. Independent agent and manual reviews gate the dev merge (develop),
  and main/deployment evidence gates production.
  A todo task with unfinished dependencies shows as blocked.
  An agent that needs the human's reply in the conversation runs `ask`
  first, so the board shows the task waiting for them until the next note.
  YOLO selects a dependency chain for autonomous attention: Factory writes the plans
  of its defined tasks by itself; it never bypasses definitions, designs, reviews,
  merges or production evidence.

examples:
  %(prog)s status
  %(prog)s start DV-02 -m "Plan: API, data, tests and rollout" --worktree .
  %(prog)s finish DV-02 -m "Implemented the parser; 14 tests pass"
  %(prog)s propose Development "Add retries" "Retry failed uploads" --deps DV-02
"""


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-b", "--board", default=argparse.SUPPRESS, metavar="PATH",
                        help=f"board file (default: nearest {BOARD_FILE})")
    common.add_argument("--as", dest="as_name", default=argparse.SUPPRESS, metavar="NAME",
                        help="name recorded on notes and proposals (default: $KANVAS_AGENT or 'agent')")

    prog = command_name() if INSTALLED else os.path.basename(sys.argv[0]) or "kanvas.py"
    parser = argparse.ArgumentParser(
        prog=prog, parents=[common], epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Factory: a task board shared by humans and AI agents.",
    )
    parser.add_argument("--version", action="version", version=f"Factory {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name, func, help_text, **kw):
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text, **kw)
        p.set_defaults(func=func)
        return p

    def text_sources(p, what):
        p.add_argument("--stdin", action="store_true", help=f"read the {what} from stdin (UTF-8)")
        p.add_argument("--file", metavar="PATH", help=f"read the {what} from a file")

    add("status", cmd_status, "board overview (default command)")
    p = add("list", cmd_list, "list tasks, optionally filtered by state or group")
    p.add_argument("filter", nargs="?", help="proposed, todo/ready, blocked, doing, review, develop, done, production, or a group")
    p = add("show", cmd_show, "task details, dependencies and notes")
    p.add_argument("id")
    add("ready", cmd_ready, "tasks that can be started now")
    add("blocked", cmd_blocked, "blocked tasks and what they wait on")
    add("check", cmd_check, "validate the board (exit code 1 on errors)")

    p = add("start", cmd_start, "start a ready task with a technical construction plan (todo -> doing)")
    p.add_argument("id")
    p.add_argument("-m", "--message", help="required architecture/senior construction plan")
    p.add_argument("--worktree", action="append", metavar="PATH",
                   help="Git worktree used for the task (repeatable; defaults to the current directory when it is in Git)")
    text_sources(p, "technical plan")
    p = add("finish", cmd_finish, "hand a task over for independent agent and manual review (doing -> review); needs a summary")
    p.add_argument("id")
    p.add_argument("-m", "--message", help="summary of what you did")
    text_sources(p, "summary")
    p = add("agent-review", cmd_agent_review, "independently approve a reviewed task or request changes")
    p.add_argument("id")
    verdict = p.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--approve", action="store_true", help="approve the implementation")
    verdict.add_argument("--changes", action="store_true", help="request changes and return it to doing")
    p.add_argument("-m", "--message", help="review findings or approval summary")
    text_sources(p, "review summary")
    p = add("pause", cmd_pause, "stop working on a task (doing -> todo)")
    p.add_argument("id")
    p.add_argument("-m", "--message", help="optional note on why")
    text_sources(p, "note")
    p = add("retry", cmd_retry, "retry failed automation in its existing isolated worktree")
    p.add_argument("id")
    p = add("inbox", cmd_inbox, "send an idea or an incident to the product owner, who turns it into a task; with no text, list the inbox")
    p.add_argument("text", nargs="?", help="the idea or the incident, as you would say it")
    p.add_argument("--task", metavar="ID", help="answer the product owner on this idea instead of starting a new one")
    p.add_argument("--approve", metavar="ID", help="approve the draft definition of an idea, which makes it To do")
    p.add_argument("--drop", metavar="ID", help="throw an idea away")
    p.add_argument("--pair", action="store_true", help="print the address that pairs a phone with the inbox (run it in your own terminal)")
    p = add("telegram", cmd_telegram, "listen to a private Telegram bot: what you write is an idea or an incident for the inbox")
    p.add_argument("--once", action="store_true", help="handle what is waiting and stop")
    p = add("mcp", cmd_mcp, "serve the board and the documentation to Claude over MCP (stdio), for agents that need to look things up")
    p.add_argument("--read-only", action="store_true", help="offer only the tools that read, without proposing tasks or adding notes")
    p = add("shots", cmd_shots, "screenshot a page at phone, iPad and desktop size and keep them as the task's evidence")
    p.add_argument("target", help="an HTML file or the URL of a running app")
    p.add_argument("--task", metavar="ID", help="the task the screenshots belong to (default: $KANVAS_TASK)")
    p.add_argument("--out", metavar="DIR", help="save them here instead of in the task's evidence folder")
    p.add_argument("--wait", type=float, default=4, metavar="SECONDS", help="how long the page may run before the shot (default: %(default)s)")
    p = add("stop", cmd_stop, "stop the agent that is working on a task (a stuck run), so it spends no more tokens")
    p.add_argument("id")
    p = add("design", cmd_design, "show, approve or ask for changes to the UI/UX design a front task needs before it is built")
    p.add_argument("id")
    p.add_argument("--approve", action="store_true", help="approve the proposal so the implementation can start")
    p.add_argument("--changes", metavar="TEXT", help="ask the designer for these changes")
    p.add_argument("--again", action="store_true", help="have the designer try again (after a failure)")
    p = add("review", cmd_review, "have the reviewer agent review a task in review now, and record its verdict")
    p.add_argument("id")
    p = add("usage", cmd_usage, "turns, cost and tokens of every automated run, per task, agent role and model")
    p.add_argument("--json", action="store_true", help="print the rows as JSON")
    p = add("note", cmd_note, "append a note to a task (keeps the description intact)")
    p.add_argument("id")
    p.add_argument("text", nargs="?")
    text_sources(p, "note")
    p = add("ask", cmd_ask, "show the human that you wait for their reply to a question in the conversation; "
                            "the next note ends the wait")
    p.add_argument("id")
    p.add_argument("text", nargs="?", help="the question, as you ask it in the conversation")
    text_sources(p, "question")
    p = add("propose", cmd_propose, "propose a new task (a human approves it)")
    p.add_argument("group", help="group name or prefix, or - for none")
    p.add_argument("title")
    p.add_argument("desc", nargs="?")
    p.add_argument("--deps", "--depends-on", nargs="+", metavar="ID", help="tasks this one depends on")
    text_sources(p, "description")
    p = add("add-group", cmd_add_group, "create a group of tasks")
    p.add_argument("name")
    p.add_argument("--prefix", help="task ID prefix (default: derived from the name)")
    p = add("batch", cmd_batch, "propose many groups and tasks from JSON (stdin or --file)")
    p.add_argument("--file", metavar="PATH", help="read the JSON from a file")
    p = add("depend", cmd_depend, "make a task depend on others: depend DV-03 DV-01 DV-02")
    p.add_argument("id")
    p.add_argument("on", nargs="+", metavar="ON")
    p = add("edit", cmd_edit, "revise a proposal before it is approved")
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--group", help="group name or prefix, or - for none")
    p.add_argument("--desc", dest="new_desc", metavar="DESC", help="new description")
    p.add_argument("-m", "--message", help="optional note on why")
    text_sources(p, "description")
    p = add("withdraw", cmd_withdraw, "delete proposals that are not approved yet")
    p.add_argument("ids", nargs="+", metavar="ID")
    p = add("undepend", cmd_undepend, "remove dependencies from a proposal: undepend DV-03 DV-01")
    p.add_argument("id")
    p.add_argument("on", nargs="+", metavar="ON")

    p = add("serve", cmd_serve, "open the interactive board in your browser")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"port to serve on (default: %(default)s), or the next free one up to 19 higher ({DEFAULT_PORT + 19} by default); "
                        "when a Factory already runs on any of these ports, serve opens the board there and returns")
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 for containers or remote machines")
    p.add_argument("--tailscale", action="store_true", help="also open the boards to your phone and other devices on your Tailscale network")
    p.add_argument("--saved", action="store_true", help="serve only the boards open last time, without a project of its own (for `kanvas service`)")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p = add("service", cmd_service, "start Factory when you log in, so your boards are always open")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--tailscale", action="store_true", help="also open the boards to your phone over Tailscale")
    p = add("init", cmd_init, "set up Factory in a project (never overwrites files)")
    p.add_argument("dir", nargs="?", help=f"project folder (default: the folder of the nearest {BOARD_FILE}, "
                                          "else the top folder of the git repository, else this one)")
    p = add("import", cmd_import, "convert an Obsidian .canvas board from Kanvas 0.x")
    p.add_argument("canvas")
    p.add_argument("-o", "--output", help=f"output file (default: {BOARD_FILE} next to the canvas)")
    p.add_argument("--force", action="store_true", help="replace an existing board that has tasks")
    p = add("export", cmd_export, "write a read-only HTML snapshot to share")
    p.add_argument("output", nargs="?", default="kanvas-snapshot.html")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    # argparse cannot fill an optional positional that follows an option,
    # as in `note DV-01 --as claude "text"`.
    for name in ("text", "desc"):
        if extra and getattr(args, name, False) is None and not extra[0].startswith("-"):
            setattr(args, name, extra.pop(0))
    if extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    func = getattr(args, "func", cmd_status)
    try:
        return func(args) or 0
    except KanvasError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
