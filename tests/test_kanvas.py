"""Tests for kanvas.py. Run with: python -m unittest discover -s tests -v"""

import argparse
import atexit
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import kanvas  # noqa: E402

# The CLI processes the tests start inherit the environment; they must never reach a real board.
os.environ.pop("KANVAS_BOARD", None)
# Nor the boards a real Factory remembers between runs.
STATE_DIR = tempfile.mkdtemp(prefix="kanvas-state-")
os.environ["KANVAS_STATE_DIR"] = STATE_DIR
atexit.register(shutil.rmtree, STATE_DIR, True)

SCRIPT = os.path.join(ROOT, "kanvas.py")


def write_board(path, board):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(kanvas.dumps(board))


def task(tid, state="todo", group=None, deps=(), **extra):
    t = {"id": tid, "title": f"Task {tid}", "state": state, "group": group, "deps": list(deps)}
    t.update(extra)
    return t


def run_html_js(test, code, stdin, sections=()):
    """Runs `code` after the pure part of kanvas.html under Node, with `stdin` parsed as JSON in `input`.

    `sections` adds more of the page, each as a (start, end) pair of markers.
    """
    node = shutil.which("node")
    if not node:
        test.skipTest("node is not installed")
    with open(os.path.join(ROOT, "kanvas.html"), encoding="utf-8") as f:
        html = f.read()
    parts = [html[html.index("const STATES ="):html.index("// App state")]]
    for start, end in sections:
        i = html.index(start)
        parts.append(html[i:html.index(end, i)])
    js = ("\n".join(parts)
          + "\nconst input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n" + code + "\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
    test.addCleanup(os.remove, f.name)
    proc = subprocess.run([node, f.name], input=stdin.encode("utf-8"), capture_output=True, timeout=120)
    test.assertEqual(proc.returncode, 0, proc.stderr.decode())
    return proc.stdout.decode("utf-8")


class TempBoard(unittest.TestCase):
    """A temporary directory with a kanvas.json, and a helper to run the CLI in it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kanvas-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "kanvas.json")
        write_board(self.path, self.initial_board())

    def initial_board(self):
        return {
            "kanvas": 1,
            "title": "Test",
            "groups": [{"id": "DV", "name": "Development"}],
            "tasks": [
                task("DV-01", "done", "DV"),
                task("DV-02", "todo", "DV", ["DV-01"], definition={"approved": True, "outcome": "Task DV-02"}),
                task("DV-03", "todo", "DV", ["DV-02"], definition={"approved": True, "outcome": "Task DV-03"}),
                task("DV-04", "proposed", "DV"),
            ],
        }

    def run_cli(self, *args, stdin=None, cwd=None, env=None, check=True):
        full_env = dict(os.environ, KANVAS_AGENT="")
        full_env.pop("KANVAS_BOARD", None)
        full_env.update(env or {})
        proc = subprocess.run(
            [sys.executable, SCRIPT, *args],
            cwd=cwd or self.dir,
            input=stdin if stdin is not None else b"",
            capture_output=True,
            env=full_env,
        )
        out, err = proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")
        if check and proc.returncode != 0:
            self.fail(f"kanvas {' '.join(args)} failed ({proc.returncode}):\n{out}{err}")
        return proc.returncode, out, err

    def fails(self, *args, **kw):
        code, out, err = self.run_cli(*args, check=False, **kw)
        self.assertNotEqual(code, 0, f"expected kanvas {' '.join(args)} to fail:\n{out}")
        return err

    def board(self):
        return kanvas.load_board(self.path)

    def get(self, tid):
        return kanvas.find_task(self.board(), tid)

    def raw(self):
        with open(self.path, "rb") as f:
            return f.read()


class FormatTests(unittest.TestCase):
    def sample(self):
        with open(os.path.join(ROOT, "examples", "sample.json"), encoding="utf-8") as f:
            return f.read()

    def test_dumps_is_stable(self):
        text = self.sample()
        self.assertEqual(kanvas.dumps(kanvas.parse_board(text)), text)

    def test_the_example_boards_are_valid_and_canonical(self):
        for name in sorted(os.listdir(os.path.join(ROOT, "examples"))):
            with self.subTest(name), open(os.path.join(ROOT, "examples", name), encoding="utf-8") as f:
                text = f.read()
                board = kanvas.parse_board(text)
                self.assertEqual(kanvas.dumps(board), text)
                self.assertEqual(kanvas.problems(board), ([], []))

    def test_short_string_arrays_stay_on_one_line(self):
        board = kanvas.new_board("t")
        board["tasks"] = [task("A-01"), task("A-02", deps=["A-01"]), task("A-03", deps=["A-01", "A-02"])]
        text = kanvas.dumps(board)
        self.assertIn('"deps": ["A-01", "A-02"]', text)
        self.assertEqual(json.loads(text)["tasks"][2]["deps"], ["A-01", "A-02"])

    def test_empty_optional_fields_are_omitted_and_unknown_keys_kept(self):
        board = kanvas.parse_board(json.dumps({
            "kanvas": 1, "groups": [], "custom": {"keep": True},
            "tasks": [{"id": "G-01", "title": "x", "state": "todo", "estimate": 3}],
        }))
        out = json.loads(kanvas.dumps(board))
        self.assertEqual(out["tasks"][0], {"id": "G-01", "title": "x", "state": "todo", "estimate": 3})
        self.assertEqual(out["custom"], {"keep": True})
        self.assertEqual(list(out)[:4], ["kanvas", "title", "groups", "tasks"])

    def test_newer_format_is_rejected(self):
        with self.assertRaises(kanvas.KanvasError):
            kanvas.parse_board('{"kanvas": 99, "groups": [], "tasks": []}')

    def assert_writers_agree(self, text):
        out = run_html_js(self, "process.stdout.write(serialize(normalize(input)));", text)
        self.assertEqual(out, kanvas.dumps(kanvas.parse_board(text)))

    def test_html_serializer_matches_python(self):
        """kanvas.html must write byte-identical files, or every browser save would reformat the board."""
        board = kanvas.parse_board(self.sample())
        board["tasks"][0]["desc"] = 'Quotes " and \\ backslash, tab\t, unicode é 日本 ✓, </script>'
        board["tasks"][1]["deps"] = []
        board["tasks"][1]["extra"] = {"nested": [1, 2.5, None, True]}
        board["tasks"][2]["yolo"] = True
        board["tasks"][3]["yolo"] = False
        board["tasks"][3]["notes"] = [
            {"text": "Edited", "edited": "2026-10-21T10:16:00Z", "by": "human", "at": "2026-10-19T08:00:00Z"},
            {"at": "2026-10-19T09:00:00Z", "by": "codex", "text": "Not edited", "edited": ""},
        ]
        board["unknown"] = ["kept"]
        self.assert_writers_agree(json.dumps(board, ensure_ascii=False))

    def test_writers_agree_on_unusual_values(self):
        """Numbers, numeric keys, keys that are special in JavaScript, missing values and lone surrogates."""
        self.assert_writers_agree(json.dumps({
            "kanvas": 1, "title": 5, "10": "ten", "2": "two", "b": True, "01": "kept in place",
            "constructor": {"__proto__": 1, "toString": None},
            "groups": [{"id": "G", "name": ""}],
            "tasks": [
                {"id": "G-01", "title": None, "state": "", "group": "", "desc": 0, "deps": [], "yolo": 1,
                 "7": "seven", "constructor": "kept", "__proto__": {"x": 1},
                 "numbers": [1.0, -0.0, 1e21, 1e-7, 0.1, 2 ** 53 + 1, 10 ** 30, 123.456, 5e-324]},
                {"id": "G-02", "title": "Lone \ud800 surrogate", "state": None, "deps": ["G-01"], "yolo": 0,
                 "notes": [{"at": "2026-10-21T09:00:00Z", "by": "codex", "text": "x", "4": 4}]},
            ],
        }))


class ModelTests(unittest.TestCase):
    def test_suggest_prefix(self):
        self.assertEqual(kanvas.suggest_prefix("Research", []), "RS")
        self.assertEqual(kanvas.suggest_prefix("Development", []), "DV")
        self.assertEqual(kanvas.suggest_prefix("Delivery", ["DV"]), "DL")
        self.assertEqual(kanvas.suggest_prefix("Report Writing", []), "RW")
        self.assertEqual(kanvas.suggest_prefix("Level 0 Contract", []), "L0C")

    def test_next_id_goes_past_99(self):
        board = kanvas.new_board()
        board["tasks"] = [task("DV-99")]
        self.assertEqual(kanvas.next_id(board, "DV"), "DV-100")
        board["tasks"].append(task("DV-100"))
        self.assertEqual(kanvas.next_id(board, "dv"), "DV-101")

    def test_html_next_id_matches_python(self):
        board = kanvas.new_board()
        board["tasks"] = [task(tid) for tid in ("DV-007", "dv-12", "DV-99", "DVX-150", "D-170", "DV-X-140", "DV-", "DV-1a",
                                                "-5", "DV-²", "DV-٣", "L0C-03", "g-04", "QA-1")]
        prefixes = ["DV", "dv", "DVX", "D", "L0C", "G", "QA", "NEW"]
        out = run_html_js(self, f"const b = normalize(input); process.stdout.write(JSON.stringify({json.dumps(prefixes)}.map((p) => nextId(b, p))));",
                          json.dumps(board))
        self.assertEqual(json.loads(out), [kanvas.next_id(board, p) for p in prefixes])
        self.assertEqual(json.loads(out), ["DV-100", "DV-100", "DVX-151", "D-171", "L0C-04", "G-05", "QA-02", "NEW-01"])

    def test_problems(self):
        board = kanvas.new_board()
        board["tasks"] = [
            task("A-01", deps=["A-02"]), task("A-02", deps=["A-01"]),
            task("A-03", deps=["A-09"]), task("A-04", "doing", deps=["A-03"]), task("bad id"),
        ]
        errors, warnings = kanvas.problems(board)
        self.assertTrue(any("Circular dependency" in e for e in errors), errors)
        self.assertTrue(any("missing task A-09" in e for e in errors), errors)
        self.assertTrue(any("Invalid task id" in e for e in errors), errors)
        self.assertTrue(any("A-04 is doing but depends on unfinished A-03" in w for w in warnings), warnings)


class UndoTests(unittest.TestCase):
    """Undo and redo replay a change on the latest board, which agents may have changed meanwhile."""

    NOTE = {"at": "2026-10-21T09:00:00Z", "by": "human", "text": "Same"}
    AGENT = {"at": "2026-10-21T09:05:00Z", "by": "codex", "text": "Agent note"}

    def board(self, *tasks):
        return {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01")] + list(tasks)}

    def notes(self, *notes):
        return self.board(task("A-02", notes=list(notes)))

    def replay(self, cur, frm, to):
        code = ("const [cur, from, to] = input.map(normalize);\n"
                "const result = replay(cur, from, to);\n"
                "process.stdout.write(JSON.stringify({ ...result, board: cur }));")
        out = json.loads(run_html_js(self, code, json.dumps([cur, frm, to])))
        return out["board"], out["applied"], out["skipped"]

    def test_undoing_a_note_edit_restores_the_note_whatever_its_key_order(self):
        edited = dict(self.NOTE, text="Changed", edited="2026-10-21T09:10:00Z")
        read_back = {k: edited[k] for k in ("edited", "text", "by", "at")}
        board, _, _ = self.replay(self.notes(read_back, self.AGENT), self.notes(edited), self.notes(self.NOTE))
        self.assertEqual(board["tasks"][1]["notes"], [self.NOTE, self.AGENT])

    def test_identical_notes_are_counted(self):
        board, _, _ = self.replay(self.notes(self.NOTE, self.AGENT), self.notes(self.NOTE), self.notes(self.NOTE, self.NOTE))
        self.assertEqual(board["tasks"][1]["notes"], [self.NOTE, self.NOTE, self.AGENT])
        board, _, _ = self.replay(board, self.notes(self.NOTE, self.NOTE), self.notes(self.NOTE))
        self.assertEqual(board["tasks"][1]["notes"], [self.NOTE, self.AGENT])

    def test_deleting_a_group_and_undoing_it(self):
        board = {"kanvas": 1, "title": "", "groups": [{"id": "A", "name": "Alpha"}, {"id": "B", "name": "Beta"}],
                 "tasks": [task("A-01", group="A"), task("A-02", group="A"), task("B-01", group="B", deps=["A-01", "A-02"])]}
        code = ("const b = normalize(input), before = clone(b);\n"
                "removeGroup(b, 'A', ['a-01', 'B-01']);\n"
                "const after = clone(b);\n"
                "replay(b, after, before);\n"
                "process.stdout.write(JSON.stringify({ before, after, undone: b }));")
        out = json.loads(run_html_js(self, code, json.dumps(board)))
        after = out["after"]
        self.assertEqual(after["groups"], [{"id": "B", "name": "Beta"}])
        self.assertEqual([(t["id"], t["group"], t["deps"]) for t in after["tasks"]], [("A-02", None, []), ("B-01", "B", ["A-02"])])
        self.assertEqual(out["undone"], out["before"])

    def test_a_task_someone_changed_is_not_removed(self):
        created = self.board(task("A-02"))
        board, applied, skipped = self.replay(self.board(task("A-02", notes=[self.AGENT])), created, self.board())
        self.assertEqual([t["id"] for t in board["tasks"]], ["A-01", "A-02"])
        self.assertEqual((applied, skipped), (0, 1))
        board, applied, _ = self.replay(created, created, self.board())
        self.assertEqual([t["id"] for t in board["tasks"]], ["A-01"])
        self.assertEqual(applied, 1)

    def test_identical_notes_are_told_apart_by_rank(self):
        code = ("const t = normalize(input).tasks[1];\n"
                "process.stdout.write(JSON.stringify([noteRank(t, t.notes[0]), noteRank(t, t.notes[1]), noteRank(t, { ...t.notes[0] }),\n"
                "  noteIndex(t, t.notes[0], 1), noteIndex(t, t.notes[0], 5), noteIndex(t, t.notes[2])]));")
        out = run_html_js(self, code, json.dumps(self.notes(self.NOTE, self.NOTE, self.AGENT)))
        self.assertEqual(json.loads(out), [0, 1, 0, 1, 1, 2])

    def test_undo_labels_name_the_action(self):
        def board(*tasks, groups=("A", "B")):
            return {"kanvas": 1, "title": "", "groups": [{"id": g, "name": f"Group {g}"} for g in groups], "tasks": list(tasks)}

        def change(fn):
            before = board(task("A-01", "proposed", "A"), task("A-02", "doing", "A", ["A-01"], notes=[self.NOTE]),
                           task("A-03", "review", "A", ["A-02"]), task("A-04", "done", "A"), task("A-05", "proposed", "A"))
            after = json.loads(json.dumps(before))
            fn(after, {t["id"]: t for t in after["tasks"]})
            return [before, after]

        def drop(b, ids):
            b["tasks"] = [t for t in b["tasks"] if t["id"] not in ids]
            for t in b["tasks"]:
                t["deps"] = [d for d in t["deps"] if d not in ids]

        cases = {
            "approving the definition of A-01": lambda b, t: t["A-01"].update(state="todo"),
            "pausing A-02": lambda b, t: t["A-02"].update(state="todo"),
            "moving A-03 to To do": lambda b, t: t["A-03"].update(state="todo", notes=[self.NOTE]),
            "reopening A-04": lambda b, t: t["A-04"].update(state="todo"),
            "starting A-01": lambda b, t: t["A-01"].update(state="doing"),
            "recording the dev merge of A-02 and A-03": lambda b, t: [t[i].update(state="develop") for i in ("A-02", "A-03")],
            "moving A-04 to Proposed": lambda b, t: t["A-04"].update(state="proposed"),
            "rejecting A-01": lambda b, t: drop(b, {"A-01"}),
            "deleting A-02": lambda b, t: drop(b, {"A-02"}),
            "rejecting A-01 and A-05": lambda b, t: drop(b, {"A-01", "A-05"}),
            "unlinking A-03 from A-02": lambda b, t: t["A-03"].update(deps=[]),
            "making A-05 wait for A-01 and A-04": lambda b, t: t["A-05"].update(deps=["A-01", "A-04"]),
            "adding A-06": lambda b, t: (b["tasks"].append(task("A-06", group="A")), t["A-05"].update(deps=["A-06"])),
            "turning on YOLO up to A-03": lambda b, t: (t["A-03"].update(yolo=True), t["A-01"].update(state="todo")),
            "deleting the Group A group": lambda b, t: (b["groups"].pop(0), [x.update(group=None) for x in b["tasks"]]),
            "adding a note to A-02": lambda b, t: t["A-02"]["notes"].append(self.AGENT),
            "deleting a note from A-02": lambda b, t: t["A-02"].update(notes=[]),
            "editing a note on A-02": lambda b, t: t["A-02"].update(notes=[dict(self.NOTE, text="New")]),
            "asking for changes to A-05": lambda b, t: t["A-05"].update(notes=[dict(self.NOTE, text="Changes requested: Split it")]),
            "renaming A-02": lambda b, t: t["A-02"].update(title="New title"),
            "moving A-02 to Group B": lambda b, t: t["A-02"].update(group="B"),
            "reordering the cards": lambda b, t: b["tasks"].reverse(),
            "renaming the Group B group": lambda b, t: b["groups"][1].update(name="Beta"),
            "renaming the board": lambda b, t: b.update(title="Board"),
        }
        pairs = [change(fn) for fn in cases.values()]
        yolo_off = change(lambda b, t: t["A-03"].update(yolo=True))[::-1]
        many = [board(*(task(f"A-0{i}", "proposed") for i in range(1, 6))), board(*(task(f"A-0{i}") for i in range(1, 6)))]
        code = "process.stdout.write(JSON.stringify(input.map(([a, z]) => describeChange(normalize(a), normalize(z)))));"
        out = run_html_js(self, code, json.dumps(pairs + [yolo_off, many]), [("const MOVE_WORDS =", "\n// Undoes or redoes")])
        self.assertEqual(json.loads(out), list(cases) + ["turning off YOLO on A-03", "approving the definition of 5 tasks"])


class FailedSaveTests(unittest.TestCase):
    """A change that is not saved hands its text back through commit()'s onFail, and the board goes back."""

    SECTIONS = [("async function commit(", "async function poll("), ("function mustFind(", "\n// ----")]

    def run_commits(self, code, board):
        setup = (
            "let board, confirmed, store, lost = null, pending = 0, writes = 0, saveQueue = Promise.resolve();\n"
            "const log = [], failed = [];\n"
            "toast = (msg) => log.push(msg);\n"
            "const setBoard = (b) => { board = b; }, render = () => {}, remember = () => ({}), forget = () => {}, setSync = () => {};\n"
            "const onFail = (e) => failed.push(e.message);\n"
            "board = normalize(input); confirmed = clone(board);\n"
        )
        return json.loads(run_html_js(self, setup + code, json.dumps(board), self.SECTIONS))

    def test_a_failed_save_calls_on_fail_and_restores_the_board(self):
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01")]}
        out = self.run_commits(
            "store = { load: async () => clone(confirmed), save: async () => { throw new Error('server answered 500'); } };\n"
            "(async () => {\n"
            "  const refused = await commit(ops.note('A-09', 'Typed text'), { onFail });\n"
            "  const queued = await commit(ops.note('A-01', 'Typed text'), { onFail });\n"
            "  const shown = board.tasks[0].notes.length;\n"
            "  await saveQueue;\n"
            "  process.stdout.write(JSON.stringify({ refused, queued, shown, after: board.tasks[0].notes.length, failed, log }));\n"
            "})();", board)
        self.assertEqual((out["refused"], out["queued"], out["shown"], out["after"]), (False, True, 1, 0))
        self.assertEqual(out["failed"], ["A-09 no longer exists", "server answered 500"])
        self.assertEqual(out["log"], ["A-09 no longer exists", "Not saved: server answered 500"])

    def test_a_saved_change_does_not_call_on_fail(self):
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01")]}
        out = self.run_commits(
            "store = { load: async () => clone(confirmed), save: async (fn, base) => { const b = clone(base); fn(b); return { board: b, base }; } };\n"
            "(async () => {\n"
            "  await commit(ops.note('A-01', 'Typed text'), { onFail });\n"
            "  await saveQueue;\n"
            "  process.stdout.write(JSON.stringify({ notes: confirmed.tasks[0].notes.map((n) => n.text), failed, log }));\n"
            "})();", board)
        self.assertEqual(out, {"notes": ["Typed text"], "failed": [], "log": []})

    def test_changes_wait_while_the_board_cannot_be_reached(self):
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01")]}
        out = self.run_commits(
            "store = { save: async () => { throw new Error('tried to save'); } };\n"
            "lost = 'Factory stopped running or cannot be reached';\n"
            "(async () => {\n"
            "  const ok = await commit(ops.note('A-01', 'Typed text'), { onFail });\n"
            "  await saveQueue;\n"
            "  process.stdout.write(JSON.stringify({ ok, pending, notes: board.tasks[0].notes.length, failed, log }));\n"
            "})();", board)
        self.assertEqual(out, {"ok": False, "pending": 0, "notes": 0, "failed": ["Factory stopped running or cannot be reached"],
                               "log": ["Not saved: Factory stopped running or cannot be reached"]})


class LostConnectionTests(unittest.TestCase):
    """The server store tells a board it cannot reach from other errors, in plain words."""

    def test_errors_say_when_the_board_cannot_be_reached(self):
        code = (
            "const setTimeout = (fn) => { fn(); return 0; };\n"
            "let answer;\n"
            "const fetch = (url, opts) => new Promise((resolve, reject) => {\n"
            "  // setTimeout runs at once, so a request with a time limit is aborted before it starts; one without gets its late answer.\n"
            "  if (answer === 'hang' && opts.signal.aborted) reject(new DOMException('aborted', 'AbortError'));\n"
            "  else if (answer === 'hang') resolve({ ok: true, status: 200, text: async () => '{}', headers: new Map() });\n"
            "  else if (answer === 'down') reject(new TypeError('Failed to fetch'));\n"
            "  else resolve({ ok: false, status: answer, statusText: '', text: async () => '{\"error\": \"No board\"}', headers: new Map() });\n"
            "});\n"
            "(async () => {\n"
            "  const out = {};\n"
            "  for (answer of ['down', 'hang', 404, 500]) {\n"
            "    for (const step of ['load', 'poll', 'save']) {\n"
            "      try { await serverStore()[step]((b) => b, normalize({})); out[`${answer} ${step}`] = 'no error'; }\n"
            "      catch (e) { out[`${answer} ${step}`] = [e.message, !!e.lost]; }\n"
            "    }\n"
            "  }\n"
            "  process.stdout.write(JSON.stringify(out));\n"
            "})();"
        )
        out = json.loads(run_html_js(self, code, "{}", [("function serverStore()", "function fileStore(")]))
        down = ["Factory stopped running or cannot be reached", True]
        closed = ["this board is no longer open in Factory", True]
        self.assertEqual(out, {
            "down load": down, "down poll": down, "down save": down,
            # Only reads give up on a request that hangs: a write may have landed, and waits for its answer.
            "hang load": down, "hang poll": down, "hang save": "no error",
            "404 load": closed, "404 poll": closed, "404 save": ["No board", False],
            "500 load": ["server answered 500", False], "500 poll": ["server answered 500", False], "500 save": ["No board", False],
        })


class FileStoreTests(TempBoard):
    """In file mode, an agent's CLI write that lands while the page saves is kept, and the page's change goes on top."""

    def save_with_agent(self, writes):
        """Saves a human note on DV-03 while the CLI adds the agent notes in `writes` ({step: [text, ...]}) at that step.

        The handle works as Chrome's does: createWritable() fills a swap file, and close() moves it over the board.
        """
        code = (
            "const fs = require('fs'), { execFileSync } = require('child_process');\n"
            "const [path, python, script, writes] = input;\n"
            "let aborted = 0;\n"
            "const agent = (step) => { const text = (writes[step] || []).shift();\n"
            "  if (text) execFileSync(python, [script, 'note', 'DV-02', text, '--board', path, '--as', 'agent']); };\n"
            "const handle = {\n"
            "  name: 'kanvas.json',\n"
            "  async getFile() { const text = fs.readFileSync(path, 'utf8'), st = fs.statSync(path);\n"
            "    return { lastModified: st.mtimeMs, size: st.size, text: async () => text }; },\n"
            "  async createWritable() {\n"
            "    const swap = path + '.crswap';\n"
            "    fs.writeFileSync(swap, '');\n"
            "    return { async write(t) { fs.writeFileSync(swap, t); agent('write'); },\n"
            "             async close() { fs.renameSync(swap, path); agent('close'); },\n"
            "             async abort() { fs.unlinkSync(swap); aborted++; } };\n"
            "  },\n"
            "};\n"
            "(async () => {\n"
            "  const store = fileStore(handle), base = await store.load();\n"
            "  let saved, error = null;\n"
            "  try { saved = (await store.save(ops.note('DV-03', 'Human note'), base)).board; } catch (e) { error = e.message; }\n"
            "  const notes = (b) => b.tasks.flatMap((t) => t.notes.map((n) => `${t.id} ${n.by}: ${n.text}`));\n"
            "  const file = fs.readFileSync(path, 'utf8'), polled = await store.poll();\n"
            "  process.stdout.write(JSON.stringify({ error, aborted, saved: saved ? notes(saved) : null, file: notes(normalize(JSON.parse(file))),\n"
            "    canonical: serialize(normalize(JSON.parse(file))) === file, polled: polled && notes(polled.board),\n"
            "    swap: fs.existsSync(path + '.crswap') }));\n"
            "})();"
        )
        stdin = json.dumps([self.path, sys.executable, SCRIPT, writes])
        sections = [("function fileStore(", "function memoryStore("), ("function mustFind(", "\n// ----")]
        return json.loads(run_html_js(self, code, stdin, sections))

    def test_a_save_without_agents_writes_once(self):
        out = self.save_with_agent({})
        self.assertEqual(out, {"error": None, "aborted": 0, "saved": ["DV-03 human: Human note"], "file": ["DV-03 human: Human note"],
                               "canonical": True, "polled": None, "swap": False})

    def test_an_agent_write_during_a_save_is_kept(self):
        out = self.save_with_agent({"write": ["Agent note"]})
        both = ["DV-02 agent: Agent note", "DV-03 human: Human note"]
        self.assertEqual(out, {"error": None, "aborted": 1, "saved": both, "file": both, "canonical": True, "polled": None, "swap": False})

    def test_an_agent_write_right_after_a_save_shows_at_the_next_poll(self):
        out = self.save_with_agent({"close": ["Agent note"]})
        both = ["DV-02 agent: Agent note", "DV-03 human: Human note"]
        self.assertEqual(out, {"error": None, "aborted": 0, "saved": ["DV-03 human: Human note"], "file": both, "canonical": True,
                               "polled": both, "swap": False})

    def test_a_save_gives_up_when_the_file_keeps_changing(self):
        notes = [f"Agent note {i}" for i in range(6)]
        out = self.save_with_agent({"write": list(notes)})
        agent = [f"DV-02 agent: {n}" for n in notes]
        self.assertEqual(out, {"error": "the board kept changing; try again", "aborted": 6, "saved": None, "file": agent,
                               "canonical": True, "polled": agent, "swap": False})


class LeaveTests(unittest.TestCase):
    """Leaving the page asks first only while something would be lost."""

    def test_unsaved_work(self):
        code = (
            "let fields = [], pending = 0, store, board = normalize(input), IX = indexOf(board);\n"
            "globalThis.document = { querySelector: () => null, querySelectorAll: () => fields };\n"
            "const get = (id) => IX.get(key(id)), ui = {}, drafts = { note: new Map(), back: new Map() };\n"
            "const reset = () => {\n"
            "  fields = []; pending = 0; store = { kind: 'server' };\n"
            "  Object.assign(ui, { selected: 'A-01', editingDesc: false, editingNote: null });\n"
            "  Object.assign(drafts, { desc: null, noteEdit: null, title: null, boardTitle: null });\n"
            "  drafts.note.clear(); drafts.back.clear();\n"
            "};\n"
            "const cases = {\n"
            "  clean: () => {},\n"
            "  pendingSave: () => { pending = 1; },\n"
            "  memoryBoard: () => { store = { kind: 'memory', dirty: false }; },\n"
            "  memoryBoardChanged: () => { store = { kind: 'memory', dirty: true }; },\n"
            "  snapshot: () => { store = { kind: 'snapshot', readOnly: true }; drafts.note.set('A-01', 'typed'); },\n"
            "  note: () => { drafts.note.set('A-01', 'typed'); },\n"
            "  blankNote: () => { drafts.note.set('A-01', '  '); },\n"
            "  noteOnRemovedTask: () => { drafts.note.set('A-09', 'typed'); },\n"
            "  descEditorUnchanged: () => { ui.editingDesc = true; drafts.desc = 'Old '; },\n"
            "  descEditorChanged: () => { ui.editingDesc = true; drafts.desc = 'New'; },\n"
            "  noteEditorChanged: () => { ui.editingNote = { text: 'Was' }; drafts.noteEdit = 'Is'; },\n"
            "  givenBack: () => { drafts.back.set(key('A-01'), [() => {}]); },\n"
            "  givenBackRemovedTask: () => { drafts.back.set(key('A-09'), [() => {}]); },\n"
            "  dialogFilled: () => { fields = [{ value: 'A task' }]; },\n"
            "  dialogEmpty: () => { fields = [{ value: ' ' }]; },\n"
            "};\n"
            "const out = {};\n"
            "for (const [name, set] of Object.entries(cases)) { reset(); set(); out[name] = unsaved(); }\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01", desc="Old")]}
        out = json.loads(run_html_js(self, code, json.dumps(board), [("function unsaved(", "async function boot(")]))
        asks = {name for name, value in out.items() if value}
        self.assertEqual(asks, {"pendingSave", "memoryBoardChanged", "note", "descEditorChanged", "noteEditorChanged", "givenBack", "dialogFilled"})


class ChecklistTests(unittest.TestCase):
    """A checkbox ticked in a rendered description changes the item it shows and nothing else."""

    LINES = [
        "Steps, with a fake - [ ] inside a sentence",
        "- [ ] plain",
        "  - [x] nested",
        "    continued",
        "* [X] star, upper case",
        "+ [ ] plus",
        "1. [ ] numbered",
        "2) [x] numbered with a parenthesis",
        "> - [ ] quoted",
        "```",
        "- [ ] not real",
        "```",
        "- [ ]",
        "- not a task [ ]",
        "- [ ] plain",
        "| a | - [ ] in a table |",
        "|---|---|",
        "- [x] after a table",
        "  - [ ] child",
        "    - [x] grandchild",
        "1. [ ] numbered after bullets",
        ">>>>>> - [x] six quotes deep",
        ">>>>>>> - [ ] seven quotes deep, so text",
        "- [ ]\u2028a line separator after the box",
        "-\u2029[x] separators\u2028inside",
    ]
    DESC = "\n".join(LINES)
    # The line of each item in DESC, in the order the boxes are rendered.
    ITEMS = [(1, "plain", False), (2, "nested continued", True), (4, "star, upper case", True), (5, "plus", False),
             (6, "numbered", False), (7, "numbered with a parenthesis", True), (8, "quoted", False), (14, "plain", False),
             (17, "after a table", True), (18, "child", False), (19, "grandchild", True), (20, "numbered after bullets", False),
             (21, "six quotes deep", True), (23, "a line separator after the box", False), (24, "separators\u2028inside", True)]

    def run_js(self, code, desc=DESC):
        code = "const get = () => null;\n" + code
        return json.loads(run_html_js(self, code, json.dumps(desc), [("function inlineMd(", "\n// ----")]))

    def test_the_items_are_the_boxes_markdown_renders(self):
        out = self.run_js("process.stdout.write(JSON.stringify([taskItems(input).map((x) => [x.text, x.checked]),\n"
                          "  [...markdown(input).matchAll(/<input type=\"checkbox\" disabled( checked)?>/g)].map((m) => !!m[1])]));")
        self.assertEqual(out, [[[text, checked] for _, text, checked in self.ITEMS], [checked for *_, checked in self.ITEMS]])

    def test_a_quote_ten_thousand_deep_holds_no_boxes(self):
        out = self.run_js("process.stdout.write(JSON.stringify([taskItems(input), markdown(input).includes('checkbox')]));", ">" * 10000 + " - [ ] deep")
        self.assertEqual(out, [[], False])

    def test_toggling_changes_only_the_line_of_that_item(self):
        out = self.run_js("const items = taskItems(input);\n"
                          "process.stdout.write(JSON.stringify(items.map((x, i) => toggleTask(input, x.text,\n"
                          "  items.slice(0, i).filter((y) => y.text === x.text).length, !x.checked))));")
        for (line, _, checked), toggled in zip(self.ITEMS, out):
            lines = list(self.LINES)
            lines[line] = re.sub(r"\[[ xX]\]", "[ ]" if checked else "[x]", lines[line], count=1)
            self.assertEqual(toggled, "\n".join(lines))
        self.assertEqual(len(out), len(self.ITEMS))

    def test_missing_items_and_items_already_in_that_state(self):
        out = self.run_js("process.stdout.write(JSON.stringify([toggleTask(input, 'gone', 0, true), toggleTask(input, 'plain', 2, true),\n"
                          "  toggleTask(input, 'plain', 1, false) === input, toggleTask(input, 'star, upper case', 0, true) === input,\n"
                          "  toggleTask(input.replace(/\\n/g, '\\r\\n'), 'plus', 0, true)]));")
        self.assertEqual(out, [None, None, True, True, self.DESC.replace("+ [ ] plus", "+ [x] plus").replace("\n", "\r\n")])


def link(href, text):
    return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{text}</a>'


def ref(text, tid="RS-02"):
    return f'<a class="tref" href="#{tid}" data-ref="{tid}">{text}</a>'


def table(head, *rows, align=()):
    def row(cells, tag):
        out = ""
        for cell, a in zip(cells, list(align) + [None] * len(cells)):
            attr = f' style="text-align: {a}"' if a else ""
            out += f"<{tag}{attr}>{cell}</{tag}>"
        return f"<tr>{out}</tr>"
    body = f"<tbody>{''.join(row(r, 'td') for r in rows)}</tbody>" if rows else ""
    return f'<div class="table"><table><thead>{row(head, "th")}</thead>{body}</table></div>'


class MarkdownTests(unittest.TestCase):
    """markdown() turns text from agents into HTML: the subset it knows, with everything else escaped."""

    def check(self, cases):
        code = ("const tasks = new Map([['RS-02', { id: 'RS-02' }]]);\n"
                "const get = (id) => tasks.get(key(id));\n"
                "process.stdout.write(JSON.stringify(input.map((src) => markdown(src))));")
        out = json.loads(run_html_js(self, code, json.dumps([src for src, _ in cases]), [("function inlineMd(", "\n// ----")]))
        for (src, expected), html in zip(cases, out):
            with self.subTest(src=src):
                self.assertEqual(html, expected)

    def test_task_ids_link_outside_link_text_only(self):
        self.check([
            ("RS-02 and rs-02, XX-99 is unknown", f"<p>{ref('RS-02')} and {ref('rs-02')}, XX-99 is unknown</p>"),
            ("[see RS-02](https://example.com)", f"<p>{link('https://example.com', 'see RS-02')}</p>"),
            ("RS-02, [see RS-02](https://example.com) and RS-02",
             f"<p>{ref('RS-02')}, {link('https://example.com', 'see RS-02')} and {ref('RS-02')}</p>"),
            ("[**RS-02** first](mailto:a@example.com)", f"<p>{link('mailto:a@example.com', '<strong>RS-02</strong> first')}</p>"),
            ("[RS-02](https://example.com/RS-02) https://example.com/RS-02",
             f"<p>{link('https://example.com/RS-02', 'RS-02')} {link('https://example.com/RS-02', 'https://example.com/RS-02')}</p>"),
            # A refused URL leaves plain text, where a task ID is a reference again.
            ("[see RS-02](#RS-02) [x](vbscript:RS-02)", f"<p>see {ref('RS-02')} x</p>"),
            ("`RS-02` *RS-02*", f"<p><code>RS-02</code> <em>{ref('RS-02')}</em></p>"),
        ])

    def test_urls_hold_balanced_parentheses(self):
        foo, nested = "https://en.wikipedia.org/wiki/Foo_(bar)", "https://example.com/a_(b_(c))_d"
        self.check([
            (f"[spec]({foo})", f"<p>{link(foo, 'spec')}</p>"),
            (f"[spec]({nested}) {nested}", f"<p>{link(nested, 'spec')} {link(nested, nested)}</p>"),
            (f"{foo}, ({foo}) and ({foo}).", f"<p>{link(foo, foo)}, ({link(foo, foo)}) and ({link(foo, foo)}).</p>"),
            ("(see [spec](https://example.com/a)) (see https://example.com/a)",
             f"<p>(see {link('https://example.com/a', 'spec')}) (see {link('https://example.com/a', 'https://example.com/a')})</p>"),
            ("[RS-02 notes](https://example.com/RS-02_(draft))", f"<p>{link('https://example.com/RS-02_(draft)', 'RS-02 notes')}</p>"),
            # Unbalanced: a link needs its closing parenthesis, and a bare URL ends before one that closes nothing.
            ("[spec](https://example.com/a_(b) and more",
             f"<p>[spec]({link('https://example.com/a_(b)', 'https://example.com/a_(b)')} and more</p>"),
            ("[spec](https://example.com/a_b)) https://example.com/a)b https://example.com/a_(b c",
             f"<p>{link('https://example.com/a_b', 'spec')}) {link('https://example.com/a', 'https://example.com/a')})b "
             f"{link('https://example.com/a_', 'https://example.com/a_')}(b c</p>"),
            ("[spec](https://example.com/a(b c)", f"<p>[spec]({link('https://example.com/a', 'https://example.com/a')}(b c)</p>"),
            # The rest is as before.
            ('Go to https://example.com/a.b?q=1&r=2. "https://example.com/x"!',
             f"<p>Go to {link('https://example.com/a.b?q=1&amp;r=2', 'https://example.com/a.b?q=1&amp;r=2')}. "
             f"&quot;{link('https://example.com/x', 'https://example.com/x')}&quot;!</p>"),
            ("[js link](javascript:alert(1)) and [ok](https://example.com)", f"<p>js link and {link('https://example.com', 'ok')}</p>"),
            ('[x](https://example.com/"onmouseover="alert(1))',
             f"<p>{link('https://example.com/&quot;onmouseover=&quot;alert(1)', 'x')}</p>"),
            ("[d](data:text/html,<script>alert(1)</script>) https://x", "<p>d https://x</p>"),
        ])

    def test_tables(self):
        one = table(["a"], ["1"])
        self.check([
            ("| Left | Center | Right | None |\n|:-----|:------:|------:|------|\n| a | b | c | d |",
             '<div class="table"><table><thead><tr><th style="text-align: left">Left</th><th style="text-align: center">Center</th>'
             '<th style="text-align: right">Right</th><th>None</th></tr></thead><tbody><tr><td style="text-align: left">a</td>'
             '<td style="text-align: center">b</td><td style="text-align: right">c</td><td>d</td></tr></tbody></table></div>'),
            # Outer pipes are optional, \| is a pipe inside a cell, code included, and cells hold inline Markdown.
            ("Name | Note\n--- | :-:\n`a\\|b` | x \\| y, **RS-02**",
             table(["Name", "Note"], ["<code>a|b</code>", f"x | y, <strong>{ref('RS-02')}</strong>"], align=[None, "center"])),
            # Missing cells are empty and extra ones dropped.
            ("| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |\n|| 2 |", table(["a", "b"], ["1", ""], ["1", "2"], ["", "2"])),
            ("| a |\n|---|", table(["a"])),
            # A table may follow a paragraph line or a list, and ends at a line without a pipe or a new block.
            ("Results:\n| a |\n|---|\n| 1 |\nafter\n\n| b |\n|---|\n\n| 2 |", f"<p>Results:</p>{one}<p>after</p>{table(['b'])}<p>| 2 |</p>"),
            ("- item\n| a |\n|---|\n| 1 |\n- next", f"<ul><li>item</li></ul>{one}<ul><li>next</li></ul>"),
            ("1. one\n\n| a |\n|---|\n| 1 |\n2. two\n> | a |\n> |---|\n> | 1 |",
             f'<ol><li>one</li></ol>{one}<ol><li>two</li></ol><blockquote>{one}</blockquote>'),
            # Not tables: cell counts differ, no row of dashes, or dashes with no pipe.
            ("| a | b |\n|---|\n\na | b\nc | d\n\n| a |\n| - - |\n\nTitle\n---",
             "<p>| a | b |<br>|---|</p><p>a | b<br>c | d</p><p>| a |<br>| - - |</p><p>Title</p><hr>"),
            ("| <script>alert(1)</script> | [x](javascript:alert(1)) |\n|:-:|---|\n| <img src=x onerror=alert(1)> | ![y](https://example.com/\"onerror=\"alert(1)) |",
             table(["&lt;script&gt;alert(1)&lt;/script&gt;", "x"],
                   ["&lt;img src=x onerror=alert(1)&gt;", link("https://example.com/&quot;onerror=&quot;alert(1)", "y")], align=["center"])),
        ])

    def test_nested_lists(self):
        box, ticked = '<li class="task"><input type="checkbox" disabled>', '<li class="task"><input type="checkbox" disabled checked>'
        self.check([
            ("- a\n  - b\n    - c\n  - d\n- e", "<ul><li>a<ul><li>b<ul><li>c</li></ul></li><li>d</li></ul></li><li>e</li></ul>"),
            ("1. one\n   - sub\n   - sub two\n2. two\n   1. deep",
             "<ol><li>one<ul><li>sub</li><li>sub two</li></ul></li><li>two<ol><li>deep</li></ol></li></ol>"),
            # A switch between bullets and numbers starts a new list.
            ("- a\n- b\n1. c\n2) d\n* e", "<ul><li>a</li><li>b</li></ul><ol><li>c</li><li>d</li></ol><ul><li>e</li></ul>"),
            ("- [ ] parent\n  - [x] child\n    with more\n  - plain child, RS-02\n- [X] next",
             f"<ul>{box} parent<ul>{ticked} child with more</li><li>plain child, {ref('RS-02')}</li></ul></li>{ticked} next</li></ul>"),
            # Two columns deeper make a child; in between, an item joins the deeper list. A tab counts as four.
            ("- a\n - b\n    - c\n  - d\n\t- e", "<ul><li>a</li><li>b<ul><li>c</li><li>d</li><li>e</li></ul></li></ul>"),
            ("  - a\n- b", "<ul><li>a</li><li>b</li></ul>"),
            ("Steps:\n1. a\n2. b\n\n- c\n\n- d", "<p>Steps:</p><ol><li>a</li><li>b</li></ol><ul><li>c</li></ul><ul><li>d</li></ul>"),
            ("> - a\n>   - b", "<blockquote><ul><li>a<ul><li>b</li></ul></li></ul></blockquote>"),
            ("\n".join(" " * 2 * k + f"- {k}" for k in range(8)),
             "<ul><li>0<ul><li>1<ul><li>2<ul><li>3<ul><li>4<ul><li>5</li><li>6</li><li>7</li></ul></li></ul></li></ul></li></ul></li></ul></li></ul>"),
        ])

    def test_quotes_nest_six_deep_at_most(self):
        six = lambda html: "<blockquote>" * 6 + html + "</blockquote>" * 6
        self.check([
            ("> one\n> two **RS-02**\n>\n>no space", f"<blockquote><p>one<br>two <strong>{ref('RS-02')}</strong></p><p>no space</p></blockquote>"),
            ("before\n> # Title\n> ```\n> <b>\n> ```\n> 1. one\n> ---\nafter",
             "<p>before</p><blockquote><h3>Title</h3><pre><code>&lt;b&gt;</code></pre><ol><li>one</li></ol><hr></blockquote><p>after</p>"),
            ("> a\n>> b\n> > c\n> d", "<blockquote><p>a</p><blockquote><p>b<br>c</p></blockquote><p>d</p></blockquote>"),
            (">>>>>> six\n>>>>>> - [x] box", six('<p>six</p><ul><li class="task"><input type="checkbox" disabled checked> box</li></ul>')),
            # Deeper markers are text, in the paragraph they stand in.
            ("> > > > > > > seven\n>>>>>>>> - [ ] eight, RS-02", six(f"<p>&gt; seven<br>&gt;&gt; - [ ] eight, {ref('RS-02')}</p>")),
            (">" * 10000 + " deep", six("<p>" + "&gt;" * 9994 + " deep</p>")),
            ("> " * 10000 + "deep", six("<p>" + "&gt; " * 9994 + "deep</p>")),
        ])

    def test_images_are_plain_links(self):
        self.check([
            ("![img](https://example.com/x.png)", f"<p>{link('https://example.com/x.png', 'img')}</p>"),
            ("![](https://example.com/x_y_z.png) and [](https://example.com)",
             f"<p>{link('https://example.com/x_y_z.png', 'https://example.com/x_y_z.png')} and {link('https://example.com', 'https://example.com')}</p>"),
            ("![see RS-02](https://example.com/a_(b).png)", f"<p>{link('https://example.com/a_(b).png', 'see RS-02')}</p>"),
            ("![x](javascript:alert(1)) ![RS-02](x.png) ![](data:image/png;base64,AAAA)", f"<p>x {ref('RS-02')} data:image/png;base64,AAAA</p>"),
            ("![<img src=x onerror=alert(1)>](https://example.com/x.png)",
             f"<p>{link('https://example.com/x.png', '&lt;img src=x onerror=alert(1)&gt;')}</p>"),
        ])

    def test_the_stress_boards_hostile_description(self):
        desc = ("<script>alert(1)</script>\n\n[js link](javascript:alert(1)) and [ok](https://example.com)\n\n"
                "| a | b |\n|---|---|\n| 1 | 2 |\n\n![img](https://example.com/x.png)\n\n- [ ] first\n- [x] second\n  - nested item\n\n"
                "1. one\n2. two\n\n# Heading one\n\n> quote with RS-02 reference")
        self.check([(desc, (
            "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"
            f"<p>js link and {link('https://example.com', 'ok')}</p>"
            + table(["a", "b"], ["1", "2"])
            + f"<p>{link('https://example.com/x.png', 'img')}</p>"
            '<ul><li class="task"><input type="checkbox" disabled> first</li>'
            '<li class="task"><input type="checkbox" disabled checked> second<ul><li>nested item</li></ul></li></ul>'
            "<ol><li>one</li><li>two</li></ol><h3>Heading one</h3>"
            f"<blockquote><p>quote with {ref('RS-02')} reference</p></blockquote>"))])

    def test_html_stays_escaped(self):
        self.check([
            ("<script>alert(1)</script>", "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"),
            ("<img src=x onerror=alert(1)> **bold** `code`", "<p>&lt;img src=x onerror=alert(1)&gt; <strong>bold</strong> <code>code</code></p>"),
            ("# <i>RS-02</i>", f"<h3>&lt;i&gt;{ref('RS-02')}&lt;/i&gt;</h3>"),
            ("```\n<b>'x'</b>\n```", "<pre><code>&lt;b&gt;&#39;x&#39;&lt;/b&gt;</code></pre>"),
            ('[<b>RS-02</b>](https://example.com/"><script>)',
             f"<p>{link('https://example.com/&quot;&gt;&lt;script&gt;', '&lt;b&gt;RS-02&lt;/b&gt;')}</p>"),
            ('https://example.com/a"onmouseover="x', f"<p>{link('https://example.com/a&quot;onmouseover=&quot;x', 'https://example.com/a&quot;onmouseover=&quot;x')}</p>"),
            ("[a](javascript:x) [b](JavaScript:x) [c](data:text/html,x) [d](//example.com)", "<p>a b c d</p>"),
        ])

    def test_emphasis_and_links(self):
        e = "https://e.com"
        self.check([
            ("*a* _b_ **c** ~~d~~", "<p><em>a</em> <em>b</em> <strong>c</strong> <del>d</del></p>"),
            ("**a *b* c** and *a **b** c*", "<p><strong>a <em>b</em> c</strong> and <em>a <strong>b</strong> c</em></p>"),
            # Not inside words. Emphasis runs to the first closer after its opener, and an opener without one is text.
            ("a*b*c, snake_case_name and 2*3*4", "<p>a*b*c, snake_case_name and 2*3*4</p>"),
            ("*a *b c* and _a _b c_", "<p><em>a *b c</em> and <em>a _b c</em></p>"),
            ("x ** y* and * a* and *a *", "<p>x <em>* y</em> and * a* and <em>a </em></p>"),
            ("*a* *b _c_ _d", "<p><em>a</em> *b <em>c</em> _d</p>"),
            # A link's text runs from its "[" to the first "]" after it.
            (f"[a [b]({e}) and [x] [y]({e}) [z]", f"<p>{link(e, 'a [b')} and [x] {link(e, 'y')} [z]</p>"),
            (f"![x]({e}) !![y]({e}) [![z]({e})", f"<p>{link(e, 'x')} !{link(e, 'y')} {link(e, '![z')}</p>"),
            (f"[`a`]({e}) and [a `b]({e})` c", f"<p>{link(e, '`a`')} and {link(e, 'a `b')}` c</p>"),
            (f"`[`[b]({e}) `a` and [c]({e})`", f"<p><code>[</code>{link(e, 'b')} <code>a</code> and {link(e, 'c')}`</p>"),
            (f"[a]]]({e}) [a] ({e})", f"<p>[a]]]({link(e, e)}) [a] ({link(e, e)})</p>"),
            (f"[*a* RS-02]({e}) [a](javascript:x) [RS-02](#x)", f"<p>{link(e, '<em>a</em> RS-02')} a {ref('RS-02')}</p>"),
        ])

    def test_long_lines_of_unclosed_openers_render_quickly(self):
        # 100 KB of each, then the end of the line: [unit, end, the HTML of the end].
        cases = [[" *a", "", ""], [" _a", "", ""], ["(*a", "", ""], ["[", "", ""], ["[a", "", ""], ["![", "", ""], [" *a _a [a", "", ""],
                 ["[ *a", "", ""], ["[a", "]", "]"], [" *a", " _b_", " <em>b</em>"], [" _a", " [c](https://e.com)", f" {link('https://e.com', 'c')}"]]
        code = ("const get = () => null;\n"
                "process.stdout.write(JSON.stringify(input.map(([unit, end, endHtml]) => {\n"
                "  const head = unit.repeat(Math.ceil(100000 / unit.length)), t = process.hrtime.bigint(), html = markdown(head + end);\n"
                "  return [Number(process.hrtime.bigint() - t) / 1e6, html === `<p>${escHtml(head)}${endHtml}</p>`];\n"
                "})));")
        out = json.loads(run_html_js(self, code, json.dumps(cases), [("function inlineMd(", "\n// ----")]))
        for (unit, end, _), (ms, same) in zip(cases, out):
            with self.subTest(unit=unit, end=end):
                self.assertTrue(same)
                self.assertLess(ms, 1000)

    def test_line_separators_are_part_of_their_line(self):
        # U+2028 and U+2029 end no line, item or span. Like a no-break space, they are whitespace and stay in the text.
        L, P = "\u2028", "\u2029"
        box, ticked = '<li class="task"><input type="checkbox" disabled>', '<li class="task"><input type="checkbox" disabled checked>'
        cases = [
            (f"- a{L}b\n- [ ] c{P}d\n-{L}[x]{P}e\n- {L}\n  {L}more", f"<ul><li>a{L}b</li>{box} c{P}d</li>{ticked} e</li><li> more</li></ul>"),
            (f"1.{P}one\n   - [ ]{L}sub", f"<ol><li>one<ul>{box} sub</li></ul></li></ol>"),
            (f"# a{L}b\n#{P}c", f"<h3>a{L}b</h3><h3>c</h3>"),
            (f"*a{L}b* _c{P}d_ **e{L}f** ~~g{P}h~~ *{L}i*", f"<p><em>a{L}b</em> <em>c{P}d</em> <strong>e{L}f</strong> <del>g{P}h</del> *{L}i*</p>"),
            (f"[a{L}b](https://e.com) https://e.com/c{L}d", f"<p>{link('https://e.com', f'a{L}b')} {link('https://e.com/c', 'https://e.com/c')}{L}d</p>"),
            (f">{L}- [x] a\n> b{P}c", f"<blockquote><ul>{ticked} a</li></ul><p>b{P}c</p></blockquote>"),
            (f"| a{L} | b |\n|---|---{P}|\n| 1 | {L} |", table(["a", "b"], ["1", ""])),
            (f"```\n- {L}a\n```\na\n{L}{P}\nb{L}\n---{L}", f"<pre><code>- {L}a</code></pre><p>a</p><p>b{L}</p><hr>"),
        ]
        self.check(cases)
        code = ("const get = () => null, nbsp = (s) => s.replace(/[\\u2028\\u2029]/g, '\\u00a0');\n"
                "process.stdout.write(JSON.stringify(input.map((src) => [markdown(nbsp(src)) === nbsp(markdown(src)),\n"
                "  JSON.stringify(taskItems(nbsp(src))) === nbsp(JSON.stringify(taskItems(src)))])));")
        out = json.loads(run_html_js(self, code, json.dumps([src for src, _ in cases]), [("function inlineMd(", "\n// ----")]))
        self.assertEqual(out, [[True, True]] * len(cases))

    def test_long_texts_with_line_separators_render_quickly(self):
        # 100 KB of each, as one description: [unit, the boxes in one unit].
        cases = [["- \u2028a\n", 0], ["- [ ] a\u2028b\n", 1], ["-\u2029[x]\u2028a\n", 1], ["> \u2028- [ ] a\n", 1], ["# \u2028a\n", 0],
                 ["- a\n  \u2028b\n", 0], [" *a\u2028", 0], ["[a\u2029", 0], ["\u2028\n", 0]]
        code = ("const get = () => null;\n"
                "process.stdout.write(JSON.stringify(input.map(([unit, boxes]) => {\n"
                "  const n = Math.ceil(100000 / unit.length), src = unit.repeat(n), t = process.hrtime.bigint();\n"
                "  const html = markdown(src), items = taskItems(src), ms = Number(process.hrtime.bigint() - t) / 1e6;\n"
                "  return [ms, items.length === boxes * n && (html.match(/<input type=\"checkbox\"/g) || []).length === items.length];\n"
                "})));")
        out = json.loads(run_html_js(self, code, json.dumps(cases), [("function inlineMd(", "\n// ----")]))
        for (unit, _), (ms, found) in zip(cases, out):
            with self.subTest(unit=unit):
                self.assertTrue(found)
                self.assertLess(ms, 1000)

    def test_urls_of_many_megabytes_stay_text(self):
        # From about 8 MB a URL overflows the stack of the regex engine. The page stays up and keeps all the text.
        # [source, its text without the markup, how its HTML starts and ends]
        cases = [["see URL and *b*", "see URL and b", "<p>see ", " and <em>b</em></p>"],
                 ["[a](https://e.com) URL *b*\n\n*c*", "a URL bc", f"<p>{link('https://e.com', 'a')} ", " <em>b</em></p><p><em>c</em></p>"]]
        code = ("const get = () => null, url = 'https://e.com/' + 'a'.repeat(16 << 20);\n"
                "process.stdout.write(JSON.stringify(input.map(([src, text, head, tail]) => {\n"
                "  const html = markdown(src.replace('URL', url));\n"
                "  return html.startsWith(head) && html.endsWith(tail) && html.replace(/<[^>]*>/g, '') === text.replace('URL', url);\n"
                "})));")
        out = json.loads(run_html_js(self, code, json.dumps(cases), [("function inlineMd(", "\n// ----")]))
        self.assertEqual(out, [True, True])


class BoardViewTests(unittest.TestCase):
    """The board view on phones, where one column fills the screen."""

    def run_js(self, code, data):
        return json.loads(run_html_js(self, code, json.dumps(data), [("function columnInView(", "\n// The row of states")]))

    def test_the_row_of_states_marks_the_column_that_fills_most_of_the_screen(self):
        # Six 340px columns 10px apart on a 390px phone, as [left, right] on screen for a scroll position.
        spans = lambda scroll: [[10 + 350 * i - scroll, 350 + 350 * i - scroll] for i in range(6)]
        cases = [
            [spans(0), 0, 390, False],     # at rest on the first column
            [spans(150), 0, 390, False],   # swiped less than halfway
            [spans(190), 0, 390, False],   # past halfway, before the scroll settles
            [spans(1400), 0, 390, False],  # Review, with a sliver of Done
            [spans(1720), 0, 390, True],   # scrolled to the end
            # A phone held sideways: two columns fit, so the first of them is marked, until the end, which marks the last
            [spans(700), 0, 844, False],
            [spans(1266), 0, 844, True],
            # The board view hidden, and a board without columns
            [[[0, 0], [0, 0]], 0, 0, False],
            [[], 0, 390, True],
        ]
        out = self.run_js("process.stdout.write(JSON.stringify(input.map((c) => columnInView(...c))));", cases)
        self.assertEqual(out, [0, 0, 1, 4, 5, 2, 5, -1, -1])

    def test_cards_dropped_by_mouse_or_touch_move_and_are_refused_alike(self):
        """Both drop through dropCard. The page shows `page`, and the save replays the move on `latest`."""
        page = {"kanvas": 1, "title": "", "groups": [], "tasks": [
            task("P-01", "proposed"), task("P-02", "proposed"), task("T-01"), task("D-01", "doing"), task("R-01", "review")]}
        latest = json.loads(json.dumps(page))
        latest["tasks"][1]["desc"] = "Revised by an agent"
        latest["tasks"][2]["state"] = "doing"
        drops = [["P-01", "blocked"], ["P-01", "proposed"], ["D-01", "done"], ["R-01", "todo"], ["T-01", "blocked"],
                 ["T-01", "review"], ["P-02", "todo"], ["X-09", "done"]]
        code = ("let board = normalize(input.page), id = null;\n"
                "const get = (x) => board.tasks.find((t) => same(t.id, x));\n"
                "const out = [];\n"
                "const commit = (fn) => { const b = normalize(clone(input.latest));\n"
                "  try { fn(b); out.push(b.tasks.find((t) => same(t.id, id)).state); } catch (e) { out.push(e.message); } };\n"
                "for (const [x, s] of input.drops) { id = x; const n = out.length; dropCard(x, s); if (out.length === n) out.push(null); }\n"
                "process.stdout.write(JSON.stringify(out));")
        sections = [("function mustFind(", "\n// ----"), ("function dropCard(", "\n// The column that takes up most")]
        out = json.loads(run_html_js(self, code, json.dumps({"page": page, "latest": latest, "drops": drops}), sections))
        self.assertEqual(out, [
            "P-01 needs its PO/TL product definition first", None,
            "D-01 needs both reviews and dev merge evidence first", "todo", None,
            "T-01 is in Doing now, not To do",  # an agent started it meanwhile
            "P-02 was revised since you opened it; read the new version first",
            None,
        ])

    def test_delivery_gates_reach_done_and_production_only_with_evidence(self):
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01", "proposed")]}
        code = (
            "const b = normalize(input), apply = (op) => op(b), errors = [];\n"
            "apply(ops.define('A-01', seenOf(b.tasks[0]), { approved: true, outcome: 'Ship', needs: 'API', acceptance: 'Tests pass' }));\n"
            "apply(ops.startWork('A-01', 'Implement API and tests'));\n"
            "apply(ops.state('A-01', seenOf(b.tasks[0]), 'review'));\n"
            "try { apply(ops.mergeDev('A-01', 'PR #1')); } catch (e) { errors.push(e.message); }\n"
            "b.tasks[0].review.agent = { status: 'approved', by: 'reviewer' };\n"
            "apply(ops.manualReview('A-01', 'Checked manually'));\n"
            "apply(ops.mergeDev('A-01', 'PR #1 merged at abc123'));\n"
            "apply(ops.promote('A-01', 'main abc123; smoke test passed'));\n"
            "const t = b.tasks[0]; process.stdout.write(JSON.stringify([errors, t.state, t.review, t.delivery]));")
        errors, state, review, delivery = json.loads(run_html_js(
            self, code, json.dumps(board), [("function mustFind(", "\n// ----")]))
        self.assertEqual(errors, ["A-01 needs the manual review approved before merge to dev"])
        self.assertEqual(state, "production")
        self.assertEqual((review["agent"]["status"], review["manual"]["status"]), ("approved", "approved"))
        self.assertEqual(delivery["dev"]["evidence"], "PR #1 merged at abc123")
        self.assertEqual(delivery["production"]["evidence"], "main abc123; smoke test passed")

    def test_a_legacy_todo_can_receive_its_product_definition(self):
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01", "todo")]}
        code = ("const b = normalize(input), board = b, IX = indexOf(b), vstate = (t) => vstateIn(IX, t), before = stateMoves(b.tasks[0])[0].label;"
                "ops.define('A-01', seenOf(b.tasks[0]), { outcome: 'Ship', repositories: ['api'] })(b);"
                "process.stdout.write(JSON.stringify([before, b.tasks[0].state, b.tasks[0].definition.repositories]));")
        out = json.loads(run_html_js(self, code, json.dumps(board), [
            ("function mustFind(", "\n// ----"), ("function stateMoves(", "\nfunction stateMenu(")]))
        self.assertEqual(out, ["PO/TL Grill me", "todo", ["api"]])


class ReviewFlowTests(unittest.TestCase):
    """Marking the open task done or sending it back in the board view opens the next task in review."""

    FILTERS = [("const get = (id) =>", "\nfunction setBoard("), ("function matches(", "\nfunction render()")]
    STATE = "let board = null, IX = new Map(), COVER = new Map();\nconst ui = { view: 'board', selected: null, search: '', states: new Set(), groups: new Set() };\n"

    @staticmethod
    def board(*tasks):
        return {"kanvas": 1, "title": "", "groups": [{"id": "BE", "name": "Backend"}, {"id": "FE", "name": "Frontend"}], "tasks": list(tasks)}

    def test_the_next_task_follows_the_board_view_order_and_filters(self):
        board = self.board(
            task("FE-01", "review", "FE"), task("BE-01", "review", "BE"), task("BE-02", "done", "BE"),
            task("FE-02", "review", "FE", title="Forecast chart"), task("BE-03", "review", "BE"), task("X-01", "review", "XX"),
            task("G-01", "review"), task("BE-04", "review", "BE", notes=[{"at": "", "by": "claude", "text": "Reads the forecast cache"}]),
            task("FE-03", "doing", "FE"))
        # The task reviewed, the filters, and what else changed by the time the change was saved
        cases = [
            ["BE-01", {}, {}],                           # the done BE-02 is skipped
            ["BE-04", {}, {}],                           # on to the next group
            ["FE-02", {}, {}],                           # an unknown group comes after the groups, with the ungrouped tasks
            ["X-01", {}, {}],
            ["G-01", {}, {}],                            # round to the first
            ["FE-02", {"groups": ["FE"]}, {}],           # round, within the group
            ["FE-02", {"groups": ["FE"]}, {"FE-01": "done"}],
            ["BE-03", {"groups": ["FE"]}, {}],           # opened from elsewhere: its place in the order still counts
            ["X-01", {"groups": [""]}, {}],              # No group holds the tasks of unknown groups
            ["G-01", {"groups": [""]}, {}],
            ["FE-02", {"search": "forecast"}, {}],       # BE-04 matches by its note
            ["BE-04", {"search": "FRONTEND"}, {}],       # and groups by their name
            ["BE-01", {"states": ["review"]}, {}],       # the reviewed task no longer shows, and keeps its place
            ["BE-01", {"states": ["doing"]}, {}],        # no Review column
            ["BE-01", {}, {"BE-03": "done"}],            # moved on by YOLO meanwhile
            ["FE-02", {}, {"FE-03": "review"}],          # finished by an agent meanwhile
            ["BE-01", {}, {"BE-03": "done", "BE-04": "done", "FE-01": "done", "FE-02": "done", "X-01": "done", "G-01": "done"}],
            ["ZZ-09", {}, {}],                           # deleted meanwhile: from the top
        ]
        code = self.STATE + (
            "const out = [];\n"
            "for (const [id, f, changes] of input.cases) {\n"
            "  board = normalize(clone(input.board));\n"
            "  for (const t of board.tasks) if (same(t.id, id)) t.state = 'done';\n"
            "  for (const [x, s] of Object.entries(changes)) board.tasks.find((t) => t.id === x).state = s;\n"
            "  IX = indexOf(board);\n"
            "  Object.assign(ui, { groups: new Set(f.groups || []), states: new Set(f.states || []), search: f.search || '' });\n"
            "  out.push(nextReview(board, id, matches)?.id ?? null);\n"
            "}\n"
            "process.stdout.write(JSON.stringify({ order: boardOrder(normalize(input.board)).map((t) => t.id), next: out }));")
        out = json.loads(run_html_js(self, code, json.dumps({"board": board, "cases": cases}), self.FILTERS))
        self.assertEqual(out["order"], ["BE-01", "BE-02", "BE-03", "BE-04", "FE-01", "FE-02", "FE-03", "X-01", "G-01"])
        self.assertEqual(out["next"], [
            "BE-03", "FE-01", "X-01", "G-01", "BE-01",
            "FE-01", None, "FE-01",
            "G-01", "X-01",
            "BE-04", "FE-01",
            "BE-03", None,
            "BE-04", "FE-03", None, "BE-01",
        ])

    def test_moving_on_waits_for_the_save_and_reads_the_board_it_wrote(self):
        board = self.board(task("FE-01", "review", "FE"), task("BE-01", "done", "BE"), task("FE-02", "review", "FE"),
                           task("BE-03", "review", "BE"), task("FE-03", "doing", "FE"), task("BE-04", "review", "BE"), task("G-01"))
        lost = "Factory stopped running or cannot be reached"
        cases = [
            {"id": "BE-03", "undo": True},
            {"id": "BE-03", "undo": "FE-02"},  # Undo leaves the panel alone once the human opened another task
            {"id": "BE-03", "agent": {"BE-04": "done", "FE-03": "review"}},
            {"id": "BE-03", "fail": "server answered 500"},
            {"id": "BE-03", "lost": lost},
            {"id": "BE-03", "view": "graph"},
            {"id": "BE-03", "open": "FE-01"},  # from the card menu, with another task open
            {"id": "BE-03", "open": None},     # with no task open
            {"id": "BE-03", "state": "doing", "note": "Sent back: Cap the retries"},
            {"id": "BE-03", "agent": {"BE-04": "done", "FE-01": "done", "FE-02": "done"}},
            {"id": "FE-02", "groups": ["FE"], "agent": {"FE-01": "done"}},
            {"id": "FE-02", "groups": ["FE"], "agent": {"FE-01": "done", "BE-04": "done"}},
            {"id": "BE-03", "during": {"selected": "FE-02"}},
            {"id": "BE-03", "during": {"editingDesc": True}},
        ]
        code = self.STATE + (
            "let confirmed, store, lost = null, pending = 0, writes = 0, saveQueue = Promise.resolve();\n"
            "const edits = { undo: [], redo: [] }, toasts = [];\n"
            "const setBoard = (b) => { board = b; IX = indexOf(b); }, render = () => {}, setSync = () => {};\n"
            "const remember = (before, after) => { const e = { before, after }; edits.undo.push(e); return e; };\n"
            "const forget = (e) => { edits.undo = edits.undo.filter((x) => x !== e); };\n"
            "const select = (id) => { ui.selected = id; }, shownCard = () => null;\n"
            "globalThis.document = { activeElement: null, createTextNode: (data) => ({ data }), querySelector: () => ({ contains: () => false }) };\n"
            "h = (tag, props, ...kids) => ({ kids });\n"
            "toast = (msg) => { const t = { msg, remove() { t.gone = true; } }; toasts.push(t); return t; };\n"
            "const text = (t) => (typeof t.msg === 'string' ? t.msg : t.msg[0].kids[0].data) + (t.gone ? ' (taken away)' : '');\n"
            "(async () => {\n"
            "  const out = [];\n"
            "  for (const c of input.cases) {\n"
            "    board = normalize(clone(input.board)); confirmed = clone(board); IX = indexOf(board);\n"
            "    edits.undo = []; toasts.length = 0; lost = c.lost || null;\n"
            "    Object.assign(ui, { view: c.view || 'board', selected: 'open' in c ? c.open : c.id, editingDesc: false, editingNote: null, groups: new Set(c.groups || []) });\n"
            "    const latest = normalize(clone(input.board));\n"
            "    for (const [x, s] of Object.entries(c.agent || {})) latest.tasks.find((t) => t.id === x).state = s;\n"
            "    store = { load: async () => clone(confirmed), save: async (fn) => {\n"
            "      await new Promise((r) => setTimeout(r, 5));\n"
            "      Object.assign(ui, c.during || {});\n"
            "      if (c.fail) throw new Error(c.fail);\n"
            "      const b = clone(latest); fn(b); return { board: b, base: latest };\n"
            "    } };\n"
            "    const target = c.state || 'develop', direct = (b) => { const x = mustFind(b, c.id); x.state = target;"
            "      if (c.note) (x.notes ||= []).push({ at: '2026-10-29T10:00:00Z', by: 'human', text: c.note }); };\n"
            "    await review(get(c.id), target, c.note || '', null, direct);\n"
            "    await saveQueue;\n"
            "    const t = get(c.id), r = { open: ui.selected, toasts: toasts.map(text), saved: [t.state, t.notes.map((n) => n.text)] };\n"
            "    if (c.undo) { if (c.undo !== true) ui.selected = c.undo; const e = edits.undo[edits.undo.length - 1]; if (e.onUndo) e.onUndo(); r.afterUndo = ui.selected; }\n"
            "    out.push(r);\n"
            "  }\n"
            "  process.stdout.write(JSON.stringify(out));\n"
            "})();")
        sections = [("async function commit(", "async function poll("), ("function undoable(", "\nfunction renderUndo("),
                    ("const MOVE_WORDS =", "\n// What a change did"), ("function mustFind(", "\n// ----"),
                    ("async function review(", "\nasync function askChanges(")] + self.FILTERS
        out = json.loads(run_html_js(self, code, json.dumps({"board": board, "cases": cases}), sections))
        done, kept = ["develop", []], ["review", []]
        self.assertEqual(out, [
            {"open": "BE-04", "toasts": ["Recorded the dev merge of BE-03. Next to review: BE-04."], "saved": done, "afterUndo": "BE-03"},
            {"open": "BE-04", "toasts": ["Recorded the dev merge of BE-03. Next to review: BE-04."], "saved": done, "afterUndo": "FE-02"},
            # The next task comes from the board as saved, with the agents' changes
            {"open": "FE-01", "toasts": ["Recorded the dev merge of BE-03. Next to review: FE-01."], "saved": done},
            # A failed save takes the toast away, and one refused at once never shows it
            {"open": "BE-03", "toasts": ["Recorded the dev merge of BE-03 (taken away)", "Not saved: server answered 500"], "saved": kept},
            {"open": "BE-03", "toasts": [f"Not saved: {lost}"], "saved": kept},
            {"open": "BE-03", "toasts": ["Recorded the dev merge of BE-03"], "saved": done},
            {"open": "FE-01", "toasts": ["Recorded the dev merge of BE-03"], "saved": done},
            {"open": None, "toasts": ["Recorded the dev merge of BE-03"], "saved": done},
            {"open": "BE-04", "toasts": ["Sent BE-03 back. Next to review: BE-04."], "saved": ["doing", ["Sent back: Cap the retries"]]},
            {"open": "BE-03", "toasts": ["Recorded the dev merge of BE-03. No more tasks to review."], "saved": done},
            {"open": "FE-02", "toasts": ["Recorded the dev merge of FE-02. No more tasks to review in this view; 2 more are filtered out."], "saved": done},
            {"open": "FE-02", "toasts": ["Recorded the dev merge of FE-02. No more tasks to review in this view; 1 more is filtered out."], "saved": done},
            # The human went on meanwhile: to another task, or to editing this one
            {"open": "FE-02", "toasts": ["Recorded the dev merge of BE-03"], "saved": done},
            {"open": "BE-03", "toasts": ["Recorded the dev merge of BE-03"], "saved": done},
        ])


class PanelPlaceTests(unittest.TestCase):
    """Every way of opening a task goes through select(), and the panel shows each task from its top."""

    def test_another_task_opens_at_the_top_and_the_same_one_keeps_the_readers_place(self):
        board = {"kanvas": 1, "title": "", "groups": [], "tasks": [task("A-01", "doing"), task("B-01", "review")]}
        # The panel is 300px tall, and each step reports the task on show and its scroll position, or None once closed
        steps = [
            ["open", "A-01"],                         # the panel opens at the top
            ["scroll", 900], ["render"],              # an agent's change elsewhere, or a save, keeps the place
            ["tall", 2600], ["render"],               # a note added below
            ["tall", 1000], ["render"],               # a shorter task keeps what is left of the place
            ["open", "B-01"],                         # another task starts at the top
            ["scroll", 500], ["type"], ["render"],    # no rebuild while a text field has focus
            ["leave"], ["render"],                    # and none moves it once the field lets go
            ["type"], ["open", "A-01"],               # a link or the address bar opens another task while typing
            ["scroll", 400], ["open", "A-01"],        # its own card, or a link to itself
            ["close"], ["open", "A-01"],              # opened again
            ["scroll", 300], ["close"], ["open", "B-01"],
        ]
        code = (
            "const board = normalize(input.board), get = (id) => board.tasks.find((t) => same(t.id, id));\n"
            "const ui = { selected: null, editingDesc: false, editingNote: null, panelStale: false };\n"
            "const drafts = { title: null, desc: null, descBase: null, noteEdit: null, back: new Map() };\n"
            "const phone = { matches: false }, history = { state: null, pushState() {}, replaceState() {}, back() {} }, location = { pathname: '/', search: '' };\n"
            "const tall = { 'A-01': 2000, 'B-01': 1200 }, body = { matches: () => false };\n"
            "const field = { matches: () => true, blur() { document.activeElement = body; } };\n"
            "const panel = { hidden: true, dataset: {}, height: 0, offset: 0, querySelectorAll: () => [], contains: (el) => el === panel || el === field,\n"
            "  focus() { document.activeElement = panel; }, get scrollTop() { return this.offset; }, set scrollTop(v) { this.offset = Math.max(0, Math.min(v, this.height - 300)); } };\n"
            "globalThis.document = { activeElement: body, querySelector: () => panel };\n"
            "const showPanel = (p, open) => { p.hidden = !open; }, render = () => renderPanel(), reveal = () => {};\n"
            # The browser keeps the offset through a rebuild as far as the new content allows, and shifts it as the
            # title grows to a second line.
            "const fillPanel = (p, t) => { showPanel(p, true); p.height = tall[t.id]; p.scrollTop += 24; };\n"
            "const out = [];\n"
            "for (const [what, arg] of input.steps) {\n"
            "  if (what === 'open') select(arg);\n"
            "  if (what === 'close') select(null);\n"
            "  if (what === 'render') renderPanel();\n"
            "  if (what === 'scroll') { panel.scrollTop = arg; continue; }\n"
            "  if (what === 'tall') { tall[ui.selected] = arg; continue; }\n"
            "  if (what === 'type') { document.activeElement = field; continue; }\n"
            "  if (what === 'leave') { document.activeElement = body; continue; }\n"
            "  out.push([ui.selected, panel.hidden ? null : panel.scrollTop]);\n"
            "}\n"
            "process.stdout.write(JSON.stringify(out));")
        sections = [("function select(", "\n// A note that failed to save"), ("const tabbables =", "\nfunction fillPanel(")]
        out = json.loads(run_html_js(self, code, json.dumps({"board": board, "steps": steps}), sections))
        self.assertEqual(out, [
            ["A-01", 0],
            ["A-01", 900], ["A-01", 900], ["A-01", 700],
            ["B-01", 0],
            ["B-01", 500], ["B-01", 500],
            ["A-01", 0],
            ["A-01", 400],
            [None, None], ["A-01", 0],
            [None, None], ["B-01", 0],
        ])


class ShortcutLabelTests(unittest.TestCase):
    def test_apple_keyboards_show_command_where_the_others_show_ctrl(self):
        code = ("process.stdout.write(JSON.stringify(input.map((platform) => {\n"
                "  Object.defineProperty(globalThis, 'navigator', { value: { platform }, configurable: true });\n"
                "  return [keyLabel('Ctrl+Shift+Z / Ctrl+Y'), keyLabel('Deleted 2 tasks. Ctrl+Z brings them back.')];\n"
                "})));")
        out = json.loads(run_html_js(self, code, json.dumps(["MacIntel", "iPad", "Win32", "Linux x86_64", ""])))
        mac = ["⌘+Shift+Z / ⌘+Y", "Deleted 2 tasks. ⌘+Z brings them back."]
        other = ["Ctrl+Shift+Z / Ctrl+Y", "Deleted 2 tasks. Ctrl+Z brings them back."]
        self.assertEqual(out, [mac, mac, other, other, other])

    def test_the_shortcuts_dialog_lists_every_shortcut_once_in_four_groups(self):
        code = "process.stdout.write(JSON.stringify(SHORTCUTS.map(([head, keys]) => [head, keys.map(([k]) => k)])));"
        groups = json.loads(run_html_js(self, code, "null", [("const SHORTCUTS =", "\nfunction helpDialog")]))
        self.assertEqual([head for head, _ in groups], ["Moving around", "The selected task", "Views", "Editing"])
        keys = [k for _, ks in groups for k in ks]
        self.assertEqual(len(keys), 24)
        self.assertEqual(len(set(keys)), len(keys))
        self.assertIn("Enter", groups[0][1])
        self.assertIn("Ctrl+Z", groups[3][1])


class CardLabelTests(unittest.TestCase):
    def test_a_card_is_named_by_its_id_title_state_and_who_its_foot_shows(self):
        with open(os.path.join(ROOT, "examples", "sample.json"), encoding="utf-8") as f:
            b = json.load(f)
        next(t for t in b["tasks"] if t["id"] == "FE-02")["notes"].append({"by": "codex", "at": "2026-10-20T10:00:00Z", "text": "Question: metric or imperial?"})
        code = ("const b = normalize(input), ix = indexOf(b);\n"
                "process.stdout.write(JSON.stringify(Object.fromEntries(b.tasks.map((t) => [t.id, cardLabel(t, vstateIn(ix, t), unmetIn(ix, t))]))));")
        names = json.loads(run_html_js(self, code, json.dumps(b), [("function cardLabel(", "\nfunction cardFoot(")]))
        self.assertEqual(names["BE-02"], "BE-02 Fetch and cache forecasts, Review, claude")
        self.assertEqual(names["BE-03"], "BE-03 City search endpoint, Doing, codex")
        self.assertEqual(names["FE-02"], "FE-02 Forecast cards, Waiting for you, codex")
        self.assertEqual(names["FE-03"], "FE-03 City search box, Blocked, waiting on FE-02, BE-03")
        self.assertEqual(names["FE-05"], "FE-05 Offline banner, Proposed, codex")
        self.assertEqual(names["FE-04"], "FE-04 Dark mode, To do")
        self.assertEqual(names["RS-01"], "RS-01 Compare weather APIs, Done")


class WorkflowTests(TempBoard):
    def test_status_lists_what_matters(self):
        _, out, _ = self.run_cli("status")
        self.assertIn("Ready to start (1)", out)
        self.assertIn("DV-03  Task DV-03  (waiting on DV-02)", out)
        self.assertIn("Proposed, awaiting human approval (1)", out)
        _, out, _ = self.run_cli("ready")
        self.assertEqual(out.split(), ["DV-02", "Task", "DV-02"])

    def test_status_names_an_untitled_board_as_the_boards_page_does(self):
        board = self.board()
        board["title"] = ""
        write_board(self.path, board)
        _, out, _ = self.run_cli("status")
        self.assertEqual(out.splitlines()[0], f"{kanvas.board_name(self.path)}  (kanvas.json)")

    def test_agents_cannot_start_blocked_proposed_or_running_tasks(self):
        self.assertIn("blocked by unfinished DV-02", self.fails("start", "DV-03", "-m", "Plan"))
        self.assertIn("a human must approve it", self.fails("start", "DV-04", "-m", "Plan"))
        self.run_cli("start", "DV-02", "-m", "Plan")
        self.assertEqual(self.get("DV-02")["state"], "doing")
        self.assertIn("already in progress", self.fails("start", "DV-02", "-m", "Plan"))

    def test_finish_requires_a_summary(self):
        self.run_cli("start", "DV-02", "-m", "Plan")
        self.assertIn("needs a summary", self.fails("finish", "DV-02"))
        self.assertEqual(self.get("DV-02")["state"], "doing")
        self.run_cli("finish", "DV-02", "--as", "claude", "-m", "Parser done; 12 tests pass")
        t = self.get("DV-02")
        self.assertEqual(t["state"], "review")
        self.assertEqual((t["notes"][-1]["by"], t["notes"][-1]["text"]), ("claude", "Parser done; 12 tests pass"))
        self.assertIn("only tasks in progress", self.fails("finish", "DV-02", "-m", "again"))

    def test_start_requires_an_approved_definition_and_records_git_worktrees(self):
        board = self.board()
        kanvas.find_task(board, "DV-02").pop("definition")
        write_board(self.path, board)
        self.assertIn("no approved product definition", self.fails("start", "DV-02", "-m", "Plan"))
        board = self.board()
        kanvas.find_task(board, "DV-02")["definition"] = {"approved": True, "outcome": "Ship it"}
        write_board(self.path, board)
        self.run_cli("start", "DV-02", "--as", "codex", "-m", "Plan", "--worktree", ROOT)
        work = self.get("DV-02")["work"]
        self.assertEqual((work["by"], work["plan"]), ("codex", "Plan"))
        self.assertEqual(len(work["git"]), 1)
        self.assertEqual(os.path.normcase(work["git"][0]["worktree"]), os.path.normcase(ROOT))
        self.assertRegex(work["git"][0]["commit"], r"^[0-9a-f]{40}$")

    def test_agent_review_is_independent_and_changes_return_to_doing(self):
        self.run_cli("start", "DV-02", "--as", "codex", "-m", "Plan")
        self.run_cli("finish", "DV-02", "--as", "codex", "-m", "Implemented and tested")
        self.assertIn("needs another identity", self.fails(
            "agent-review", "DV-02", "--as", "codex", "--approve", "-m", "Looks good"))
        self.run_cli("agent-review", "DV-02", "--as", "claude", "--approve", "-m", "Reviewed tests and diff")
        t = self.get("DV-02")
        self.assertEqual(t["state"], "review")
        self.assertEqual(t["review"]["agent"]["status"], "approved")
        self.assertEqual(t["review"]["manual"]["status"], "pending")
        self.run_cli("agent-review", "DV-02", "--as", "claude", "--changes", "-m", "Add the missing rollback test")
        self.assertEqual(self.get("DV-02")["state"], "doing")

    def test_pause_returns_the_task_with_a_note(self):
        self.run_cli("start", "DV-02", "-m", "Plan: write the parser first")
        self.run_cli("pause", "DV-02", "-m", "Waiting for an API key")
        t = self.get("DV-02")
        self.assertEqual(t["state"], "todo")
        self.assertEqual([n["text"] for n in t["notes"]], ["Plan: write the parser first", "Waiting for an API key"])

    def test_author_comes_from_as_or_environment(self):
        self.run_cli("note", "DV-02", "from env", env={"KANVAS_AGENT": "codex"})
        self.run_cli("note", "DV-02", "--as", "gemini", "after an option")
        self.run_cli("note", "DV-02", "plain")
        self.assertEqual([n["by"] for n in self.get("DV-02")["notes"]], ["codex", "gemini", "agent"])

    def test_proposals_are_signed_like_notes(self):
        self.run_cli("propose", "Development", "Signed", env={"KANVAS_AGENT": "codex"})
        self.run_cli("batch", "--as", "gemini", stdin=json.dumps({"tasks": [{"title": "From a batch"}]}).encode())
        signed = {t["title"]: t for t in self.board()["tasks"] if t["title"] in ("Signed", "From a batch")}
        self.assertEqual(signed["Signed"]["by"], "codex")
        self.assertEqual(signed["From a batch"]["by"], "gemini")
        self.assertRegex(signed["Signed"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.run_cli("edit", signed["Signed"]["id"], "--title", "Signed and revised", "--as", "claude")
        self.assertEqual(self.get(signed["Signed"]["id"])["by"], "codex")
        self.assertIn("By:     codex", self.run_cli("show", signed["Signed"]["id"])[1])

    def test_utf8_from_stdin(self):
        text = "Café — naïve ✓ 日本語\nsecond line: ü"
        self.run_cli("note", "DV-02", "--stdin", stdin=text.encode("utf-8"))
        self.run_cli("propose", "Development", "Übersicht ✓", "--stdin", stdin=text.encode("utf-8"))
        self.assertEqual(self.get("DV-02")["notes"][0]["text"], text)
        self.assertEqual(self.get("DV-05")["title"], "Übersicht ✓")
        self.assertEqual(self.get("DV-05")["desc"], text)
        _, out, _ = self.run_cli("show", "DV-05")
        self.assertIn("Café — naïve ✓ 日本語", out)

    def test_show_marks_edited_notes(self):
        board = kanvas.parse_board(self.raw())
        board["tasks"][1]["notes"] = [{"at": "2026-10-19T08:00:00Z", "by": "claude", "edited": "2026-10-21T10:16:00Z", "text": "Parser done"}]
        write_board(self.path, board)
        _, out, _ = self.run_cli("show", board["tasks"][1]["id"])
        self.assertIn("2026-10-19 08:00:00 UTC  claude, edited 2026-10-21 10:16:00 UTC:", out)

    def test_multiline_text_from_a_file(self):
        notes = os.path.join(self.dir, "summary.md")
        with open(notes, "wb") as f:
            f.write("\ufeffDone:\r\n- parser\r\n- tests\r\n".encode("utf-8"))
        self.run_cli("start", "DV-02", "-m", "Plan")
        self.run_cli("finish", "DV-02", "--file", notes)
        self.assertEqual(self.get("DV-02")["notes"][-1]["text"], "Done:\n- parser\n- tests")

    def test_start_reads_the_plan_from_stdin_or_a_file(self):
        plan = "Plan ✓:\n- parse\n- test"
        self.run_cli("start", "DV-02", "--stdin", stdin=plan.encode("utf-8"))
        self.assertEqual(self.get("DV-02")["notes"][-1]["text"], plan)
        self.run_cli("pause", "DV-02")
        path = os.path.join(self.dir, "plan.md")
        with open(path, "wb") as f:
            f.write("\ufeffPlan:\r\n- again\r\n".encode("utf-8"))
        self.run_cli("start", "DV-02", "--file", path)
        self.assertEqual(self.get("DV-02")["notes"][-1]["text"], "Plan:\n- again")
        self.run_cli("pause", "DV-02")
        before = self.raw()
        self.assertIn("only once", self.fails("start", "DV-02", "-m", "inline", "--stdin", stdin=b"piped"))
        self.assertEqual(self.raw(), before)

    def test_text_may_be_given_only_once(self):
        self.assertIn("only once", self.fails("note", "DV-02", "inline", "--stdin", stdin=b"piped"))

    def test_done_tasks_are_frozen_for_agents(self):
        self.assertIn("propose a follow-up", self.fails("note", "DV-01", "late"))
        self.assertIn("dependencies can no longer change", self.fails("depend", "DV-01", "DV-04"))

    def test_depend_rejects_cycles_and_self(self):
        self.run_cli("depend", "DV-04", "DV-03")
        self.assertEqual(self.get("DV-04")["deps"], ["DV-03"])
        self.assertIn("circular", self.fails("depend", "DV-02", "DV-04"))
        self.assertIn("circular", self.fails("depend", "DV-04", "DV-04"))
        _, out, _ = self.run_cli("depend", "DV-04", "dv-03")
        self.assertIn("already depends", out)

    def test_failed_commands_leave_the_file_untouched(self):
        before = self.raw()
        self.fails("start", "DV-03")
        self.fails("propose", "Nope", "Title")
        self.fails("depend", "DV-02", "DV-03")
        self.assertEqual(self.raw(), before)

    def test_board_is_found_from_a_subdirectory(self):
        sub = os.path.join(self.dir, "src", "deep")
        os.makedirs(sub)
        _, out, _ = self.run_cli("ready", cwd=sub)
        self.assertIn("DV-02", out)

    def test_check_exit_code(self):
        _, out, _ = self.run_cli("check")
        self.assertTrue(out.startswith("OK: 4 tasks"))
        board = self.board()
        board["tasks"].append(task("DV-05", deps=["DV-77"]))
        write_board(self.path, board)
        self.assertNotEqual(self.run_cli("check", check=False)[0], 0)


class AutomationTests(TempBoard):
    def automated_board(self, repo):
        board = self.board()
        board["automation"] = {
            "enabled": True,
            "project_root": self.dir,
            "worktrees_root": ".kanvas/worktrees",
            "runs_root": ".kanvas/runs",
            "repositories": [{"id": "front", "name": "Frontend", "path": repo, "base": "HEAD"}],
            "design": False,
            "ci": False,
        }
        t = kanvas.find_task(board, "DV-02")
        t["state"] = "doing"
        t["definition"]["repositories"] = ["front"]
        t["work"] = {"by": "human", "at": kanvas.now_iso(), "plan": "Implement and test", "repositories": ["front"]}
        return board

    def test_prepares_an_isolated_worktree_for_each_selected_repository(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        subprocess.run(["git", "init", repo], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
        with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
            f.write("front\n")
        subprocess.run(["git", "-C", repo, "add", "README.md"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, capture_output=True)
        board = self.automated_board(repo)
        config = kanvas.automation_config(board, self.path)
        worktrees = kanvas.prepare_automation_worktrees(kanvas.find_task(board, "DV-02"), config)
        self.assertEqual(len(worktrees), 1)
        self.assertTrue(os.path.isfile(os.path.join(worktrees[0]["worktree"], "README.md")))
        self.assertEqual(subprocess.run(["git", "-C", worktrees[0]["worktree"], "branch", "--show-current"],
                                        capture_output=True, text=True, check=True).stdout.strip(), worktrees[0]["branch"])
        retried = kanvas.prepare_automation_worktrees(kanvas.find_task(board, "DV-02"), config)
        self.assertEqual(retried[0]["worktree"], worktrees[0]["worktree"])

    def test_retry_clears_a_failed_run_for_the_dispatcher(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        task = kanvas.find_task(board, "DV-02")
        task["work"]["automation"] = {"status": "failed", "summary": "bad flags"}
        write_board(self.path, board)
        _, out, _ = self.run_cli("retry", "DV-02", "--as", "codex")
        self.assertIn("Queued retry", out)
        task = self.get("DV-02")
        self.assertNotIn("automation", task["work"])
        self.assertIn("Retrying failed automation", task["notes"][-1]["text"])

    def test_configurable_branch_pattern_uses_task_id(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        board["automation"]["branch_pattern"] = "feature/{id}"
        config = kanvas.automation_config(board, self.path)
        self.assertEqual(kanvas.automation_branch(kanvas.find_task(board, "DV-02"), config), "feature/DV-02")

    def fake_worktrees(self, repo):
        return [{"id": "front", "name": "Frontend", "path": repo, "base": "origin/dev",
                 "branch": "feature/DV-02", "worktree": repo, "commit": "a" * 40}]

    def run_claude(self, repo, events, returncode=0, pulls=None, publish_error=None, agent=None, **extra):
        board = self.automated_board(repo)
        board["automation"].update(extra)
        if agent:
            kanvas.find_task(board, "DV-02")["definition"]["agent"] = agent
        write_board(self.path, board)

        def completed(command, **kwargs):
            runs = os.path.join(self.dir, ".kanvas", "runs")
            with open(os.path.join(runs, "dv-02.jsonl"), "w", encoding="utf-8") as f:
                for event in events:
                    f.write((event if isinstance(event, str) else json.dumps(event)) + "\n")
            return mock.Mock(returncode=returncode)

        publish = mock.Mock(side_effect=publish_error, return_value=pulls or [])
        with mock.patch.object(kanvas, "prepare_automation_worktrees", return_value=self.fake_worktrees(repo)), \
             mock.patch.object(kanvas, "publish_pull_requests", publish), mock.patch.object(kanvas, "save_changes"), \
             mock.patch.object(kanvas, "agent_run", side_effect=completed) as run:
            kanvas.run_automation(self.path, "DV-02")
        return run, publish

    def test_codex_is_no_longer_an_executor(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        board["automation"]["executor"] = "codex"
        with self.assertRaises(kanvas.KanvasError) as caught:
            kanvas.automation_config(board, self.path)
        self.assertIn("Claude", str(caught.exception))
        board["automation"]["executor"] = "claude"
        self.assertIsNotNone(kanvas.automation_config(board, self.path))

    def test_claude_command_runs_headless_and_leaves_github_to_kanvas(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        board["automation"].update(model="haiku", max_turns=40, max_budget_usd=2.5)
        config = kanvas.automation_config(board, self.path)
        worktrees = self.fake_worktrees(repo) + [dict(self.fake_worktrees(repo)[0], id="api", worktree="/work/api")]
        command = kanvas.claude_command(config, worktrees)
        self.assertEqual(os.path.basename(command[0]).split(".")[0], "claude")
        self.assertEqual(command[1:3], ["-p", "--output-format"])
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", command)
        self.assertEqual(command[command.index("--model") + 1], "haiku")
        self.assertEqual(command[command.index("--max-turns") + 1], "40")
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "2.5")
        self.assertEqual(command[command.index("--add-dir") + 1], "/work/api")
        denied = command[command.index("--disallowedTools") + 1:]
        self.assertEqual(denied, ["Bash(git push:*)", "Bash(git -* push*)", "Bash(git merge:*)", "Bash(git -* merge*)",
                                  "Bash(gh:*)"])
        self.assertIn("Edit", command[command.index("--allowedTools"):command.index("--disallowedTools")])
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_claude_tool_lists_can_be_replaced(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        board["automation"].update(allowed_tools="Read Grep", disallowed_tools=["Bash(rm:*)"])
        config = kanvas.automation_config(board, self.path)
        self.assertEqual((config["allowed_tools"], config["disallowed_tools"]), (["Read", "Grep"], ["Bash(rm:*)"]))
        board["automation"]["allowed_tools"] = 5
        with self.assertRaises(kanvas.KanvasError):
            kanvas.automation_config(board, self.path)

    def test_prompt_tells_the_agent_not_to_push_or_open_pull_requests(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        prompt = kanvas.automation_prompt(board, kanvas.find_task(board, "DV-02"),
                                          kanvas.automation_config(board, self.path), self.fake_worktrees(repo))
        self.assertIn("Do not push, open pull requests, merge or deploy", prompt)
        self.assertNotIn("open a pull request targeting", prompt)

    def roles_of(self, **automation):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo, exist_ok=True)
        board = self.automated_board(repo)
        board["automation"].update(automation)
        return board, kanvas.automation_config(board, self.path)

    def test_agent_roles_start_with_the_built_in_ones_and_the_board_changes_them(self):
        _, config = self.roles_of()
        roles = config["agents"]
        self.assertEqual([roles[r]["model"] for r in ("implementer", "complex", "reviewer")], ["sonnet", "opus", "haiku"])
        self.assertEqual(roles["reviewer"]["max_turns"], 12)
        self.assertNotIn("Edit", roles["reviewer"]["allowed_tools"])
        _, config = self.roles_of(agents={"complex": {"model": "fable"}, "docs": {"model": "haiku", "effort": "low", "max_turns": 5}})
        roles = config["agents"]
        self.assertEqual(roles["complex"], {"model": "fable", "effort": "high", "timeout_minutes": 60, "max_budget_usd": 5})
        self.assertEqual(roles["docs"], {"model": "haiku", "effort": "low", "max_turns": 5})
        self.assertEqual(roles["implementer"], {"model": "sonnet", "effort": "medium", "timeout_minutes": 40, "max_budget_usd": 2})
        for name, role in roles.items():
            if name in kanvas.DEFAULT_AGENTS:
                self.assertTrue(role["timeout_minutes"] and role["max_budget_usd"], name)

    def run_process(self, code, **kwargs):
        """kanvas.agent_run over a real Python process standing in for Claude Code."""
        log = os.path.join(self.dir, "agent.jsonl")
        started = time.monotonic()
        with open(log, "w", encoding="utf-8") as output:
            proc = kanvas.agent_run([sys.executable, "-u", "-c", code], input="prompt", stdout=output, cwd=self.dir,
                                    env=dict(os.environ), **kwargs)
        return proc, time.monotonic() - started, log

    def test_a_run_that_ends_by_itself_is_left_alone(self):
        proc, _, log = self.run_process("import sys; print('got', sys.stdin.read())", limits={"timeout": 30, "idle": 30})
        self.assertEqual((proc.returncode, proc.stopped), (0, None))
        self.assertEqual(kanvas.stop_text(proc), "")
        with open(log, encoding="utf-8") as f:
            self.assertIn("got prompt", f.read())

    def mcp_board(self):
        """A board with a brain and a front repository that has an adr folder, and DV-02 with a run behind it."""
        board = self.accepted_board()
        front, brain = os.path.join(self.dir, "front"), os.path.join(self.dir, "brain")
        os.makedirs(os.path.join(brain, "modules"), exist_ok=True)
        os.makedirs(os.path.join(front, "adr"), exist_ok=True)
        os.makedirs(os.path.join(front, "node_modules"), exist_ok=True)
        files = {os.path.join(brain, "modules", "forms.md"): "# Forms\n\nA coordinator builds a form and parents fill it in.\nExports go to Google Sheets.\n",
                 os.path.join(brain, "README.md"): "# Brain\n\nIndex of the documentation.\n",
                 os.path.join(front, "adr", "0001-chips.md"): "# ADR 1\n\nFilters in lists are chips.\n",
                 os.path.join(front, "node_modules", "junk.md"): "forms forms forms\n",
                 os.path.join(front, "secret.env"): "forms=1\n"}
        for file, text in files.items():
            with open(file, "w", encoding="utf-8") as f:
                f.write(text)
        write_board(self.path, board)
        return board

    def call(self, name, arguments=None, read_only=False):
        response = kanvas.mcp_handle(self.path, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                 "params": {"name": name, "arguments": arguments or {}}}, read_only)
        return response["result"]["content"][0]["text"], response["result"]["isError"]

    def test_the_mcp_server_answers_the_protocol_over_stdio(self):
        self.mcp_board()
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "brain_search", "arguments": {"query": "forms parents"}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "nope"}]
        proc = subprocess.run([sys.executable, SCRIPT, "--board", self.path, "mcp"], input="\n".join(map(json.dumps, requests)) + "\n",
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "kanvas")
        self.assertEqual(replies[0]["result"]["capabilities"], {"tools": {}})
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertEqual(names, {"kanvas_status", "kanvas_list", "kanvas_show", "kanvas_usage", "brain_search", "brain_read",
                                 "brain_list", "kanvas_propose", "kanvas_note"})
        self.assertTrue(all(t["inputSchema"]["type"] == "object" for t in replies[1]["result"]["tools"]))
        self.assertIn("modules/forms.md", replies[2]["result"]["content"][0]["text"])
        self.assertEqual(replies[3]["error"]["code"], -32601)

    def test_the_mcp_tools_read_the_board(self):
        self.mcp_board()
        status, failed = self.call("kanvas_status")
        self.assertFalse(failed)
        self.assertIn('"done"', status)
        listed = json.loads(self.call("kanvas_list", {"filter": "done"})[0])
        self.assertIn("DV-02", [t["id"] for t in listed])
        self.assertTrue(all(t["state"] == "done" for t in listed))
        shown = json.loads(self.call("kanvas_show", {"id": "dv-02"})[0])
        self.assertEqual(shown["id"], "DV-02")
        self.assertEqual(shown["run"]["status"], "completed")
        self.assertIn("Added the city filter", shown["run"]["summary"])
        text, failed = self.call("kanvas_show", {"id": "NOPE-9"})
        self.assertTrue(failed)
        self.assertIn("not found", text)

    def test_the_mcp_documentation_tools_search_read_and_list_only_the_documentation(self):
        self.mcp_board()
        found = json.loads(self.call("brain_search", {"query": "forms parents fill"})[0])
        self.assertEqual(found[0]["path"], "brain/modules/forms.md")
        self.assertIn("parents", found[0]["snippet"])
        self.assertEqual(self.call("brain_search", {"query": "chips"}) and json.loads(self.call("brain_search", {"query": "chips"})[0])[0]["path"],
                         "front/adr/0001-chips.md")
        self.assertIn("Exports go to Google Sheets", self.call("brain_read", {"path": "brain/modules/forms.md"})[0])
        listing = self.call("brain_list")[0].splitlines()
        self.assertEqual(listing, ["brain/README.md", "brain/modules/forms.md", "front/adr/0001-chips.md"])
        for hidden in ("front/node_modules/junk.md", "front/secret.env", "../kanvas.json", "brain/../kanvas.json", "kanvas.json"):
            text, failed = self.call("brain_read", {"path": hidden})
            self.assertTrue(failed, hidden)
        self.assertTrue(self.call("brain_search", {"query": "a"})[1])
        self.assertEqual(self.call("brain_search", {"query": "zebra giraffe"})[0], "No document matches those words.")

    def test_the_mcp_tools_let_an_agent_propose_and_note_but_not_decide(self):
        self.mcp_board()
        text, failed = self.call("kanvas_propose", {"title": "Export players to a sheet", "description": "A club asked for it", "group": "DV"})
        self.assertFalse(failed)
        new_id = text.split()[1].rstrip(":")
        added = self.get(new_id)
        self.assertEqual((added["state"], added["by"], added["title"]), ("proposed", "mcp", "Export players to a sheet"))
        self.assertTrue(self.call("kanvas_note", {"id": "DV-02", "text": "late"})[1])
        self.assertFalse(self.call("kanvas_note", {"id": new_id, "text": "Estimate: 2 days"})[1])
        self.assertEqual(self.get(new_id)["notes"][-1]["text"], "Estimate: 2 days")
        self.assertTrue(self.call("kanvas_propose", {"title": "x", "group": "no such group"})[1])
        names = {t["name"] for t in kanvas.MCP_TOOLS}
        self.assertFalse(names & {"kanvas_approve", "kanvas_start", "kanvas_merge", "kanvas_stop", "kanvas_review"})

    def test_a_read_only_mcp_server_offers_and_runs_only_the_tools_that_read(self):
        self.mcp_board()
        listed = kanvas.mcp_handle(self.path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, True)["result"]["tools"]
        self.assertFalse({t["name"] for t in listed} & {"kanvas_propose", "kanvas_note"})
        text, failed = self.call("kanvas_propose", {"title": "Nope"}, read_only=True)
        self.assertTrue(failed)
        self.assertIn("--read-only", text)
        self.assertEqual(len(self.board()["tasks"]), len(kanvas.load_board(self.path)["tasks"]))

    def test_the_mcp_usage_tool_totals_the_runs(self):
        board = self.mcp_board()
        runs = os.path.join(self.dir, ".kanvas", "runs")
        os.makedirs(runs, exist_ok=True)
        event = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 7, "total_cost_usd": 0.25,
                 "usage": {"output_tokens": 100}, "modelUsage": {"claude-sonnet-5-5": {}}, "duration_ms": 12000, "result": "ok"}
        with open(os.path.join(runs, "dv-02.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        usage = json.loads(self.call("kanvas_usage")[0])
        self.assertEqual((usage["runs"], usage["total_usd"], usage["rows"][0]["turns"]), (1, 0.25, 7))

    def inbox_board(self):
        board = self.mcp_board()
        write_board(self.path, board)
        return board

    def po_reply(self, **data):
        """What the product owner answers: prose and the json block that ends it."""
        base = {"kind": "idea", "title": "Export players to Google Sheets weekly", "group": "DV", "desc": "A club wants a weekly export.",
                "duplicates": ["DV-01"], "reply": "Entendido: exportacion semanal. Repo front, agente implementer.", "questions": [],
                "ready": True, "draft": {"outcome": "Coordinators get the player list in a sheet every week",
                                        "needs": "No data of other clubs", "acceptance": "A sheet updates weekly with the active players",
                                        "non_goals": "Real-time sync", "repositories": ["front"], "agent": "implementer"}}
        base.update(data)
        return "Thinking about it.\n```json\n" + json.dumps(base) + "\n```"

    def run_po(self, task_id, reply):
        calls = []

        def fake(command, **kwargs):
            calls.append({"command": command, "prompt": kwargs["input"], "cwd": kwargs["cwd"]})
            event = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.03, "result": reply}
            kwargs["stdout"].write(json.dumps(event) + "\n")
            return mock.Mock(returncode=0, stopped=None)

        with mock.patch.object(kanvas, "agent_run", side_effect=fake), mock.patch.object(kanvas.shutil, "which", return_value="claude"):
            result = kanvas.inbox_po_turn(self.path, task_id)
        self.po_call = calls[0]
        return result

    def test_an_idea_sent_to_the_inbox_becomes_a_proposed_task_the_product_owner_shapes(self):
        self.inbox_board()
        task_id = kanvas.inbox_submit(self.path, "Los clubs piden exportar jugadores a Sheets cada semana", "telegram")
        task = self.get(task_id)
        self.assertEqual((task["state"], task["by"]), ("proposed", "owner via telegram"))
        self.assertEqual((task["inbox"]["source"], task["inbox"]["status"]), ("telegram", "shaping"))
        self.assertEqual((task["notes"][-1]["by"], task["notes"][-1]["text"]), ("owner", "Los clubs piden exportar jugadores a Sheets cada semana"))
        result = self.run_po(task_id, self.po_reply())
        self.assertEqual((result["ready"], result["kind"], result["duplicates"]), (True, "idea", ["DV-01"]))
        task = self.get(task_id)
        self.assertEqual(task["title"], "Export players to Google Sheets weekly")
        self.assertEqual((task["group"], task["state"]), ("DV", "proposed"))
        self.assertEqual((task["inbox"]["status"], task["inbox"]["ready"], task["inbox"]["turns"]), ("ready", True, 1))
        self.assertEqual(task["inbox"]["draft"]["repositories"], ["front"])
        self.assertEqual(task["notes"][-1]["by"], "product-owner")
        self.assertIn("exportacion semanal", task["notes"][-1]["text"])
        self.assertNotIn("busy", task.get("work") or {})
        command = " ".join(self.po_call["command"])
        self.assertIn("--agent product-owner", command)
        self.assertIn("--tools Read,Glob,Grep", command)
        for text in ("[owner] Los clubs piden exportar", "at most 3 questions", "DV-03"):
            self.assertIn(text, self.po_call["prompt"])
        self.assertIn(os.path.join(self.dir, "brain"), self.po_call["prompt"])

    def test_the_owner_answers_the_product_owner_in_the_same_thread(self):
        self.inbox_board()
        task_id = kanvas.inbox_submit(self.path, "Algo falla al subir fotos", "web")
        self.run_po(task_id, self.po_reply(kind="bug", ready=False, questions=["Que pantalla es?", "Pasa en iPhone y Android?"],
                                           reply="Parece un bug. Que pantalla es?"))
        self.assertEqual(self.get(task_id)["inbox"]["status"], "shaping")
        self.assertEqual(kanvas.inbox_submit(self.path, "Es la de noticias, solo en iPhone", "web", task_id), task_id)
        self.run_po(task_id, self.po_reply(kind="bug"))
        thread = self.po_call["prompt"]
        self.assertLess(thread.index("[owner] Algo falla"), thread.index("[product owner] Parece un bug"))
        self.assertLess(thread.index("[product owner] Parece un bug"), thread.index("[owner] Es la de noticias"))
        self.assertEqual(len([n for n in self.get(task_id)["notes"] if n["by"] == "owner"]), 2)
        self.assertEqual(self.get(task_id)["inbox"]["turns"], 2)
        with self.assertRaises(kanvas.KanvasError):
            kanvas.inbox_submit(self.path, "more", "web", "DV-02")

    def test_the_product_owners_answer_is_checked_before_it_touches_the_board(self):
        board = self.inbox_board()
        config = kanvas.automation_config(board, self.path)
        raw = json.loads(self.po_reply(group="NOPE", duplicates=["NOPE-9", "DV-01"], draft={
            "outcome": "o", "acceptance": "a", "repositories": ["front", "ghost", "brain"], "agent": "reviewer"}).split("```json\n")[1].split("\n```")[0])
        clean = kanvas.clean_inbox(raw, board, config)
        self.assertEqual((clean["group"], clean["duplicates"]), (None, ["DV-01"]))
        self.assertEqual((clean["draft"]["repositories"], clean["draft"]["agent"]), (["front"], "implementer"))
        self.assertTrue(clean["ready"])
        self.assertFalse(kanvas.clean_inbox({**raw, "draft": {"outcome": "o", "repositories": ["front"]}}, board, config)["ready"])
        self.assertFalse(kanvas.clean_inbox({**raw, "kind": "question"}, board, config)["ready"])
        self.assertIsNone(kanvas.clean_inbox({**raw, "kind": "joke"}, board, config))
        self.assertIsNone(kanvas.clean_inbox("nothing", board, config))

    def test_approving_an_idea_needs_a_complete_draft_and_makes_it_to_do(self):
        self.inbox_board()
        task_id = kanvas.inbox_submit(self.path, "Idea", "terminal")
        with self.assertRaises(kanvas.KanvasError) as caught:
            kanvas.inbox_approve(self.path, task_id)
        self.assertIn("still lacks", str(caught.exception))
        self.run_po(task_id, self.po_reply())
        kanvas.inbox_approve(self.path, task_id, "Telegram")
        task = self.get(task_id)
        self.assertEqual(task["state"], "todo")
        self.assertEqual((task["definition"]["approved"], task["definition"]["via"], task["definition"]["by"]), (True, "Telegram", "human"))
        self.assertEqual(task["definition"]["repositories"], ["front"])
        self.assertEqual(task["inbox"]["status"], "approved")
        self.assertEqual(kanvas.inbox_tasks(self.board()), [])
        with self.assertRaises(kanvas.KanvasError):
            kanvas.inbox_approve(self.path, task_id)

    def test_dropping_an_idea_removes_it_and_only_ideas(self):
        self.inbox_board()
        task_id = kanvas.inbox_submit(self.path, "Silly idea", "web")
        self.assertEqual(kanvas.inbox_drop(self.path, task_id), task_id)
        with self.assertRaises(kanvas.KanvasError):
            self.get(task_id)
        with self.assertRaises(kanvas.KanvasError):
            kanvas.inbox_drop(self.path, "DV-02")

    def test_a_turn_that_fails_leaves_a_note_and_the_idea_waiting(self):
        self.inbox_board()
        task_id = kanvas.inbox_submit(self.path, "Idea", "web")
        with mock.patch.object(kanvas, "agent_run", return_value=mock.Mock(returncode=1, stopped=None)), \
             mock.patch.object(kanvas.shutil, "which", return_value="claude"):
            with self.assertRaises(kanvas.KanvasError):
                kanvas.inbox_po_turn(self.path, task_id)
        task = self.get(task_id)
        self.assertIn("could not shape this", task["notes"][-1]["text"])
        self.assertEqual(task["state"], "proposed")
        self.assertNotIn("busy", task.get("work") or {})

    def test_the_inbox_command_lists_approves_and_drops(self):
        self.inbox_board()
        keep, silly = kanvas.inbox_submit(self.path, "Keep", "web"), kanvas.inbox_submit(self.path, "Silly", "web")
        self.run_po(keep, self.po_reply())
        _, out, _ = self.run_cli("inbox")
        self.assertIn(keep, out)
        self.assertIn(silly, out)
        self.assertIn("Approved", self.run_cli("inbox", "--approve", keep)[1])
        self.assertIn("Dropped", self.run_cli("inbox", "--drop", silly)[1])
        self.assertIn("The inbox is empty", self.run_cli("inbox")[1])

    class FakeTelegram:
        """The Bot API, with what the bot sends recorded."""
        def __init__(self, updates=()):
            self.updates, self.sent, self.calls = list(updates), [], []

        def __call__(self, method, params=None, timeout=40):
            self.calls.append((method, params))
            if method == "getUpdates":
                taken, self.updates = self.updates, []
                return taken
            if method == "sendMessage":
                self.sent.append(params)
            if method == "getFile":
                return {"file_path": "photos/x.jpg"}
            return {}

        def download(self, file_path):
            return b"\xff\xd8fake-jpeg"

    OWNER = 4242

    def bridge(self, updates=()):
        api = self.FakeTelegram(updates)
        return kanvas.TelegramBridge(self.path, api, [self.OWNER], say=lambda m: None), api

    def test_the_bot_tells_the_owner_once_when_a_run_stops_and_ignores_what_was_already_stopped(self):
        self.inbox_board()

        def stop(summary, updated):
            def op(board):
                task = kanvas.find_task(board, "DV-03")
                task["state"] = "doing"
                task["work"] = {"automation": {"status": "failed", "round": 1, "updated": updated, "summary": summary}}
            kanvas.mutate(self.path, op)

        stop("old failure", "2026-01-01T00:00:00Z")
        bridge, api = self.bridge()
        bridge.notify_stopped()
        self.assertEqual(api.sent, [])
        stop("Needs Cloudflare access", "2026-01-02T00:00:00Z")
        bridge.notify_stopped()
        bridge.notify_stopped()
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(api.sent[0]["chat_id"], self.OWNER)
        self.assertIn("DV-03", api.sent[0]["text"])
        self.assertIn("Cloudflare", api.sent[0]["text"])

    def tg_message(self, text, user=None, chat_type="private", **extra):
        user = user or self.OWNER
        return {"update_id": 1, "message": {"from": {"id": user}, "chat": {"id": user, "type": chat_type}, "text": text, **extra}}

    def handle_with_po(self, bridge, update, reply):
        def fake(command, **kwargs):
            event = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0.02, "result": reply}
            kwargs["stdout"].write(json.dumps(event) + "\n")
            self.po_prompt = kwargs["input"]
            return mock.Mock(returncode=0, stopped=None)

        with mock.patch.object(kanvas, "agent_run", side_effect=fake), mock.patch.object(kanvas.shutil, "which", return_value="claude"):
            bridge.handle(update)

    def keyboard(self, params):
        return [b["callback_data"] for row in params.get("reply_markup", {}).get("inline_keyboard", []) for b in row]

    def test_the_bot_hears_only_the_owner_in_a_private_chat(self):
        self.inbox_board()
        bridge, api = self.bridge()
        before = len(self.board()["tasks"])
        for update in (self.tg_message("idea", user=999), self.tg_message("idea", chat_type="group"),
                       {"update_id": 2, "callback_query": {"id": "1", "from": {"id": 999}, "data": "ap:DV-03",
                                                           "message": {"chat": {"id": 999, "type": "private"}}}}):
            bridge.handle(update)
        self.assertEqual((api.sent, len(self.board()["tasks"]), bridge.ignored), ([], before, 3))

    def test_an_idea_written_to_the_bot_comes_back_shaped_with_buttons_to_approve_or_drop(self):
        self.inbox_board()
        bridge, api = self.bridge()
        self.handle_with_po(bridge, self.tg_message("Los clubs piden exportar jugadores a Sheets cada semana"), self.po_reply())
        task_id = kanvas.inbox_tasks(self.board())[0]["id"]
        sent = api.sent[-1]
        self.assertEqual(sent["chat_id"], self.OWNER)
        self.assertIn(task_id, sent["text"])
        self.assertIn("exportacion semanal", sent["text"])
        self.assertEqual(self.keyboard(sent), [f"ap:{task_id}", f"dr:{task_id}"])
        self.assertIn(("sendChatAction", {"chat_id": self.OWNER, "action": "typing"}), api.calls)
        self.assertEqual(self.get(task_id)["inbox"]["source"], "telegram")

    def test_a_question_from_the_product_owner_is_answered_in_the_same_thread_until_it_is_ready(self):
        self.inbox_board()
        bridge, api = self.bridge()
        self.handle_with_po(bridge, self.tg_message("Falla algo al subir fotos"),
                            self.po_reply(kind="bug", ready=False, questions=["Que pantalla?"], reply="Que pantalla es?"))
        first = kanvas.inbox_tasks(self.board())[0]["id"]
        self.assertEqual(self.keyboard(api.sent[-1]), [f"dr:{first}"])
        self.handle_with_po(bridge, self.tg_message("Noticias, en iPhone"), self.po_reply(kind="bug"))
        self.assertEqual([t["id"] for t in kanvas.inbox_tasks(self.board())], [first])
        self.assertIn("[owner] Noticias, en iPhone", self.po_prompt)
        self.assertEqual(self.keyboard(api.sent[-1]), [f"ap:{first}", f"dr:{first}"])
        self.handle_with_po(bridge, self.tg_message("Otra cosa distinta"), self.po_reply(title="Otra", ready=False))
        self.assertEqual(len(kanvas.inbox_tasks(self.board())), 2)

    def test_new_starts_another_idea_even_when_one_is_half_done(self):
        self.inbox_board()
        bridge, _ = self.bridge()
        self.handle_with_po(bridge, self.tg_message("Idea uno"), self.po_reply(ready=False, questions=["q?"]))
        self.handle_with_po(bridge, self.tg_message("/new Idea dos"), self.po_reply(title="Dos", ready=False))
        self.assertEqual(len(kanvas.inbox_tasks(self.board())), 2)

    def test_the_buttons_approve_or_drop_the_idea(self):
        self.inbox_board()
        bridge, api = self.bridge()
        self.handle_with_po(bridge, self.tg_message("Idea"), self.po_reply())
        task_id = kanvas.inbox_tasks(self.board())[0]["id"]
        query = lambda data: {"update_id": 5, "callback_query": {"id": "q", "from": {"id": self.OWNER}, "data": data,
                                                                  "message": {"chat": {"id": self.OWNER, "type": "private"}}}}
        bridge.handle(query(f"ap:{task_id}"))
        self.assertEqual(self.get(task_id)["state"], "todo")
        self.assertEqual(self.get(task_id)["definition"]["via"], "Telegram")
        self.assertIn("aprobado", api.sent[-1]["text"])
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "q"}), api.calls)
        bridge.handle(query(f"ap:{task_id}"))
        self.assertIn("not an idea", api.sent[-1]["text"])
        silly = kanvas.inbox_submit(self.path, "Tonteria", "telegram")
        bridge.handle(query(f"dr:{silly}"))
        self.assertIn("descartado", api.sent[-1]["text"])
        self.assertEqual([t["id"] for t in kanvas.inbox_tasks(self.board())], [])

    def test_the_bot_commands_list_approve_drop_and_explain(self):
        self.inbox_board()
        bridge, api = self.bridge()
        bridge.handle(self.tg_message("/help"))
        self.assertIn("/new", api.sent[-1]["text"])
        bridge.handle(self.tg_message("/list"))
        self.assertEqual(api.sent[-1]["text"], "No hay ideas pendientes.")
        idea = kanvas.inbox_submit(self.path, "Una idea", "telegram")
        bridge.handle(self.tg_message("/list"))
        self.assertIn(idea, api.sent[-1]["text"])
        bridge.handle(self.tg_message(f"/approve {idea}"))
        self.assertIn("still lacks", api.sent[-1]["text"])
        bridge.handle(self.tg_message(f"/drop {idea.lower()}"))
        self.assertIn("descartado", api.sent[-1]["text"])
        bridge.handle(self.tg_message("/nope"))
        self.assertIn("No conozco", api.sent[-1]["text"])

    def test_a_photo_with_a_caption_is_kept_and_given_to_the_product_owner(self):
        self.inbox_board()
        bridge, api = self.bridge()
        update = self.tg_message("", photo=[{"file_id": "small"}, {"file_id": "big"}], caption="Un club nos manda esta captura")
        self.handle_with_po(bridge, update, self.po_reply(kind="bug"))
        task = self.get(kanvas.inbox_tasks(self.board())[0]["id"])
        note = task["notes"][0]["text"]
        self.assertIn("Un club nos manda esta captura", note)
        kept = re.search(r"\[attachment: (.*)\]", note).group(1)
        with open(kept, "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8fake-jpeg")
        self.assertIn(os.path.join(self.dir, ".kanvas", "inbox"), kept)
        self.assertIn(kept, self.po_prompt)

    def test_the_bot_polls_for_updates_and_moves_its_offset(self):
        self.inbox_board()
        bridge, api = self.bridge([{**self.tg_message("/help"), "update_id": 7}, {**self.tg_message("/list"), "update_id": 9}])
        bridge.run(once=True)
        self.assertEqual(bridge.offset, 10)
        self.assertEqual(len(api.sent), 2)

    def test_the_bot_token_is_never_shown_nor_given_to_an_agent(self):
        with mock.patch.dict(os.environ, {"KANVAS_TELEGRAM_TOKEN": "123:SECRET", "KEEP_THIS": "yes"}):
            env = kanvas.agent_environment()
        self.assertNotIn("KANVAS_TELEGRAM_TOKEN", env)
        self.assertEqual(env["KEEP_THIS"], "yes")
        with mock.patch.object(kanvas.urllib.request, "urlopen", side_effect=kanvas.urllib.error.URLError("failed for https://x/bot123:SECRET/getMe")):
            with self.assertRaises(kanvas.TelegramError) as caught:
                kanvas.telegram_api("123:SECRET")("getMe")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_the_bot_refuses_to_start_without_a_token_and_the_owner_id(self):
        self.inbox_board()
        clean = {"KANVAS_TELEGRAM_TOKEN": "", "KANVAS_TELEGRAM_USERS": "", "KANVAS_STATE_DIR": os.path.join(self.dir, "state")}
        self.assertIn("Connect Telegram first", self.fails("telegram", env=clean))
        self.assertIn("KANVAS_TELEGRAM_USERS", self.fails("telegram", env={**clean, "KANVAS_TELEGRAM_TOKEN": "1:x"}))
        self.assertIn("KANVAS_TELEGRAM_USERS", self.fails("telegram", env={**clean, "KANVAS_TELEGRAM_TOKEN": "1:x", "KANVAS_TELEGRAM_USERS": "me"}))

    def test_the_owner_pairs_by_sending_the_code_the_board_showed_and_only_that_account_is_heard_after(self):
        self.inbox_board()
        paired = []
        api = self.FakeTelegram()
        bridge = kanvas.TelegramBridge(self.path, api, [], say=lambda m: None, pairing={"code": "123456", "expires": time.time() + 60},
                                       on_pair=paired.append)
        for update in (self.tg_message("123456", user=7, chat_type="group"), self.tg_message("654321", user=7), self.tg_message("hola", user=7)):
            bridge.handle(update)
        self.assertEqual((api.sent, paired, bridge.users), ([], [], set()))
        bridge.handle(self.tg_message("123456", user=7))
        self.assertEqual((paired, bridge.users), ([7], {7}))
        self.assertIn("Conectado", api.sent[-1]["text"])
        self.assertIsNone(bridge.pairing)
        sent = len(api.sent)
        bridge.handle(self.tg_message("123456", user=8))
        self.assertEqual((len(api.sent), bridge.users), (sent, {7}))
        bridge.handle(self.tg_message("/help", user=7))
        self.assertIn("/new", api.sent[-1]["text"])

    def test_an_expired_pairing_code_pairs_nobody(self):
        bridge = kanvas.TelegramBridge(self.path, self.FakeTelegram(), [], say=lambda m: None,
                                       pairing={"code": "123456", "expires": time.time() - 1}, on_pair=lambda u: self.fail("paired"))
        bridge.handle(self.tg_message("123456", user=7))
        self.assertEqual(bridge.users, set())

    def test_the_saved_connection_is_read_and_the_environment_wins_when_it_is_complete(self):
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {"KANVAS_STATE_DIR": state, "KANVAS_TELEGRAM_TOKEN": "",
                                                                                   "KANVAS_TELEGRAM_USERS": ""}):
            self.assertEqual(kanvas.telegram_config(), {})
            kanvas.save_telegram_config({"token": "1:abc", "users": [5, "x"], "bot": "mybot", "pairing": None})
            self.assertEqual(kanvas.telegram_config()["users"], [5])
            with mock.patch.dict(os.environ, {"KANVAS_TELEGRAM_TOKEN": "9:env", "KANVAS_TELEGRAM_USERS": "11, 12"}):
                self.assertEqual((kanvas.telegram_config()["token"], kanvas.telegram_config()["users"]), ("9:env", [11, 12]))
            self.assertEqual(len(kanvas.new_pairing()["code"]), 6)

    def png_size(self, path):
        with open(path, "rb") as f:
            head = f.read(24)
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")

    def test_shots_keep_the_page_at_phone_ipad_and_desktop_size_as_evidence(self):
        browser = kanvas.find_browser()
        if not browser:
            self.skipTest("No Chrome, Chromium or Edge to take the screenshots")
        page = os.path.join(self.dir, "page.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write("<!doctype html><html><body style='margin:0'><h1 style='background:#0b5fff;color:#fff'>Hi</h1></body></html>")
        _, out, _ = self.run_cli("shots", page, "--task", "DV-02", env={"KANVAS_BROWSER": browser})
        folder = os.path.join(self.dir, ".kanvas", "evidence", "DV-02", "final")
        self.assertEqual(len(out.split()), 3)
        self.assertEqual([self.png_size(os.path.join(folder, f"design-{n}.png")) for n in ("mobile", "ipad", "web")],
                         [(390, 844), (820, 1180), (1280, 800)])
        self.assertEqual([f["path"] for f in kanvas.evidence_files(os.path.join(self.dir, ".kanvas", "evidence", "DV-02"))],
                         ["final/design-ipad.png", "final/design-mobile.png", "final/design-web.png"])

    def test_shots_refuse_what_they_cannot_screenshot(self):
        self.assertIn("is not a file or a URL", self.fails("shots", os.path.join(self.dir, "nope.html"), "--task", "DV-02",
                                                           env={"KANVAS_BROWSER": sys.executable}))
        self.assertIn("No browser", self.fails("shots", "x.html", "--task", "DV-02", env={"KANVAS_BROWSER": os.path.join(self.dir, "none")}))
        self.assertIn("not found", self.fails("shots", "x.html", "--task", "NOPE-1"))

    def test_the_implementer_is_told_to_look_at_what_it_built_when_a_design_was_approved(self):
        def prompt_for(**design):
            board = self.designing_board(**design)
            config = kanvas.automation_config(board, self.path)
            repo = config["repositories"]["front"]["path"]
            return kanvas.automation_prompt(board, kanvas.find_task(board, "DV-02"), config, self.fake_worktrees(repo))

        approved = prompt_for(status="approved", text="Chips on top")
        self.assertIn("shots <the html file", approved)
        self.assertIn("--task DV-02", approved)
        self.assertIn(self.path, approved)
        self.assertNotIn("shots <the html file", prompt_for(status="pending", text="Chips on top"))

    def test_a_run_that_writes_nothing_is_stopped_so_it_spends_no_more(self):
        proc, took, _ = self.run_process("import time; time.sleep(60)", limits={"timeout": 60, "idle": 2})
        self.assertIn("wrote nothing", proc.stopped)
        self.assertLess(took, 20)
        self.assertIn("Stopped by Factory because it wrote nothing", kanvas.stop_text(proc))

    def test_a_run_that_lasts_too_long_is_stopped_even_if_it_keeps_writing(self):
        code = "import time\nwhile True:\n    print('x'); time.sleep(0.2)"
        proc, took, _ = self.run_process(code, limits={"timeout": 2, "idle": 60})
        self.assertIn("ran for more than", proc.stopped)
        self.assertLess(took, 20)

    def test_a_run_is_stopped_when_the_human_asks(self):
        asked = []
        proc, took, _ = self.run_process("import time; time.sleep(60)", limits={"timeout": 60, "idle": 60},
                                         stop=lambda: asked.append(1) or len(asked) > 1)
        self.assertEqual(proc.stopped, "stopped")
        self.assertEqual(kanvas.stop_text(proc), "Stopped by you.")
        self.assertLess(took, 20)

    def test_the_limits_come_from_the_role_then_the_board_then_the_defaults(self):
        config = {"timeout_minutes": 20, "idle_minutes": 5}
        self.assertEqual(kanvas.agent_limits(config, {"timeout_minutes": 7}), {"timeout": 420, "idle": 300})
        self.assertEqual(kanvas.agent_limits(config, None), {"timeout": 1200, "idle": 300})
        self.assertEqual(kanvas.agent_limits({}, {}), {"timeout": 40 * 60, "idle": 10 * 60})
        _, config = self.roles_of(timeout_minutes=12, idle_minutes=3)
        self.assertEqual((config["timeout_minutes"], config["idle_minutes"]), (12, 3))
        for bad in ({"timeout_minutes": 0}, {"idle_minutes": "soon"}):
            with self.assertRaises(kanvas.KanvasError):
                self.roles_of(**bad)

    def test_stopping_a_task_needs_an_agent_working_on_it(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        write_board(self.path, self.automated_board(repo))
        self.assertIn("No agent is working on DV-02", self.fails("stop", "DV-02"))
        board = self.board()
        kanvas.find_task(board, "DV-02")["work"]["automation"] = {"status": "running", "updated": kanvas.now_iso()}
        write_board(self.path, board)
        _, out, _ = self.run_cli("stop", "DV-02")
        self.assertIn("to stop", out)
        self.assertTrue(kanvas.stop_requested(self.path, "DV-02"))
        kanvas.clear_stop(self.path, "DV-02")
        self.assertFalse(kanvas.stop_requested(self.path, "DV-02"))

    def test_a_stopped_implementer_run_fails_with_the_reason_and_can_be_retried(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        write_board(self.path, self.automated_board(repo))
        stopped = mock.Mock(returncode=1, stopped="stopped")

        def fake(command, **kwargs):
            kwargs["stdout"].write("")
            return stopped

        with mock.patch.object(kanvas, "prepare_automation_worktrees", return_value=self.fake_worktrees(repo)),              mock.patch.object(kanvas, "publish_pull_requests") as publish, mock.patch.object(kanvas, "agent_run", side_effect=fake):
            kanvas.run_automation(self.path, "DV-02")
        run = self.get("DV-02")["work"]["automation"]
        self.assertEqual(run["status"], "failed")
        self.assertIn("Stopped by you.", run["summary"])
        self.assertIn("retry DV-02", run["summary"])
        publish.assert_not_called()
        self.assertEqual(self.get("DV-02")["state"], "doing")

    def test_a_stopped_review_leaves_a_note_and_the_task_for_the_human(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        stopped = mock.Mock(returncode=1, stopped="it wrote nothing for more than 10 minutes")
        write_board(self.path, self.review_board(repo))

        def fake(command, **kwargs):
            return stopped

        with mock.patch.object(kanvas, "review_diff", return_value=("", "", "")), mock.patch.object(kanvas, "agent_run", side_effect=fake):
            outcome, text = kanvas.run_review(self.path, "DV-02")
        self.assertEqual(outcome, "failed")
        self.assertIn("Stopped by Factory because it wrote nothing", self.get("DV-02")["notes"][-1]["text"])
        self.assertEqual(self.get("DV-02")["state"], "review")

    def test_a_stop_asked_to_a_run_that_no_longer_exists_is_settled_by_the_dispatcher(self):
        board = self.designing_board()
        board["automation"]["design"] = False
        task = kanvas.find_task(board, "DV-02")
        task["work"]["automation"] = {"status": "running", "updated": kanvas.now_iso()}
        task["work"]["stop"] = {"at": kanvas.now_iso()}
        self.assertEqual(self.scan_designing(board), [])
        work = self.get("DV-02")["work"]
        self.assertNotIn("stop", work)
        self.assertEqual(work["automation"]["status"], "failed")
        self.assertIn("Stopped by you", work["automation"]["summary"])

    def test_invalid_agent_roles_and_lean_flag_are_refused(self):
        for bad in ([], {"x": "opus"}, {"x": {"model": 3}}, {"x": {"effort": "huge"}}, {"x": {"max_turns": 0}},
                    {"x": {"allowed_tools": 5}}):
            with self.subTest(agents=bad), self.assertRaises(kanvas.KanvasError):
                self.roles_of(agents=bad)
        with self.assertRaises(kanvas.KanvasError):
            self.roles_of(lean="yes")

    def test_a_task_runs_as_the_role_its_definition_names_and_as_the_implementer_otherwise(self):
        task = {"definition": {"approved": True}}
        self.assertEqual(kanvas.task_agent_name(task), "implementer")
        self.assertEqual(kanvas.task_agent_name({"definition": {"agent": "complex"}}), "complex")
        self.assertEqual(kanvas.task_agent_name({"definition": {"agent": "complex"}, "work": {"agent": "docs"}}), "docs")
        self.assertEqual(kanvas.task_agent_name({}), "implementer")

    def test_the_agent_starts_lean_by_default(self):
        board, config = self.roles_of()
        worktrees = self.fake_worktrees(os.path.join(self.dir, "front"))
        command = kanvas.claude_command(config, worktrees, config["agents"]["implementer"])
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--setting-sources") + 1], "project,local")
        self.assertEqual(command[command.index("--tools") + 1], "Bash,Edit,Write,Read,Glob,Grep")
        self.assertEqual(command[command.index("--model") + 1], "sonnet")
        self.assertEqual(command[command.index("--effort") + 1], "medium")
        _, config = self.roles_of(lean=False)
        command = kanvas.claude_command(config, worktrees, config["agents"]["implementer"])
        for flag in ("--strict-mcp-config", "--setting-sources", "--tools"):
            self.assertNotIn(flag, command)

    def test_a_role_sets_the_model_effort_limits_and_tools_over_the_automation_ones(self):
        board, config = self.roles_of(model="haiku", max_turns=40, max_budget_usd=2)
        worktrees = self.fake_worktrees(os.path.join(self.dir, "front"))
        complex_run = kanvas.claude_command(config, worktrees, config["agents"]["complex"])
        self.assertEqual((complex_run[complex_run.index("--model") + 1], complex_run[complex_run.index("--effort") + 1]), ("opus", "high"))
        self.assertEqual(complex_run[complex_run.index("--max-turns") + 1], "40")
        review = kanvas.claude_command(config, worktrees, config["agents"]["reviewer"])
        self.assertEqual(review[review.index("--max-turns") + 1], "12")
        self.assertEqual(review[review.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(review[review.index("--agent") + 1], "reviewer")
        allowed = review[review.index("--allowedTools") + 1:review.index("--disallowedTools")]
        self.assertEqual(allowed, ["Read", "Glob", "Grep"])
        strong = kanvas.claude_command(config, worktrees, config["agents"]["reviewer-strong"])
        self.assertEqual((strong[strong.index("--agent") + 1], strong[strong.index("--model") + 1]), ("reviewer", "sonnet"))
        self.assertNotIn("--agent", kanvas.claude_command(config, worktrees, config["agents"]["implementer"]))
        plain = kanvas.claude_command(config, worktrees)
        self.assertEqual(plain[plain.index("--model") + 1], "haiku")
        self.assertNotIn("--effort", plain)
        _, no_model = self.roles_of(model="haiku", agents={"docs": {"effort": "low"}})
        docs = kanvas.claude_command(no_model, worktrees, no_model["agents"]["docs"])
        self.assertEqual(docs[docs.index("--model") + 1], "haiku")

    def agent_pull(self):
        return {"id": "front", "name": "Frontend", "branch": "feature/DV-02", "base": "dev", "commits": 1,
                "url": "https://github.com/acme/front/pull/7"}

    def success_events(self):
        return [{"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.05, "result": "Done"}]

    def test_a_run_uses_the_model_of_the_role_its_task_names_and_records_it(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        for agent, model in ((None, "sonnet"), ("complex", "opus")):
            with self.subTest(agent=agent):
                run, _ = self.run_claude(repo, self.success_events(), pulls=[self.agent_pull()], agent=agent)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--model") + 1], model)
                self.assertEqual(self.get("DV-02")["work"]["automation"]["agent"], agent or "implementer")

    def test_a_task_that_names_an_unknown_role_fails_before_anything_runs(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        run, _ = self.run_claude(repo, self.success_events(), pulls=[self.agent_pull()], agent="ghost")
        self.assertEqual(run.call_count, 0)
        automation = self.get("DV-02")["work"]["automation"]
        self.assertEqual(automation["status"], "failed")
        self.assertIn("ghost", automation["summary"])
        self.assertIn("implementer", automation["summary"])

    # --- the reviewer: Review -> verdict -> approval, another round, or the human ---

    def review_board(self, repo, round_no=1, notes=(), **run):
        """A task in Review whose last run left worktrees and a pull request, with its agent review pending."""
        board = self.automated_board(repo)
        board["automation"]["repositories"][0]["base"] = "origin/dev"
        task = kanvas.find_task(board, "DV-02")
        task["state"] = "review"
        task["definition"].update(acceptance="A club admin can export players", outcome="Export")
        task["work"]["automation"] = {"status": "completed", "updated": "2026-11-02T10:00:00Z", "round": round_no,
                                      "summary": "Added the export. E2E: not run", "worktrees": [
                                          {"id": "front", "path": repo, "worktree": repo, "branch": "feature/DV-02", "commit": "a" * 40}],
                                      "pull_requests": [{"id": "front", "url": "https://github.com/acme/front/pull/7"}], **run}
        task["review"] = {"agent": {"status": "pending"}, "manual": {"status": "pending"}}
        task["notes"] = list(notes)
        return board

    def run_reviewers(self, board, verdicts, task_id="DV-02", force=False):
        """Runs the reviewer over the board with git, claude and gh faked. Each verdict is a dict, a raw reply string or
        an exception; returns the outcome, the commands it ran and the comments it left."""
        write_board(self.path, board)
        self.review_calls, self.comments = [], []
        queue = list(verdicts)

        def fake_run(command, **kwargs):
            reply = queue.pop(0)
            self.review_calls.append({"command": command, "prompt": kwargs["input"], "cwd": kwargs["cwd"], "env": kwargs["env"],
                                      "log": kwargs["stdout"].name})
            if isinstance(reply, Exception):
                raise reply
            text = reply if isinstance(reply, str) else "Looked at it.\n```json\n" + json.dumps(reply) + "\n```"
            event = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.02,
                     "result": text, "modelUsage": {"claude-haiku-5-5": {}}}
            kwargs["stdout"].write(json.dumps(event) + "\n")
            return mock.Mock(returncode=0)

        with mock.patch.object(kanvas, "review_diff", return_value=("abc feat: export", " a.ts | 2 +-", "+export()")), \
             mock.patch.object(kanvas, "agent_run", side_effect=fake_run), \
             mock.patch.object(kanvas, "_gh", side_effect=lambda path, *a: self.comments.append(a) or ""):
            return kanvas.run_review(self.path, task_id, force)

    def test_the_reviewer_approves_and_comments_on_the_pull_request(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        outcome = self.run_reviewers(self.review_board(repo), [{"verdict": "approve", "summary": "Meets the criteria", "findings": []}])
        self.assertEqual(outcome, ("approved", "Meets the criteria"))
        task = self.get("DV-02")
        self.assertEqual(task["state"], "review")
        self.assertEqual((task["review"]["agent"]["status"], task["review"]["agent"]["by"]), ("approved", "reviewer"))
        self.assertEqual(task["review"]["manual"], {"status": "pending"})
        self.assertEqual(task["work"]["automation"]["reviewed_round"], 1)
        self.assertEqual(task["notes"][-1]["text"], "Agent review approved: Meets the criteria")
        call = self.review_calls[0]
        command = call["command"]
        self.assertEqual((command[command.index("--agent") + 1], command[command.index("--model") + 1]), ("reviewer", "haiku"))
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(call["cwd"], self.dir)
        self.assertTrue(call["log"].endswith("dv-02-review.jsonl"))
        for text in ("A club admin can export players", "+export()", "Added the export. E2E: not run", "feature/DV-02 against origin/dev"):
            self.assertIn(text, call["prompt"])
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.comments[0][:3], ("pr", "comment", "https://github.com/acme/front/pull/7"))
        self.assertIn("**Approved.** Meets the criteria", self.comments[0][4])
        self.assertNotIn("approved before the work started", call["prompt"])

    def test_the_reviewer_is_told_what_design_the_human_approved(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.review_board(repo)
        kanvas.find_task(board, "DV-02")["work"]["design"] = {"status": "approved", "text": "Plain English, no new i18n keys"}
        self.run_reviewers(board, [{"verdict": "approve", "summary": "ok", "findings": []}])
        prompt = self.review_calls[0]["prompt"]
        self.assertIn("approved before the work started", prompt)
        self.assertIn("Plain English, no new i18n keys", prompt)
        self.assertIn("never ask for something it rules out", prompt)
        self.assertIn("ready for your review", self.comments[0][4])

    def test_the_reviewer_sends_the_work_back_with_its_findings(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        verdict = {"verdict": "changes", "summary": "One criterion is missing",
                   "findings": [{"where": "src/export.ts:12", "text": "No club check"}, "A string finding", {"text": ""}]}
        outcome = self.run_reviewers(self.review_board(repo), [verdict])
        self.assertEqual(outcome[0], "changes")
        task = self.get("DV-02")
        self.assertEqual(task["state"], "doing")
        self.assertEqual(task["review"]["agent"]["status"], "changes_requested")
        self.assertEqual(task["review"]["manual"], {"status": "pending"})
        note = task["notes"][-1]
        self.assertEqual(note["by"], "reviewer")
        self.assertEqual(note["text"], "Agent review changes requested: One criterion is missing\n- `src/export.ts:12`: No club check\n- A string finding")
        self.assertIn("will fix this in the next round", self.comments[0][4])
        # The dispatcher then runs the next round, and the reviewer's note is its feedback.
        launched = self.scan(kanvas.load_board(self.path))
        self.assertEqual(launched, ["DV-02"])
        run = self.get("DV-02")["work"]["automation"]
        self.assertEqual((run["status"], run["round"], run["cycle_start"]), ("queued", 2, 1))

    def test_the_reviewer_looks_again_at_what_it_fixed_and_leaves_the_task_to_the_human_after_two_rounds(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        asked = [{"at": "2026-11-02T0%d:00:00Z" % i, "by": "reviewer", "text": f"{kanvas.CHANGES_NOTE}: round {i} findings"} for i in (1, 2)]
        runner = {"at": "2026-11-02T09:30:00Z", "by": "kanvas-runner", "text": "Automation completed: ok"}
        board = self.review_board(repo, round_no=3, notes=[asked[0], runner, asked[1], runner])
        outcome = self.run_reviewers(board, [{"verdict": "changes", "summary": "Still not fixed", "findings": ["x"]}])
        self.assertEqual(outcome[0], "left")
        # After two rounds of the haiku reviewer, the third review is made by the strong reviewer (sonnet).
        command = self.review_calls[0]["command"]
        self.assertEqual(command[command.index("--model") + 1], "sonnet")
        self.assertTrue(self.review_calls[0]["log"].endswith("dv-02-review-strong.jsonl"))
        self.assertEqual(len(self.review_calls), 1)
        self.assertIn("round 2 findings", self.review_calls[0]["prompt"])
        task = self.get("DV-02")
        self.assertEqual((task["state"], task["review"]["agent"]["status"]), ("review", "pending"))
        self.assertIn("Automatic review left this to you (it has asked for changes 2 times in a row)", task["notes"][-1]["text"])
        self.assertIn("**Needs your attention.**", self.comments[0][4])
        self.assertEqual(task["work"]["automation"]["reviewed_round"], 3)

    def test_a_human_note_starts_the_count_of_automatic_rounds_again(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        asked = {"at": "2026-11-02T01:00:00Z", "by": "reviewer", "text": f"{kanvas.CHANGES_NOTE}: old"}
        human = {"at": "2026-11-02T02:00:00Z", "by": "human", "text": "Sent back: please also handle X"}
        board = self.review_board(repo, notes=[asked, asked, human])
        self.assertEqual(self.run_reviewers(board, [{"verdict": "changes", "summary": "s", "findings": []}])[0], "changes")

    def test_a_stronger_reviewer_looks_again_when_the_first_cannot_decide(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        outcome = self.run_reviewers(self.review_board(repo), [{"verdict": "escalate", "summary": "Touches permissions"},
                                                              {"verdict": "approve", "summary": "Checked, it is fine"}])
        self.assertEqual(outcome[0], "approved")
        first, second = self.review_calls
        self.assertEqual(first["command"][first["command"].index("--model") + 1], "haiku")
        self.assertEqual(second["command"][second["command"].index("--model") + 1], "sonnet")
        self.assertTrue(second["log"].endswith("dv-02-review-strong.jsonl"))
        self.assertIn('Use "escalate" when', first["prompt"])
        self.assertIn("do not escalate", second["prompt"])
        self.assertNotIn('Use "escalate" when', second["prompt"])
        self.assertEqual(self.get("DV-02")["review"]["agent"]["by"], "reviewer-strong")
        self.assertIn("reviewer-strong", self.comments[0][4])

    def test_when_the_strong_reviewer_cannot_decide_either_the_human_decides(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        outcome = self.run_reviewers(self.review_board(repo), [{"verdict": "escalate", "summary": "?"},
                                                              {"verdict": "escalate", "summary": "Still unsure about isolation"}])
        self.assertEqual(outcome[0], "left")
        task = self.get("DV-02")
        self.assertEqual((task["state"], task["review"]["agent"]["status"]), ("review", "pending"))
        self.assertIn("it could not decide", task["notes"][-1]["text"])

    def test_a_review_that_cannot_run_leaves_a_note_and_the_task_for_the_human(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        for verdicts, expected in (([OSError("claude is gone")], "claude is gone"), (["No JSON here."], "did not return a verdict"),
                                   ([{"verdict": "maybe"}], "did not return a verdict")):
            with self.subTest(expected=expected):
                outcome = self.run_reviewers(self.review_board(repo), verdicts)
                self.assertEqual(outcome[0], "failed")
                self.assertIn(expected, outcome[1])
                task = self.get("DV-02")
                self.assertEqual((task["state"], task["review"]["agent"]["status"]), ("review", "pending"))
                self.assertEqual(task["work"]["automation"]["reviewed_round"], 1)
                self.assertIn("Automatic review failed", task["notes"][-1]["text"])
                self.assertEqual(self.comments, [])

    def test_a_verdict_for_a_task_that_moved_on_is_dropped(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.review_board(repo)
        original = kanvas.record_review

        def human_sends_it_back(*args, **kwargs):
            def op(b):
                kanvas.find_task(b, "DV-02")["state"] = "doing"
            kanvas.mutate(self.path, op)
            return original(*args, **kwargs)

        with mock.patch.object(kanvas, "record_review", side_effect=human_sends_it_back):
            outcome = self.run_reviewers(board, [{"verdict": "approve", "summary": "ok"}])
        self.assertEqual(outcome[0], "stale")
        task = self.get("DV-02")
        self.assertEqual((task["state"], task["review"]["agent"]["status"]), ("doing", "pending"))
        self.assertEqual(self.comments, [])

    def test_the_review_command_reviews_now_and_a_missing_worktree_is_reported(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.review_board(repo)
        kanvas.find_task(board, "DV-02")["work"]["automation"]["worktrees"][0]["worktree"] = os.path.join(self.dir, "gone")
        write_board(self.path, board)
        self.assertIn("nothing to review", self.fails("review", "DV-02"))
        self.assertIn("only tasks in review", self.fails("review", "DV-01"))

    def test_the_reviewer_can_be_turned_off_and_its_rounds_set(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        for value, expected in ((None, {"enabled": True, "rounds": 2}), (False, {"enabled": False, "rounds": 2}),
                                ({"rounds": 0}, {"enabled": True, "rounds": 0}), ({"enabled": False, "rounds": 4}, {"enabled": False, "rounds": 4})):
            with self.subTest(value=value):
                _, config = self.roles_of(**({} if value is None else {"review": value}))
                self.assertEqual(config["review"], expected)
        for bad in ("yes", 3, {"rounds": -1}, {"rounds": True}, {"rounds": "2"}, {"enabled": "no"}):
            with self.subTest(bad=bad), self.assertRaises(kanvas.KanvasError):
                self.roles_of(review=bad)

    def scan_launching(self, board_data):
        """One dispatcher pass with the implementer and the reviewer faked; returns what it launched."""
        write_board(self.path, board_data)
        launched, done = [], threading.Event()
        dispatcher = object.__new__(kanvas.AutomationDispatcher)
        dispatcher.lock, dispatcher.active = threading.Lock(), set()

        def note(kind):
            def fake(path, task_id, *a, **k):
                launched.append((kind, task_id))
                done.set()
            return fake

        with mock.patch.object(kanvas, "run_automation", side_effect=note("run")), \
             mock.patch.object(kanvas, "run_review", side_effect=note("review")):
            dispatcher.scan(self.path)
            done.wait(2)
        return launched

    def test_the_dispatcher_reviews_a_task_that_reached_review_once_per_round(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        self.assertEqual(self.scan_launching(self.review_board(repo)), [("review", "DV-02")])
        self.assertEqual(self.scan_launching(self.review_board(repo, reviewed_round=1)), [])
        self.assertEqual(self.scan_launching(self.review_board(repo, round_no=2, reviewed_round=1)), [("review", "DV-02")])
        approved = self.review_board(repo)
        kanvas.find_task(approved, "DV-02")["review"]["agent"] = {"status": "approved"}
        self.assertEqual(self.scan_launching(approved), [])
        off = self.review_board(repo)
        off["automation"]["review"] = False
        self.assertEqual(self.scan_launching(off), [])
        no_worktrees = self.review_board(repo)
        del kanvas.find_task(no_worktrees, "DV-02")["work"]["automation"]["worktrees"]
        self.assertEqual(self.scan_launching(no_worktrees), [])
        removed = self.review_board(repo)
        kanvas.find_task(removed, "DV-02")["work"]["automation"]["worktrees"][0]["worktree"] = os.path.join(self.dir, "gone")
        self.assertEqual(self.scan_launching(removed), [])
        self.assertEqual(self.get("DV-02")["notes"], [])

    def test_the_rounds_the_reviewer_asks_for_count_towards_the_limit_but_a_humans_start_a_new_cycle(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        agent_note = {"at": "2026-11-02T11:05:00Z", "by": "reviewer", "text": f"{kanvas.CHANGES_NOTE}: add a test"}
        human_note = {"at": "2026-11-02T11:06:00Z", "by": "human", "text": "Sent back: also the empty city"}
        stopped = self.sent_back_board(repo, round_no=3)
        kanvas.find_task(stopped, "DV-02")["notes"] = [agent_note]
        self.assertEqual(self.scan_launching(stopped), [])
        self.assertIn("Stopped after 3 rounds", self.get("DV-02")["work"]["automation"]["summary"])
        resumed = self.sent_back_board(repo, round_no=3)
        kanvas.find_task(resumed, "DV-02")["notes"] = [agent_note, human_note]
        self.assertEqual(self.scan_launching(resumed), [("run", "DV-02")])
        run = self.get("DV-02")["work"]["automation"]
        self.assertEqual((run["round"], run["cycle_start"]), (4, 4))
        later = self.sent_back_board(repo, round_no=5, cycle_start=4)
        kanvas.find_task(later, "DV-02")["notes"] = [human_note, agent_note]
        self.assertEqual(self.scan_launching(later), [("run", "DV-02")])
        self.assertEqual(self.get("DV-02")["work"]["automation"]["cycle_start"], 4)

    def test_usage_counts_the_reviews_apart_from_the_implementation(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        kanvas.find_task(board, "DV-02")["work"]["automation"] = {"status": "completed", "agent": "implementer"}
        write_board(self.path, board)
        self.write_run_log("dv-02.jsonl")
        self.write_run_log("dv-02-review.jsonl", total_cost_usd=0.01, modelUsage={"claude-haiku-5-5": {}})
        self.write_run_log("dv-02-review-strong.jsonl", total_cost_usd=0.05, modelUsage={"claude-sonnet-5-5": {}})
        rows = {(r["task"], r["agent"]) for r in kanvas.usage_runs(kanvas.load_board(self.path), kanvas.automation_config(self.board(), self.path))}
        self.assertEqual(rows, {("DV-02", "implementer"), ("DV-02", "reviewer"), ("DV-02", "reviewer-strong")})

    def write_run_log(self, name, **result):
        runs = os.path.join(self.dir, ".kanvas", "runs")
        os.makedirs(runs, exist_ok=True)
        event = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 12, "total_cost_usd": 0.25,
                 "duration_ms": 90500, "usage": {"output_tokens": 3000, "cache_read_input_tokens": 400000,
                                                 "cache_creation_input_tokens": 30000},
                 "modelUsage": {"claude-sonnet-5-5": {"costUSD": 0.25}}}
        with open(os.path.join(runs, name), "w", encoding="utf-8") as f:
            f.write("not json\n" + json.dumps({"type": "system"}) + "\n" + json.dumps({**event, **result}) + "\n")

    def test_usage_reports_turns_cost_and_tokens_per_run_and_model(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        kanvas.find_task(board, "DV-02")["work"]["automation"] = {"status": "completed", "agent": "complex"}
        write_board(self.path, board)
        self.write_run_log("dv-02.jsonl")
        self.write_run_log("other-run.jsonl", total_cost_usd=0.5, modelUsage={"claude-opus-5-5": {}})
        with open(os.path.join(self.dir, ".kanvas", "runs", "empty.jsonl"), "w") as f:
            f.write("no result here\n")
        rows = {r["task"]: r for r in kanvas.usage_runs(kanvas.load_board(self.path), kanvas.automation_config(self.board(), self.path))}
        self.assertEqual(set(rows), {"DV-02", "other-run"})
        self.assertEqual((rows["DV-02"]["agent"], rows["DV-02"]["model"], rows["DV-02"]["turns"], rows["DV-02"]["seconds"]),
                         ("complex", "claude-sonnet-5-5", 12, 90))
        self.assertEqual((rows["DV-02"]["output"], rows["DV-02"]["cache_read"], rows["DV-02"]["cache_write"]), (3000, 400000, 30000))
        _, out, _ = self.run_cli("usage")
        self.assertIn("DV-02", out)
        self.assertIn("2 runs, $0.75 in total", out)
        self.assertIn("claude-opus-5-5: 1 runs, $0.50", out)
        _, out, _ = self.run_cli("usage", "--json")
        self.assertEqual({r["task"] for r in json.loads(out)}, {"DV-02", "other-run"})

    def test_the_page_offers_the_same_built_in_roles_as_the_server(self):
        with open(os.path.join(ROOT, "kanvas.html"), encoding="utf-8") as f:
            html = f.read()
        declared = re.search(r"const DEFAULT_AGENT_ROLES = \{(.*?)\};", html).group(1)
        in_page = dict(re.findall(r"\"?([\w-]+)\"?: \{ model: \"(\w+)\" \}", declared))
        self.assertEqual(in_page, {name: role["model"] for name, role in kanvas.DEFAULT_AGENTS.items()})

    def accepted_board(self, with_brain=True, **run):
        """A board whose DV-02 was accepted (done) after an automated run on `front`."""
        front, brain = os.path.join(self.dir, "front"), os.path.join(self.dir, "brain")
        os.makedirs(front, exist_ok=True)
        os.makedirs(brain, exist_ok=True)
        board = self.automated_board(front)
        if with_brain:
            board["automation"]["repositories"].append({"id": "brain", "name": "Brain", "path": brain, "base": "HEAD"})
        task = kanvas.find_task(board, "DV-02")
        task["state"] = "done"
        task["work"]["automation"] = {"status": "completed", "summary": "Added the city filter", **run}
        return board

    def test_accepting_an_automated_task_creates_a_documentation_task_for_the_brain(self):
        board = self.accepted_board()
        config = kanvas.automation_config(board, self.path)
        os.makedirs(config["runs"], exist_ok=True)
        with open(kanvas.changes_path(config, "DV-02"), "w", encoding="utf-8") as f:
            f.write("## front\n\n+ city filter")
        write_board(self.path, board)
        created = kanvas.curate_task(self.path, "DV-02")
        doc = self.get(created)
        self.assertEqual((doc["state"], doc["title"]), ("doing", f"Docs: {self.get('DV-02')['title']}"))
        self.assertEqual(kanvas.task_repository_ids(doc), ["brain"])
        self.assertEqual(kanvas.task_agent_name(doc), "curator")
        self.assertTrue(doc["definition"]["approved"])
        self.assertIn("Added the city filter", doc["work"]["plan"])
        self.assertIn("+ city filter", doc["work"]["plan"])
        self.assertEqual(self.get("DV-02")["curated"], created)
        self.assertIsNone(kanvas.curate_task(self.path, "DV-02"))
        role = kanvas.automation_config(kanvas.load_board(self.path), self.path)["agents"]["curator"]
        self.assertEqual((role["agent"], role["model"]), ("brain-curator", "haiku"))
        self.assertNotIn("Bash", role["allowed_tools"])

    def test_a_finished_run_keeps_its_changes_for_the_documentation_task(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        git = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)
        subprocess.run(["git", "init", repo], check=True, capture_output=True)
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        for text in ("one\n", "one\ntwo\n"):
            with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as f:
                f.write(text)
            git("add", "a.txt")
            git("commit", "-m", text.strip())
        board = self.automated_board(repo)
        board["automation"]["repositories"][0]["base"] = "HEAD~1"
        config = kanvas.automation_config(board, self.path)
        os.makedirs(config["runs"], exist_ok=True)
        kanvas.save_changes(config, "DV-02", [{"id": "front", "worktree": repo}])
        with open(kanvas.changes_path(config, "DV-02"), encoding="utf-8") as f:
            saved = f.read()
        self.assertIn("## front", saved)
        self.assertIn("+two", saved)

    def designing_board(self, **design):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo, exist_ok=True)
        board = self.automated_board(repo)
        board["automation"]["design"] = True
        if design:
            kanvas.find_task(board, "DV-02")["work"]["design"] = design
        return board

    def scan_designing(self, board):
        """One dispatcher pass with the designer and the implementer faked; returns what it launched."""
        write_board(self.path, board)
        launched, done = [], threading.Event()
        dispatcher = object.__new__(kanvas.AutomationDispatcher)
        dispatcher.lock, dispatcher.active = threading.Lock(), set()

        def note(kind):
            def fake(path, task_id, *a, **k):
                launched.append((kind, task_id))
                done.set()
            return fake

        with mock.patch.object(kanvas, "run_automation", side_effect=note("run")), \
             mock.patch.object(kanvas, "run_design", side_effect=note("design")):
            dispatcher.scan(self.path)
            done.wait(2)
        return launched

    def test_a_front_task_gets_a_design_before_the_implementer_runs(self):
        self.assertEqual(self.scan_designing(self.designing_board()), [("design", "DV-02")])
        self.assertEqual(self.scan_designing(self.designing_board(status="changes_requested", text="v1", feedback="x")),
                         [("design", "DV-02")])
        for status in ("pending", "failed"):
            self.assertEqual(self.scan_designing(self.designing_board(status=status, text="v1")), [], status)
        self.assertEqual(self.scan_designing(self.designing_board(status="approved", text="v1")), [("run", "DV-02")])

    def test_the_design_stage_only_applies_where_it_should(self):
        off = self.designing_board()
        off["automation"]["design"] = False
        self.assertEqual(self.scan_designing(off), [("run", "DV-02")])
        other = self.designing_board()
        other["automation"]["design"] = {"repositories": ["api"]}
        self.assertEqual(self.scan_designing(other), [("run", "DV-02")])
        sent_back = self.designing_board()
        kanvas.find_task(sent_back, "DV-02")["work"]["automation"] = {"status": "completed", "round": 1}
        self.assertEqual(self.scan_designing(sent_back), [("run", "DV-02")])

    def fake_designer(self, reply, returncode=0):
        def fake_run(command, **kwargs):
            self.design_call = {"command": command, "prompt": kwargs["input"], "cwd": kwargs["cwd"]}
            event = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "total_cost_usd": 0.05,
                     "result": reply}
            kwargs["stdout"].write(json.dumps(event) + "\n")
            return mock.Mock(returncode=returncode)
        return mock.patch.object(kanvas, "agent_run", side_effect=fake_run)

    def test_the_board_marks_a_task_while_the_designer_works_on_it_and_clears_the_mark(self):
        write_board(self.path, self.designing_board())
        seen = []

        def fake_run(command, **kwargs):
            seen.append((self.get("DV-02")["work"] or {}).get("busy"))
            event = {"type": "result", "subtype": "success", "is_error": False, "result": "## Goal\nx"}
            kwargs["stdout"].write(json.dumps(event) + "\n")
            return mock.Mock(returncode=0)

        with mock.patch.object(kanvas, "agent_run", side_effect=fake_run):
            kanvas.run_design(self.path, "DV-02")
        self.assertEqual(seen[0]["by"], "designer")
        self.assertTrue(seen[0]["at"])
        self.assertNotIn("busy", self.get("DV-02")["work"])
        with self.fake_designer("", returncode=1):
            kanvas.run_design(self.path, "DV-02")
        self.assertNotIn("busy", self.get("DV-02")["work"])

    def test_the_board_marks_a_task_while_the_reviewer_works_on_it_and_clears_the_mark(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        marks = []
        original = kanvas._run_review
        with mock.patch.object(kanvas, "_run_review", side_effect=lambda *a, **k: marks.append(self.get("DV-02")["work"].get("busy")) or original(*a, **k)):
            self.run_reviewers(self.review_board(repo), [{"verdict": "approve", "summary": "ok", "findings": []}])
        self.assertEqual(marks[0]["by"], "reviewer")
        self.assertNotIn("busy", self.get("DV-02")["work"])

    def test_marks_a_crash_left_behind_are_cleared_when_the_dispatcher_starts(self):
        board = self.designing_board()
        board["automation"]["design"] = False
        kanvas.find_task(board, "DV-02")["work"]["busy"] = {"by": "reviewer", "at": kanvas.now_iso()}
        self.scan_designing(board)
        self.assertNotIn("busy", self.get("DV-02")["work"])

    def test_the_conversation_about_a_design_is_kept_as_a_history_of_rounds(self):
        page = lambda word: f"<!doctype html><html><body><p>{word}</p></body></html>"
        write_board(self.path, self.designing_board())
        with self.fake_designer(f"## v1\n\n```html\n{page('first')}\n```"):
            kanvas.run_design(self.path, "DV-02")
        kanvas.set_design(self.path, "DV-02", False, "use tabs instead")
        with self.fake_designer(f"## v2\n\n```html\n{page('second')}\n```"):
            kanvas.run_design(self.path, "DV-02")
        kanvas.set_design(self.path, "DV-02", True)
        design = self.get("DV-02")["work"]["design"]
        self.assertEqual([(x["type"], x["round"]) for x in design["history"]],
                         [("proposal", 1), ("feedback", 1), ("proposal", 2), ("approved", 2)])
        self.assertEqual(design["history"][1]["text"], "use tabs instead")
        self.assertEqual((design["history"][0]["mockup"], design["history"][2]["mockup"]), (page("first"), page("second")))
        self.assertEqual((design["text"], design["mockup"]), ("## v2", page("second")))
        self.assertTrue(all(x["at"] and x["by"] for x in design["history"]))

    def test_a_design_made_before_the_history_existed_starts_it_from_its_current_proposal(self):
        write_board(self.path, self.designing_board(status="pending", text="old words", round=3, by="designer", at="2026-11-05T10:00:00Z"))
        kanvas.set_design(self.path, "DV-02", False, "more air")
        history = self.get("DV-02")["work"]["design"]["history"]
        self.assertEqual([(x["type"], x["round"]) for x in history], [("proposal", 3), ("feedback", 3)])
        self.assertEqual(history[0]["text"], "old words")

    def test_the_designer_proposes_and_the_human_approves_or_asks_for_changes(self):
        write_board(self.path, self.designing_board())
        with self.fake_designer("## Goal\nA city filter\n\n```\n[ filter ]\n```"):
            self.assertEqual(kanvas.run_design(self.path, "DV-02")[0], "pending")
        command = " ".join(self.design_call["command"])
        self.assertIn("--agent designer", command)
        self.assertIn("--model sonnet", command)
        self.assertIn("--tools Read,Glob,Grep", command)
        self.assertIn("Do not invent a visual language", self.design_call["prompt"])
        for size in ("Mobile layout", "Tablet layout", "Web layout", "iPad"):
            self.assertIn(size, self.design_call["prompt"])
        design = self.get("DV-02")["work"]["design"]
        self.assertEqual((design["status"], design["round"]), ("pending", 1))
        self.assertIn("A city filter", design["text"])
        kanvas.set_design(self.path, "DV-02", False, "keep the filters in a sheet")
        self.assertEqual(self.get("DV-02")["work"]["design"]["status"], "changes_requested")
        with self.fake_designer("## Goal\nFilters in a sheet"):
            kanvas.run_design(self.path, "DV-02")
        self.assertIn("keep the filters in a sheet", self.design_call["prompt"])
        self.assertIn("A city filter", self.design_call["prompt"])
        self.assertEqual(self.get("DV-02")["work"]["design"]["round"], 2)
        kanvas.set_design(self.path, "DV-02", True)
        self.assertEqual(self.get("DV-02")["work"]["design"]["status"], "approved")
        with self.assertRaises(kanvas.KanvasError):
            kanvas.set_design(self.path, "DV-02", True)

    def test_the_mockup_is_taken_from_the_end_of_the_designers_reply(self):
        page = "<!doctype html><html><body><div class=chip>All</div></body></html>"
        prose, mockup = kanvas.split_design_reply(f"## Goal\nA filter\n\n```html\n{page}\n```")
        self.assertEqual((prose, mockup), ("## Goal\nA filter", page))
        self.assertEqual(kanvas.split_design_reply("## Goal\nOnly words"), ("## Goal\nOnly words", ""))
        self.assertEqual(kanvas.split_design_reply("text\n```html\nnot a page at all\n```")[1], "")
        self.assertEqual(kanvas.split_design_reply("```html\n<div>" + "x" * kanvas.MOCKUP_LIMIT + "</div>\n```")[1], "")
        self.assertIn("one fenced ```html block", kanvas.design_prompt(
            kanvas.find_task(self.designing_board(), "DV-02"), kanvas.automation_config(self.designing_board(), self.path), "", ""))

    def test_the_designers_mockup_is_kept_served_sandboxed_and_given_to_the_implementer(self):
        page = "<!doctype html><html><body><div class=chip>All</div><script>1</script></body></html>"
        write_board(self.path, self.designing_board())
        with self.fake_designer(f"## Goal\nA filter\n\n```html\n{page}\n```"):
            kanvas.run_design(self.path, "DV-02")
        design = self.get("DV-02")["work"]["design"]
        self.assertEqual((design["mockup"], design["text"]), (page, "## Goal\nA filter"))
        kanvas.set_design(self.path, "DV-02", True)
        board = kanvas.load_board(self.path)
        config = kanvas.automation_config(board, self.path)
        repo = config["repositories"]["front"]["path"]
        prompt = kanvas.automation_prompt(board, kanvas.find_task(board, "DV-02"), config, self.fake_worktrees(repo))
        self.assertIn(page, prompt)
        self.assertIn("exact look the human accepted", prompt)

    def test_the_implementer_is_told_to_build_the_approved_design(self):
        def prompt_for(**design):
            board = self.designing_board(**design)
            config = kanvas.automation_config(board, self.path)
            repo = config["repositories"]["front"]["path"]
            return kanvas.automation_prompt(board, kanvas.find_task(board, "DV-02"), config, self.fake_worktrees(repo))

        approved = prompt_for(status="approved", text="Filters live in a bottom sheet")
        self.assertIn("Filters live in a bottom sheet", approved)
        self.assertIn("do not invent other UI", approved)
        self.assertNotIn("not yet", prompt_for(status="pending", text="not yet"))

    def test_a_design_that_cannot_be_made_waits_for_the_human(self):
        write_board(self.path, self.designing_board())
        with self.fake_designer("", returncode=1):
            self.assertEqual(kanvas.run_design(self.path, "DV-02")[0], "failed")
        self.assertEqual(self.get("DV-02")["work"]["design"]["status"], "failed")
        self.assertIn("Design failed", self.get("DV-02")["notes"][-1]["text"])

    def test_the_design_command_shows_approves_and_asks_again(self):
        write_board(self.path, self.designing_board(status="pending", text="Filters in a sheet", round=1))
        _, out, _ = self.run_cli("design", "DV-02")
        self.assertIn("design pending", out)
        self.assertIn("Filters in a sheet", out)
        self.run_cli("design", "DV-02", "--changes", "use tabs")
        self.assertEqual(self.get("DV-02")["work"]["design"]["feedback"], "use tabs")
        self.assertIn("no design proposal", self.fails("design", "DV-02", "--approve"))

    def test_the_dispatcher_creates_the_documentation_task_once_and_runs_it(self):
        self.assertEqual(self.scan_launching(self.accepted_board()), [])
        created = self.get("DV-02")["curated"]
        self.assertEqual(self.get(created)["state"], "doing")
        self.assertEqual(self.scan_launching(kanvas.load_board(self.path)), [("run", created)])
        docs = [t for t in kanvas.load_board(self.path)["tasks"] if t["title"].startswith("Docs:")]
        self.assertEqual(len(docs), 1)

    def test_the_dispatcher_does_not_create_a_documentation_task_when_it_does_not_apply(self):
        def never_ran(board):
            del kanvas.find_task(board, "DV-02")["work"]["automation"]

        def not_accepted(board):
            kanvas.find_task(board, "DV-02")["state"] = "review"

        def switched_off(board):
            board["automation"]["curator"] = False

        def brain_only(board):
            kanvas.find_task(board, "DV-02")["work"]["repositories"] = ["brain"]
            kanvas.find_task(board, "DV-02")["definition"]["repositories"] = ["brain"]

        for name, change, with_brain in (("no brain repository", lambda b: None, False), ("never ran", never_ran, True),
                                         ("not accepted", not_accepted, True), ("switched off", switched_off, True),
                                         ("only the brain", brain_only, True)):
            with self.subTest(name):
                board = self.accepted_board(with_brain=with_brain)
                change(board)
                self.scan_launching(board)
                self.assertFalse(self.get("DV-02").get("curated"))
                self.assertEqual(len(kanvas.load_board(self.path)["tasks"]), len(board["tasks"]))

    def test_usage_needs_automation(self):
        self.assertIn("Automation is not enabled", self.fails("usage"))

    def publish_calls(self, branch="feature/DV-02", base="origin/dev", env=None):
        """Runs publish_pull_requests with git and gh faked, and with only the GitHub tokens in `env` in the environment."""
        repo = os.path.join(self.dir, "front")
        self.calls = []

        def fake(command, **kwargs):
            self.calls.append((command, kwargs.get("env")))
            out = "2" if "rev-list" in command else "https://github.com/acme/front/pull/7" if "create" in command else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")

        clean = {k: v for k, v in os.environ.items() if k.upper() not in kanvas.GITHUB_TOKEN_VARS}
        worktree = dict(self.fake_worktrees(repo)[0], branch=branch, base=base)
        with mock.patch.dict(os.environ, {**clean, **(env or {})}, clear=True), \
             mock.patch.object(kanvas.shutil, "which", return_value="gh"), \
             mock.patch.object(kanvas.subprocess, "run", side_effect=fake):
            return kanvas.publish_pull_requests(self.get("DV-02"), [worktree], "Done")

    def test_the_agent_runs_without_github_credentials(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        pull = {"id": "front", "name": "Frontend", "branch": "feature/DV-02", "base": "dev", "commits": 1,
                "url": "https://github.com/acme/front/pull/7"}
        events = [{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "Done"}]
        secrets = {"GH_TOKEN": "one", "GITHUB_TOKEN": "two", "KANVAS_GITHUB_TOKEN": "three",
                   "SSH_AUTH_SOCK": "/tmp/agent.sock", "KEEP_THIS": "yes"}
        with mock.patch.dict(os.environ, secrets):
            run, _ = self.run_claude(repo, events, pulls=[pull])
        env = run.call_args.kwargs["env"]
        for name in secrets:
            if name != "KEEP_THIS":
                self.assertNotIn(name, env)
        self.assertEqual(env["KEEP_THIS"], "yes")

    def test_kanvas_pushes_and_opens_pull_requests_with_a_token_of_its_own_and_nothing_else(self):
        pulls, calls = self.publish_calls(env={"GH_TOKEN": "the-users", "KANVAS_GITHUB_TOKEN": "kanvas-only"}), self.calls
        self.assertEqual(pulls[0]["url"], "https://github.com/acme/front/pull/7")
        pushes = [c for c in calls if "push" in c[0]]
        self.assertEqual(len(pushes), 1)
        command = pushes[0][0]
        self.assertEqual(command[command.index("push"):], ["push", "--set-upstream", "origin", "feature/DV-02"])
        self.assertIn("url.https://github.com/.insteadOf=git@github.com:", command)
        self.assertIn("credential.helper=!gh auth git-credential", command)
        github = [c for c in calls if "push" in c[0] or c[0][0] == "gh"]
        self.assertGreaterEqual(len(github), 3)
        for _, env in github:
            self.assertEqual(env["GH_TOKEN"], "kanvas-only")
            self.assertNotIn("KANVAS_GITHUB_TOKEN", env)
            self.assertEqual((env["GH_PROMPT_DISABLED"], env["GIT_TERMINAL_PROMPT"]), ("1", "0"))

    def test_without_a_token_of_its_own_kanvas_uses_the_computers_credentials(self):
        self.publish_calls(env={"GH_TOKEN": "the-users"})
        command, env = next(c for c in self.calls if "push" in c[0])
        self.assertEqual(command[3:], ["push", "--set-upstream", "origin", "feature/DV-02"])
        self.assertEqual(env["GH_TOKEN"], "the-users")

    def test_kanvas_never_pushes_to_a_protected_branch_or_the_one_a_pull_request_goes_into(self):
        for branch, base in (("dev", "origin/dev"), ("main", "origin/dev"), ("MASTER", "origin/dev"),
                             ("refs/heads/dev", "origin/dev"), ("release", "origin/release")):
            with self.subTest(branch=branch):
                with self.assertRaises(kanvas.KanvasError) as caught:
                    self.publish_calls(branch=branch, base=base)
                self.assertIn("Refusing", str(caught.exception))
                self.assertEqual(self.calls, [])

    def test_a_branch_pattern_that_names_a_protected_branch_is_refused_before_a_worktree_exists(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        board["automation"]["branch_pattern"] = "dev"
        config = kanvas.automation_config(board, self.path)
        with self.assertRaises(kanvas.KanvasError) as caught:
            kanvas.prepare_automation_worktrees(kanvas.find_task(board, "DV-02"), config)
        self.assertIn("Refusing", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(config["worktrees"], "dv-02", "front")))

    def test_successful_run_opens_the_pull_request_and_moves_the_task_to_review(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        pull = {"id": "front", "name": "Frontend", "branch": "feature/DV-02", "base": "dev", "commits": 2,
                "url": "https://github.com/acme/front/pull/7"}
        run, publish = self.run_claude(repo, [
            {"type": "system", "subtype": "init"},
            "not json",
            {"type": "result", "subtype": "success", "is_error": False, "num_turns": 12,
             "total_cost_usd": 0.1234, "result": "Implemented the endpoint; tests pass"},
        ], pulls=[pull])
        task = self.get("DV-02")
        automation = task["work"]["automation"]
        self.assertEqual(task["state"], "review")
        self.assertEqual(task["review"]["agent"]["status"], "pending")
        self.assertEqual((automation["status"], automation["executor"]), ("completed", "claude"))
        self.assertEqual(automation["pull_requests"], [pull])
        self.assertIn("Implemented the endpoint; tests pass", automation["summary"])
        self.assertIn("12 turns, $0.12", automation["summary"])
        self.assertIn("- Frontend: https://github.com/acme/front/pull/7", automation["summary"])
        self.assertEqual(run.call_args.kwargs["cwd"], repo)
        self.assertIn("Implement the complete task", run.call_args.kwargs["input"])
        publish.assert_called_once()

    def test_a_pull_request_failure_leaves_the_task_in_doing_with_the_reason(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        _, publish = self.run_claude(
            repo, [{"type": "result", "subtype": "success", "is_error": False, "result": "All done"}],
            publish_error=kanvas.KanvasError("gh pr create: authentication required"))
        task = self.get("DV-02")
        self.assertEqual(task["state"], "doing")
        automation = task["work"]["automation"]
        self.assertEqual(automation["status"], "failed")
        self.assertIn("All done", automation["summary"])
        self.assertIn("Pull request not opened: gh pr create: authentication required", automation["summary"])

    def test_claude_error_result_skips_the_pull_request(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        _, publish = self.run_claude(
            repo, [{"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""}], returncode=1)
        task = self.get("DV-02")
        self.assertEqual((task["state"], task["work"]["automation"]["status"]), ("doing", "failed"))
        self.assertIn("error_max_turns", task["work"]["automation"]["summary"])
        publish.assert_not_called()

    def test_claude_run_without_a_result_event_fails_with_the_log_path(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        _, publish = self.run_claude(repo, ["Credit balance is too low"], returncode=1)
        automation = self.get("DV-02")["work"]["automation"]
        self.assertEqual(automation["status"], "failed")
        self.assertIn("Claude exited with code 1", automation["summary"])
        self.assertIn("dv-02.jsonl", automation["summary"])
        publish.assert_not_called()

    # --- iterating: sent-back tasks run again with the feedback ---

    def sent_back_board(self, repo, round_no=1, **run):
        board = self.automated_board(repo)
        task = kanvas.find_task(board, "DV-02")
        task["work"]["automation"] = {"status": "completed", "updated": "2026-11-02T10:00:00Z", "round": round_no, **run}
        task["notes"] = [
            {"at": "2026-11-02T09:00:00Z", "by": "human", "text": "Before the first run"},
            {"at": "2026-11-02T10:00:01Z", "by": "kanvas-runner", "text": "Automation completed: done"},
            {"at": "2026-11-02T11:00:00Z", "by": "human", "text": "Sent back: handle the empty city"},
            {"at": "2026-11-02T11:05:00Z", "by": "claude-reviewer", "text": "Agent review changes requested: add a test"},
        ]
        return board

    def scan(self, board_data, **config):
        """Runs one dispatcher pass over the board and returns the (board path, task id) pairs it launched."""
        write_board(self.path, board_data)
        launched, done = [], threading.Event()
        dispatcher = object.__new__(kanvas.AutomationDispatcher)
        dispatcher.lock, dispatcher.active = threading.Lock(), set()

        def fake(path, task_id):
            launched.append(task_id)
            done.set()

        with mock.patch.object(kanvas, "run_automation", side_effect=fake):
            dispatcher.scan(self.path)
            done.wait(2)
        return launched

    def yolo_todo_board(self, repo, yolo=True):
        board = self.automated_board(repo)
        task = kanvas.find_task(board, "DV-02")
        task["state"], task["yolo"] = "todo", yolo
        task.pop("work", None)
        task["definition"] = {"outcome": "o", "needs": "n", "acceptance": "a", "approved": True, "repositories": ["front"]}
        return board

    def plan_by_itself(self, board, job):
        write_board(self.path, board)
        dispatcher = object.__new__(kanvas.AutomationDispatcher)
        dispatcher.lock, dispatcher.active, dispatcher.stop_event = threading.Lock(), set(), threading.Event()
        dispatcher.server = mock.Mock(boards={"b": self.path})
        dispatcher.server.start_assist.return_value = job
        with mock.patch.object(kanvas, "run_automation"):
            dispatcher.scan(self.path)
            for _ in range(50):
                time.sleep(0.1)
                if not dispatcher.active:
                    break
        return dispatcher

    def test_yolo_writes_the_plan_of_a_defined_task_and_starts_it(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        job = {"status": "done", "result": {"draft": {"plan": "Plan by the architect."}, "questions": [{"text": "Which clubs?"}]}}
        dispatcher = self.plan_by_itself(self.yolo_todo_board(repo), job)
        dispatcher.server.start_assist.assert_called_once()
        task = self.get("DV-02")
        self.assertEqual(task["state"], "doing")
        self.assertEqual(task["work"]["plan"], "Plan by the architect.")
        self.assertIn("Which clubs?", task["notes"][-1]["text"])

    def test_without_yolo_nothing_plans_by_itself(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        dispatcher = self.plan_by_itself(self.yolo_todo_board(repo, yolo=False), {"status": "done", "result": {}})
        dispatcher.server.start_assist.assert_not_called()
        self.assertEqual(self.get("DV-02")["state"], "todo")

    def test_a_plan_yolo_could_not_write_leaves_a_note_and_the_task_in_todo(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        dispatcher = self.plan_by_itself(self.yolo_todo_board(repo), {"status": "failed", "error": "boom"})
        task = self.get("DV-02")
        self.assertEqual(task["state"], "todo")
        self.assertIn("could not write the technical plan: boom", task["notes"][-1]["text"])

    def cut_off_board(self, repo, **run):
        board = self.automated_board(repo)
        kanvas.find_task(board, "DV-02")["work"]["automation"] = {"status": "running", "updated": "2026-11-02T10:00:00Z", "round": 1, **run}
        return board

    def test_a_run_cut_off_by_a_restart_is_continued_by_itself_once(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        self.assertEqual(self.scan(self.cut_off_board(repo)), ["DV-02"])
        task = self.get("DV-02")
        self.assertEqual((task["work"]["automation"]["status"], task["work"]["automation"]["resumed"]), ("queued", 1))
        self.assertTrue(any("continues the run by itself" in n["text"] for n in task["notes"]))

    def test_a_second_cut_off_in_the_same_round_waits_for_the_human(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        self.assertEqual(self.scan(self.cut_off_board(repo, resumed=1)), [])
        task = self.get("DV-02")
        self.assertEqual(task["work"]["automation"]["status"], "failed")
        self.assertIn("Press Retry", task["notes"][-1]["text"])

    def test_a_run_that_wrote_nothing_is_continued_once_and_then_fails(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        write_board(self.path, self.automated_board(repo))
        quiet = mock.Mock(returncode=1, stopped="it wrote nothing for more than 10 minutes")

        def fake(command, **kwargs):
            kwargs["stdout"].write("")
            return quiet

        with mock.patch.object(kanvas, "prepare_automation_worktrees", return_value=self.fake_worktrees(repo)),                 mock.patch.object(kanvas, "agent_run", side_effect=fake):
            kanvas.run_automation(self.path, "DV-02")
            first = self.get("DV-02")["work"]["automation"]
            kanvas.update_automation(self.path, "DV-02", "running")
            kanvas.run_automation(self.path, "DV-02")
        self.assertEqual((first.get("status"), first.get("resumed")), (None, 1))
        self.assertEqual(self.get("DV-02")["work"]["automation"]["status"], "failed")

    def test_a_retry_by_hand_starts_a_new_count_of_review_rounds(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.sent_back_board(repo, round_no=3, cycle_start=1)
        kanvas.find_task(board, "DV-02")["work"]["automation"].update(status="failed", summary="Stopped after 3 rounds (automation.max_rounds).")
        write_board(self.path, board)
        self.run_cli("retry", "DV-02", "--as", "human")
        run = self.get("DV-02")["work"]["automation"]
        self.assertEqual((run["round"], run["cycle_start"]), (3, 3))

    # --- CI: the checks of the pull requests pass before the task goes to Review ---

    def run_with_ci(self, repo, verdicts, heads=None, **ci):
        """Runs a task whose pull request has the given check verdicts in turn; returns (agent_run, publish)."""
        board = self.automated_board(repo)
        board["automation"]["ci"] = {"enabled": True, **ci}
        write_board(self.path, board)
        sequence = iter(verdicts)
        commits = iter(heads or [f"c{i}" for i in range(20)])

        def completed(command, **kwargs):
            kwargs["stdout"].write(json.dumps(self.success_events()[0]) + chr(10))
            return mock.Mock(returncode=0)

        publish = mock.Mock(return_value=[self.agent_pull()])
        failure = {"pull": self.agent_pull(), "check": "Unit tests", "url": "https://github.com/acme/front/actions/runs/99/job/1"}
        with mock.patch.object(kanvas, "prepare_automation_worktrees", return_value=self.fake_worktrees(repo)),              mock.patch.object(kanvas, "publish_pull_requests", publish), mock.patch.object(kanvas, "save_changes"),              mock.patch.object(kanvas, "agent_run", side_effect=completed) as run,              mock.patch.object(kanvas, "_heads", side_effect=lambda w: [next(commits)]),              mock.patch.object(kanvas, "ci_failure_logs", return_value="FAILED: the date test"),              mock.patch.object(kanvas, "wait_for_checks", side_effect=lambda *a, **k: (lambda v: (v, [failure] if v == "failed" else []))(next(sequence))):
            kanvas.run_automation(self.path, "DV-02")
        return run, publish

    def test_a_task_whose_checks_pass_goes_to_review(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        run, _ = self.run_with_ci(repo, ["passed"])
        self.assertEqual(run.call_count, 1)
        task = self.get("DV-02")
        self.assertEqual(task["state"], "review")
        self.assertIn("CI: all checks passed.", task["work"]["automation"]["summary"])

    def test_the_agent_fixes_a_failing_check_before_the_task_goes_to_review(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        run, publish = self.run_with_ci(repo, ["failed", "passed"])
        self.assertEqual(run.call_count, 2)
        prompt = run.call_args_list[1].kwargs["input"]
        self.assertIn("failing CI", prompt)
        self.assertIn("Unit tests", prompt)
        self.assertIn("FAILED: the date test", prompt)
        self.assertEqual(publish.call_count, 2)
        task = self.get("DV-02")
        self.assertEqual(task["state"], "review")
        self.assertIn("after 1 fix by the agent", task["work"]["automation"]["summary"])

    def test_a_task_whose_checks_keep_failing_stays_in_doing_after_the_allowed_fixes(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        run, _ = self.run_with_ci(repo, ["failed"] * 5, max_fixes=2)
        self.assertEqual(run.call_count, 3)
        task = self.get("DV-02")
        self.assertEqual((task["state"], task["work"]["automation"]["status"]), ("doing", "failed"))
        self.assertIn("CI still fails after 2 fixes", task["work"]["automation"]["summary"])

    def test_a_fix_that_changes_nothing_is_not_pushed_again(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        run, publish = self.run_with_ci(repo, ["failed"] * 3, heads=["same", "same"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(publish.call_count, 1)
        self.assertIn("changed nothing", self.get("DV-02")["work"]["automation"]["summary"])

    def test_the_checks_of_a_pull_request_are_read_as_passed_failed_or_pending(self):
        rollup = json.dumps([
            {"name": "Lint", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "https://github.com/a/b/actions/runs/1/job/2"},
            {"name": "Unit tests", "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": "https://github.com/a/b/actions/runs/3/job/4"},
            {"name": "Build", "status": "IN_PROGRESS", "conclusion": ""},
            {"context": "legacy", "state": "SUCCESS", "targetUrl": "https://ci"}])
        with mock.patch.object(kanvas, "_gh", return_value=rollup):
            checks = kanvas.pull_request_checks("/x", "https://github.com/a/b/pull/1")
        self.assertEqual([(c["name"], c["state"]) for c in checks],
                         [("Lint", "passed"), ("Unit tests", "failed"), ("Build", "pending"), ("legacy", "passed")])

    def test_waiting_for_checks_ends_when_every_one_has_finished(self):
        pull = self.agent_pull()
        states = iter([[{"name": "Build", "state": "pending", "url": ""}], [{"name": "Build", "state": "passed", "url": ""}]])
        with mock.patch.object(kanvas, "pull_request_checks", side_effect=lambda *a: next(states)),              mock.patch.object(kanvas, "CI_POLL_SECONDS", 0):
            self.assertEqual(kanvas.wait_for_checks([pull], {"front": "/x"}, 5), ("passed", []))

    def test_waiting_for_checks_gives_up_when_no_ci_answers(self):
        with mock.patch.object(kanvas, "pull_request_checks", return_value=[]), mock.patch.object(kanvas, "CI_POLL_SECONDS", 0),              mock.patch.object(kanvas, "CI_APPEAR_SECONDS", 0):
            self.assertEqual(kanvas.wait_for_checks([self.agent_pull()], {"front": "/x"}, 5), ("none", []))

    def test_a_task_sent_back_to_doing_runs_again_as_the_next_round(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        launched = self.scan(self.sent_back_board(repo, pull_requests=[{"url": "https://github.com/a/b/pull/1"}]))
        self.assertEqual(launched, ["DV-02"])
        run = self.get("DV-02")["work"]["automation"]
        self.assertEqual((run["status"], run["round"], run["since"]), ("queued", 2, "2026-11-02T10:00:00Z"))
        self.assertEqual(run["pull_requests"], [{"url": "https://github.com/a/b/pull/1"}])

    def test_a_completed_run_still_in_review_does_not_run_again(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.sent_back_board(repo)
        kanvas.find_task(board, "DV-02")["state"] = "review"
        self.assertEqual(self.scan(board), [])

    def test_the_round_limit_stops_the_loop_and_says_why(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.sent_back_board(repo, round_no=3)
        launched = self.scan(board)
        self.assertEqual(launched, [])
        task = self.get("DV-02")
        self.assertEqual(task["state"], "doing")
        self.assertEqual(task["work"]["automation"]["status"], "failed")
        self.assertIn("Stopped after 3 rounds", task["work"]["automation"]["summary"])
        self.assertIn("Stopped after 3 rounds", task["notes"][-1]["text"])
        board = self.sent_back_board(repo, round_no=3)
        board["automation"]["max_rounds"] = 4
        self.assertEqual(self.scan(board), ["DV-02"])
        self.assertEqual(self.get("DV-02")["work"]["automation"]["round"], 4)

    def test_max_rounds_must_be_a_positive_whole_number(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        for bad in (0, -1, "3", 1.5, True):
            board["automation"]["max_rounds"] = bad
            with self.assertRaises(kanvas.KanvasError):
                kanvas.automation_config(board, self.path)
        board["automation"]["max_rounds"] = 5
        self.assertEqual(kanvas.automation_config(board, self.path)["max_rounds"], 5)

    def test_feedback_is_the_notes_since_the_last_round_without_the_runner_s_own(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.sent_back_board(repo)
        task = kanvas.find_task(board, "DV-02")
        run = dict(task["work"]["automation"], since="2026-11-02T10:00:00Z")
        text = kanvas.automation_feedback(task, run, self.dir)
        self.assertIn("human (2026-11-02T11:00:00Z): Sent back: handle the empty city", text)
        self.assertIn("claude-reviewer", text)
        self.assertNotIn("Before the first run", text)
        self.assertNotIn("Automation completed", text)

    def test_feedback_includes_pull_request_comments_reviews_and_inline_comments(self):
        pulls = [{"url": "https://github.com/acme/front/pull/7"}]
        views = json.dumps({
            "comments": [{"author": {"login": "sergi"}, "createdAt": "2026-11-02T11:10:00Z", "body": "Rename the helper"},
                         {"author": {"login": "sergi"}, "createdAt": "2026-11-02T09:00:00Z", "body": "Too old"}],
            "reviews": [{"author": {"login": "sergi"}, "submittedAt": "2026-11-02T11:11:00Z",
                         "state": "CHANGES_REQUESTED", "body": "Needs work"}]})
        inline = json.dumps([{"user": {"login": "sergi"}, "created_at": "2026-11-02T11:12:00Z",
                              "path": "src/greet.ts", "line": 4, "body": "Use a const"}])
        with mock.patch.object(kanvas, "_gh", side_effect=[views, inline]) as gh:
            lines = kanvas.pull_request_feedback(pulls, "2026-11-02T10:00:00Z", self.dir)
        self.assertEqual(len(lines), 3)
        self.assertIn("Rename the helper", lines[0])
        self.assertIn("(changes requested)", lines[1])
        self.assertIn("at src/greet.ts:4", lines[2])
        self.assertEqual(gh.call_args_list[1].args[1:3], ("api", "repos/acme/front/pulls/7/comments"))

    def test_unreadable_pull_request_comments_do_not_stop_the_round(self):
        with mock.patch.object(kanvas, "_gh", side_effect=kanvas.KanvasError("gh: not logged in")):
            lines = kanvas.pull_request_feedback([{"url": "https://github.com/a/b/pull/1"}], "", self.dir)
        self.assertEqual(len(lines), 1)
        self.assertIn("could not read the comments", lines[0])

    def test_round_two_prompt_carries_the_feedback(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        config = kanvas.automation_config(board, self.path)
        task = kanvas.find_task(board, "DV-02")
        first = kanvas.automation_prompt(board, task, config, self.fake_worktrees(repo))
        self.assertNotIn("Feedback to address", first)
        second = kanvas.automation_prompt(board, task, config, self.fake_worktrees(repo), 2, "- human: handle the empty city")
        self.assertIn("This is round 2", second)
        self.assertIn("- human: handle the empty city", second)

    def test_a_follow_up_round_sends_the_feedback_to_claude_and_comments_on_the_same_pull_request(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.sent_back_board(repo)
        kanvas.find_task(board, "DV-02")["work"]["automation"].update(
            status="queued", round=2, since="2026-11-02T10:00:00Z")
        write_board(self.path, board)
        pull = {"id": "front", "name": "Frontend", "branch": "feature/DV-02", "base": "dev", "commits": 3,
                "url": "https://github.com/acme/front/pull/7"}

        def completed(command, **kwargs):
            with open(os.path.join(self.dir, ".kanvas", "runs", "dv-02.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "result", "is_error": False, "result": "Handled the empty city"}) + "\n")
            return mock.Mock(returncode=0)

        publish = mock.Mock(return_value=[pull])
        with mock.patch.object(kanvas, "prepare_automation_worktrees", return_value=self.fake_worktrees(repo)), \
             mock.patch.object(kanvas, "publish_pull_requests", publish), mock.patch.object(kanvas, "save_changes"), \
             mock.patch.object(kanvas, "agent_run", side_effect=completed) as claude:
            kanvas.run_automation(self.path, "DV-02")
        prompt = claude.call_args.kwargs["input"]
        self.assertIn("This is round 2", prompt)
        self.assertIn("Sent back: handle the empty city", prompt)
        self.assertIn("add a test", prompt)
        self.assertNotIn("Before the first run", prompt)
        self.assertEqual(publish.call_args.args[3], 2)
        task = self.get("DV-02")
        self.assertEqual((task["state"], task["work"]["automation"]["round"]), ("review", 2))
        self.assertTrue(task["work"]["automation"]["summary"].startswith("Round 2. Handled the empty city"))

    def test_publish_comments_on_an_open_pull_request_from_round_two(self):
        repo, git = self.git_repo_with_remote("front")
        with open(os.path.join(repo, "b.txt"), "w") as f:
            f.write("b\n")
        git("add", "b.txt")
        git("commit", "-m", "feat: b")
        task = kanvas.find_task(self.automated_board(repo), "DV-02")
        front = {"id": "front", "name": "Frontend", "base": "origin/dev", "branch": "feature/DV-02", "worktree": repo}
        with mock.patch.object(kanvas, "_gh", return_value="https://github.com/acme/front/pull/9") as gh:
            kanvas.publish_pull_requests(task, [front], "Handled it", 2)
        self.assertEqual([c.args[1:3] for c in gh.call_args_list], [("pr", "list"), ("pr", "comment")])
        self.assertIn("Round 2", gh.call_args_list[1].args[-1])

    def test_retry_keeps_the_round_and_the_pull_requests(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.automated_board(repo)
        task = kanvas.find_task(board, "DV-02")
        task["work"]["automation"] = {"status": "failed", "summary": "boom", "round": 2, "since": "2026-11-02T10:00:00Z",
                                      "pull_requests": [{"url": "https://github.com/a/b/pull/1"}]}
        write_board(self.path, board)
        self.run_cli("retry", "DV-02", "--as", "human")
        self.assertEqual(self.get("DV-02")["work"]["automation"],
                         {"round": 2, "since": "2026-11-02T10:00:00Z", "pull_requests": [{"url": "https://github.com/a/b/pull/1"}], "cycle_start": 2})
        launched = self.scan(kanvas.load_board(self.path))
        self.assertEqual(launched, ["DV-02"])
        self.assertEqual(self.get("DV-02")["work"]["automation"]["round"], 2)

    def git_repo_with_remote(self, name):
        """A repository on branch dev whose `origin` is a bare repository, plus a task branch with commits."""
        bare = os.path.join(self.dir, name + "-origin.git")
        repo = os.path.join(self.dir, name)
        git = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
        os.makedirs(repo)
        subprocess.run(["git", "init", "--bare", bare], check=True, capture_output=True)
        git("init", "-b", "dev")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("a\n")
        git("add", "a.txt")
        git("commit", "-m", "init")
        git("remote", "add", "origin", bare)
        git("push", "origin", "dev")
        git("fetch", "origin")
        git("checkout", "-b", "feature/DV-02")
        return repo, git

    def test_publish_pushes_the_branch_and_opens_one_pull_request_per_changed_repository(self):
        repo, git = self.git_repo_with_remote("front")
        with open(os.path.join(repo, "b.txt"), "w") as f:
            f.write("b\n")
        git("add", "b.txt")
        git("commit", "-m", "feat: b")
        board = self.automated_board(repo)
        task = kanvas.find_task(board, "DV-02")
        unchanged = {"id": "api", "name": "API", "base": "origin/dev", "branch": "feature/DV-02",
                     "worktree": repo, "path": repo}
        # An API worktree with no commits ahead of its base is skipped.
        with mock.patch.object(kanvas, "_git", side_effect=lambda path, *a, **kw: "0" if a[:1] == ("rev-list",) and path == "/api" else subprocess.run(
                ["git", "-C", path, *a], capture_output=True, text=True, check=True).stdout.strip()):
            front = {"id": "front", "name": "Frontend", "base": "origin/dev", "branch": "feature/DV-02", "worktree": repo}
            api = dict(unchanged, worktree="/api")
            with mock.patch.object(kanvas, "_gh", side_effect=["", "https://github.com/acme/front/pull/9\n"]) as gh:
                pulls = kanvas.publish_pull_requests(task, [front, api], "Did it")
        self.assertEqual(len(pulls), 1)
        self.assertEqual((pulls[0]["id"], pulls[0]["base"], pulls[0]["commits"]), ("front", "dev", 1))
        self.assertEqual(pulls[0]["url"], "https://github.com/acme/front/pull/9")
        create = gh.call_args_list[1].args
        self.assertEqual(create[1:3], ("pr", "create"))
        self.assertEqual(create[create.index("--base") + 1], "dev")
        self.assertEqual(create[create.index("--title") + 1], "DV-02: " + task["title"])
        self.assertIn("Did it", create[create.index("--body") + 1])
        remote = subprocess.run(["git", "-C", repo, "ls-remote", "--heads", "origin", "feature/DV-02"],
                                capture_output=True, text=True, check=True).stdout
        self.assertIn("refs/heads/feature/DV-02", remote)

    def test_publish_reuses_an_open_pull_request_when_retrying(self):
        repo, git = self.git_repo_with_remote("front")
        with open(os.path.join(repo, "b.txt"), "w") as f:
            f.write("b\n")
        git("add", "b.txt")
        git("commit", "-m", "feat: b")
        task = kanvas.find_task(self.automated_board(repo), "DV-02")
        front = {"id": "front", "name": "Frontend", "base": "origin/dev", "branch": "feature/DV-02", "worktree": repo}
        with mock.patch.object(kanvas, "_gh", return_value="https://github.com/acme/front/pull/9") as gh:
            pulls = kanvas.publish_pull_requests(task, [front], "Again")
        self.assertEqual(gh.call_count, 1)
        self.assertEqual(pulls[0]["url"], "https://github.com/acme/front/pull/9")

    def test_publish_fails_without_commits_or_with_uncommitted_changes(self):
        repo, git = self.git_repo_with_remote("front")
        task = kanvas.find_task(self.automated_board(repo), "DV-02")
        front = {"id": "front", "name": "Frontend", "base": "origin/dev", "branch": "feature/DV-02", "worktree": repo}
        with self.assertRaises(kanvas.KanvasError) as caught:
            kanvas.publish_pull_requests(task, [front], "x")
        self.assertIn("no commits", str(caught.exception))
        with open(os.path.join(repo, "b.txt"), "w") as f:
            f.write("b\n")
        git("add", "b.txt")
        git("commit", "-m", "feat: b")
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("changed\n")
        with self.assertRaises(kanvas.KanvasError) as caught:
            kanvas.publish_pull_requests(task, [front], "x")
        self.assertIn("uncommitted", str(caught.exception))

    def test_publish_explains_a_missing_github_cli(self):
        with mock.patch.object(kanvas.shutil, "which", return_value=None):
            with self.assertRaises(kanvas.KanvasError) as caught:
                kanvas._gh(self.dir, "pr", "list")
        self.assertIn("GitHub CLI", str(caught.exception))


class QuestionTests(TempBoard):
    def test_ask_shows_the_wait_until_the_next_note(self):
        self.run_cli("start", "DV-02", "--as", "claude", "-m", "Plan")
        _, out, _ = self.run_cli("ask", "DV-02", "--as", "claude", "Question: SQLite or Postgres?")
        self.assertIn("DV-02 now shows the human that you wait for their reply", out)
        last = self.get("DV-02")["notes"][-1]
        self.assertEqual((last["by"], last["text"]), ("claude", "Question: SQLite or Postgres?"))
        self.assertIn("DV-02  Task DV-02  (waiting for the human: SQLite or Postgres?)", self.run_cli("status")[1])
        self.assertIn("Waits:  for the human to reply to the question claude asked", self.run_cli("show", "DV-02")[1])
        _, out, _ = self.run_cli("note", "DV-02", "--as", "claude", "SQLite, the human said")
        self.assertIn("DV-02 no longer waits for the human.", out)
        self.assertIsNone(kanvas.open_question(self.get("DV-02")))
        self.assertNotIn("waiting for the human", self.run_cli("status")[1])
        self.assertNotIn("no longer waits", self.run_cli("note", "DV-02", "Another note")[1])

    def test_ask_needs_a_question_on_a_task_in_progress(self):
        self.assertIn("DV-02 is todo; ask on a task you are working on", self.fails("ask", "DV-02", "Which one?"))
        self.assertIn("DV-03 is blocked", self.fails("ask", "DV-03", "Which one?"))
        self.run_cli("start", "DV-02", "-m", "Plan")
        before = self.raw()
        self.assertIn("The question is empty", self.fails("ask", "DV-02", "Question:"))
        self.assertEqual(self.raw(), before)

    def test_leaving_doing_ends_the_wait(self):
        self.run_cli("start", "DV-02", "-m", "Plan")
        self.run_cli("ask", "DV-02", "Which one?")
        t = self.get("DV-02")
        self.assertEqual(kanvas.open_question(t)["text"], "Question: Which one?")
        t["state"] = "review"
        self.assertIsNone(kanvas.open_question(t))
        self.run_cli("finish", "DV-02", "-m", "Done without an answer")
        self.assertIsNone(kanvas.open_question(self.get("DV-02")))

    def test_html_open_question_matches_python(self):
        q = {"at": "2026-10-26T09:00:00Z", "by": "claude", "text": "Question: Which one?"}
        later = {"at": "2026-10-26T09:05:00Z", "by": "claude", "text": "The human chose the first"}
        board = kanvas.new_board()
        board["tasks"] = [task("Q-01", "doing", notes=[q]), task("Q-02", "doing", notes=[q, later]),
                          task("Q-03", "review", notes=[q]), task("Q-04", "doing", notes=[dict(q, by="human")]),
                          task("Q-05", "doing", notes=[dict(q, text="Questions: none")]), task("Q-06", "doing"),
                          task("Q-07", "doing", notes=[later, q])]
        out = run_html_js(self, "process.stdout.write(JSON.stringify(normalize(input).tasks.map((t) => !!openQuestion(t))));", json.dumps(board))
        expected = [kanvas.open_question(t) is not None for t in board["tasks"]]
        self.assertEqual(json.loads(out), expected)
        self.assertEqual(expected, [True, False, False, False, False, False, True])


class ChangeRequestTests(TempBoard):
    REQUEST = {"at": "2026-10-26T09:00:00Z", "by": "human", "text": "Changes requested: Split it in two"}
    HUMAN = {"at": "2026-10-26T09:02:00Z", "by": "human", "text": "And keep the cache out of it"}
    AGENT = {"at": "2026-10-26T09:05:00Z", "by": "codex", "text": "Split as asked"}

    def request(self, *notes):
        board = self.board()
        kanvas.find_task(board, "DV-04")["notes"] = [self.REQUEST, *notes]
        write_board(self.path, board)

    def test_status_and_show_point_at_the_request_until_an_agent_answers(self):
        self.request()
        self.assertIn("DV-04  Task DV-04  (changes requested: Split it in two)", self.run_cli("status")[1])
        self.assertIn("Revise: the human asked for changes", self.run_cli("show", "DV-04")[1])
        _, out, _ = self.run_cli("edit", "DV-04", "--title", "The endpoint", "--as", "codex")
        self.assertIn("The human's request for changes on DV-04 stays open until you say what you changed", out)
        self.assertIn("(changes requested: Split it in two)", self.run_cli("status")[1])
        _, out, _ = self.run_cli("note", "DV-04", "--as", "codex", "Split: the caching is now DV-05")
        self.assertIn("This note answers the human's request for changes on DV-04.", out)
        self.assertIsNone(kanvas.change_request(self.get("DV-04")))
        self.assertNotIn("changes requested", self.run_cli("status")[1])
        self.assertNotIn("Revise:", self.run_cli("show", "DV-04")[1])
        self.assertNotIn("request for changes", self.run_cli("note", "DV-04", "Another note")[1])

    def test_the_human_may_add_to_the_request_and_edit_with_a_note_answers_it(self):
        self.request(self.HUMAN)
        self.assertIn("(changes requested: Split it in two)", self.run_cli("status")[1])
        _, out, _ = self.run_cli("edit", "DV-04", "--desc", "Only the endpoint", "-m", "Split as asked", "--as", "codex")
        self.assertIn("This answers the human's request for changes on DV-04.", out)
        self.assertIsNone(kanvas.change_request(self.get("DV-04")))

    def test_a_proposal_under_yolo_is_not_sent_back_for_revision(self):
        self.request()
        board = self.board()
        kanvas.find_task(board, "DV-04")["yolo"] = True
        write_board(self.path, board)
        self.assertNotIn("changes requested", self.run_cli("status")[1])
        self.assertNotIn("Revise:", self.run_cli("show", "DV-04")[1])

    def test_html_change_request_matches_python(self):
        r, human, agent = self.REQUEST, self.HUMAN, self.AGENT
        unsigned = {k: v for k, v in agent.items() if k != "by"}
        board = kanvas.new_board()
        board["tasks"] = [task("C-01", "proposed", notes=[r]), task("C-02", "proposed", notes=[r, human]),
                          task("C-03", "proposed", notes=[r, agent]), task("C-04", "todo", notes=[r]),
                          task("C-05", "proposed", notes=[dict(r, by="codex")]), task("C-06", "proposed", notes=[dict(r, text="Changes requested")]),
                          task("C-07", "proposed"), task("C-08", "proposed", notes=[agent, r]),
                          task("C-09", "proposed", notes=[r, agent, human]), task("C-10", "proposed", notes=[r, unsigned])]
        out = run_html_js(self, "process.stdout.write(JSON.stringify(normalize(input).tasks.map((t) => !!changeRequest(t))));", json.dumps(board))
        expected = [kanvas.change_request(t) is not None for t in board["tasks"]]
        self.assertEqual(json.loads(out), expected)
        self.assertEqual(expected, [True, True, False, False, False, False, False, True, False, False])


class YoloTests(TempBoard):
    def initial_board(self):
        board = super().initial_board()
        board["tasks"][2]["yolo"] = True  # DV-03, which depends on DV-02
        board["tasks"].append(task("DV-05", "todo", "DV", ["DV-01"], definition={"approved": True, "outcome": "Task DV-05"}))
        return board

    def test_yolo_is_stored_after_deps(self):
        t = json.loads(self.raw())["tasks"][2]
        self.assertEqual(list(t), ["id", "title", "state", "group", "deps", "yolo", "definition"])

    def test_yolo_never_skips_review(self):
        _, out, _ = self.run_cli("ready")
        self.assertEqual(out.splitlines(), ["DV-02  Task DV-02  (YOLO)", "DV-05  Task DV-05"])
        self.run_cli("start", "DV-02", "-m", "Plan")
        _, out, _ = self.run_cli("finish", "DV-02", "-m", "Parser done")
        self.assertIn("doing -> review", out)
        self.assertEqual(self.get("DV-02")["state"], "review")
        self.assertIn("blocked by unfinished DV-02", self.fails("start", "DV-03", "-m", "Plan"))
        self.run_cli("start", "DV-05", "-m", "Plan")
        self.run_cli("finish", "DV-05", "-m", "Outside the chain")
        self.assertEqual(self.get("DV-05")["state"], "review")

    def test_nested_yolo_still_requires_review_for_each_task(self):
        board = self.board()
        kanvas.find_task(board, "DV-02")["yolo"] = True
        board["tasks"].append(board["tasks"].pop(1))  # DV-03's YOLO comes first in the file
        write_board(self.path, board)
        _, out, _ = self.run_cli("status")
        self.assertIn("YOLO on DV-03:", out)
        _, out, _ = self.run_cli("show", "DV-02")
        self.assertIn("YOLO:   through DV-03", out)
        self.run_cli("start", "DV-02", "-m", "Plan")
        _, out, _ = self.run_cli("finish", "DV-02", "-m", "Parser done")
        self.assertIn("doing -> review", out)
        self.assertEqual(self.get("DV-02")["state"], "review")
        self.assertIn("blocked by unfinished DV-02", self.fails("start", "DV-03", "-m", "Plan"))

    def test_html_yolo_cover_matches_python(self):
        board = kanvas.new_board()
        board["tasks"] = [task("X-03", deps=["X-02"], yolo=True), task("X-01"), task("X-02", deps=["X-01"], yolo=True),
                          task("X-04", deps=["X-01"], yolo=True), task("X-05", deps=["X-03", "X-04"]),
                          task("X-06"), task("X-07", "review", deps=["X-06"], yolo=True), task("X-08", "done", deps=["X-06"], yolo=True)]
        out = run_html_js(self, "process.stdout.write(JSON.stringify(Object.fromEntries(yoloCoverIn(normalize(input)))));", json.dumps(board))
        cover = kanvas.yolo_cover(board)
        self.assertEqual(json.loads(out), cover)
        self.assertEqual(cover, {"X-03": "X-03", "X-02": "X-03", "X-01": "X-03", "X-04": "X-04"})

    def test_html_yolo_takes_over_the_chain(self):
        board = kanvas.new_board()
        board["tasks"] = [task("Y-01", "review", yolo=True), task("Y-02", "proposed", deps=["Y-01"]),
                          task("Y-03", deps=["Y-02"], yolo=True), task("Y-04", "proposed", deps=["Y-03", "Y-05"]),
                          task("Y-05", "done", yolo=True), task("Y-06", "review", yolo=True)]
        out = run_html_js(self, "const b = normalize(input); yoloOn(b, 'y-04');"
                                "process.stdout.write(JSON.stringify(b.tasks.map((t) => [t.id, t.state, !!t.yolo])));", json.dumps(board))
        self.assertEqual(json.loads(out), [["Y-01", "review", False], ["Y-02", "proposed", False], ["Y-03", "todo", False],
                                           ["Y-04", "proposed", True], ["Y-05", "done", True], ["Y-06", "review", True]])

    def test_yolo_ends_when_its_task_reaches_review(self):
        board = self.board()
        kanvas.find_task(board, "DV-03")["state"] = "review"
        write_board(self.path, board)
        _, out, _ = self.run_cli("status")
        self.assertNotIn("YOLO on", out)
        _, out, _ = self.run_cli("ready")
        self.assertNotIn("(YOLO)", out)
        self.run_cli("start", "DV-02", "-m", "Plan")
        self.run_cli("finish", "DV-02", "-m", "Parser done")
        self.assertEqual(self.get("DV-02")["state"], "review")

    def test_a_proposal_linked_into_the_chain_waits_for_approval(self):
        self.run_cli("propose", "Development", "Delete the production database", "--as", "codex")
        self.assertIn("a human must approve it", self.fails("start", "DV-06"))
        _, out, _ = self.run_cli("depend", "DV-02", "DV-04", "DV-06")
        self.assertIn("DV-02 now depends on DV-04, DV-06 (blocked until they are done)", out)
        self.assertIn("ask the human to approve DV-04, DV-06 in the board", out)
        before = self.raw()
        for tid in ("DV-04", "DV-06"):
            self.assertIn("a human must approve it", self.fails("start", tid))
        self.assertEqual(self.raw(), before)
        out = self.run_cli("status")[1].replace("\r\n", "\n")
        self.assertIn("Proposed, awaiting human approval (2)\n  DV-04  Task DV-04  (YOLO)\n  DV-06  Delete the production database  (YOLO)\n", out)
        self.assertIn("DV-02  Task DV-02  (waiting on DV-04, DV-06)  (YOLO)", out)
        self.assertIn("YOLO:   joins the chain of DV-03 once a human approves it", self.run_cli("show", "DV-06")[1])
        self.assertNotIn("ask the human", self.run_cli("depend", "DV-03", "DV-04")[1])
        board = self.board()
        approved = kanvas.find_task(board, "DV-06")
        approved["state"] = "todo"
        approved["definition"] = {"approved": True, "outcome": "Delete the production database"}
        write_board(self.path, board)
        self.assertIn("DV-06  Delete the production database  (YOLO)", self.run_cli("ready")[1])
        self.run_cli("start", "DV-06", "-m", "Plan")
        self.run_cli("finish", "DV-06", "-m", "Done")
        self.assertEqual(self.get("DV-06")["state"], "review")

    def test_html_links_never_approve_proposals_in_a_yolo_chain(self):
        board = kanvas.new_board()
        board["tasks"] = [task("Y-01", "proposed"), task("Y-02", deps=["Y-01"]), task("Y-03", deps=["Y-02"], yolo=True),
                          task("Y-04", "proposed", deps=["Y-05"]), task("Y-05", "proposed"), task("Y-06", "proposed"), task("Y-07", "proposed")]
        code = ("const b = normalize(input), ix = indexOf(b), seen = b.tasks.map((t) => vstateIn(ix, t));"
                "ops.addDep('Y-02', 'Y-04')(b); ops.addTask({ title: 'New', deps: ['Y-06'], blocks: ['Y-03'] }, {})(b);"
                "process.stdout.write(JSON.stringify([seen, b.tasks.map((t) => t.state)]));")
        sections = [("function mustFind(", "\n// ----"), ("function inlineMd(", "\n// ----")]
        seen, after = json.loads(run_html_js(self, code, json.dumps(board), sections))
        tasks = kanvas.task_map(board)
        self.assertEqual(seen, [kanvas.view_state(t, tasks) for t in board["tasks"]])
        self.assertEqual(seen, ["proposed", "blocked", "blocked", "proposed", "proposed", "proposed", "proposed"])
        self.assertEqual(after, ["proposed", "todo", "todo", "proposed", "proposed", "proposed", "proposed", "proposed"])


class ProposalTests(TempBoard):
    def test_group_prefix_with_digits(self):
        self.run_cli("add-group", "Level 0 Contract")
        self.run_cli("propose", "Level 0 Contract", "Define the contract", "Scope and terms")
        t = self.get("L0C-01")
        self.assertEqual((t["state"], t["group"], t["desc"]), ("proposed", "L0C", "Scope and terms"))
        self.assertIn("approve", self.fails("start", "L0C-01"))
        self.run_cli("note", "L0C-01", "ids with digits in the prefix work")
        self.run_cli("depend", "L0C-01", "DV-02")
        self.assertEqual(self.get("l0c-01")["deps"], ["DV-02"])

    def test_propose_without_group_and_with_deps(self):
        self.run_cli("propose", "-", "Loose end", "--deps", "DV-02", "DV-03")
        t = self.get("G-01")
        self.assertEqual((t["group"], t["deps"]), (None, ["DV-02", "DV-03"]))
        self.assertIn("not found", self.fails("propose", "Development", "x", "--deps", "DV-99"))

    def test_batch_resolves_titles_and_creates_groups(self):
        spec = {
            "groups": ["Research", {"name": "Quality", "prefix": "QA"}],
            "tasks": [
                {"title": "Survey users", "group": "Research"},
                {"title": "Write test plan", "group": "QA", "deps": ["Survey users", "DV-02"]},
                {"title": "Automate tests", "group": "Quality", "depends_on": "Write test plan", "description": "CI"},
            ],
        }
        _, out, _ = self.run_cli("batch", stdin=json.dumps(spec).encode("utf-8"))
        self.assertIn("Proposed 3 tasks", out)
        self.assertEqual(self.get("QA-01")["deps"], ["RS-01", "DV-02"])
        self.assertEqual(self.get("QA-02")["deps"], ["QA-01"])
        self.assertEqual(self.get("QA-02")["desc"], "CI")
        self.assertTrue(all(self.get(i)["state"] == "proposed" for i in ("RS-01", "QA-01", "QA-02")))

    def test_batch_is_all_or_nothing(self):
        before = self.raw()
        spec = {"groups": ["Research"], "tasks": [{"title": "ok", "group": "Research"}, {"title": "bad", "deps": ["Nope"]}]}
        self.assertIn("Dependency 'Nope' not found", self.fails("batch", stdin=json.dumps(spec).encode("utf-8")))
        self.assertEqual(self.raw(), before)

    def test_batch_rejects_malformed_input(self):
        before = self.raw()
        ok = {"title": "Fine", "group": "Development"}
        cases = [
            ({"groups": "Backend"}, 'Batch "groups" must be a list'),
            ({"tasks": "nope"}, 'Batch "tasks" must be a list'),
            ({}, "The batch has no groups or tasks"),
            ({"groups": [["Research"]]}, "Group #1 in the batch must be a name or"),
            ({"groups": ["Research", {"prefix": "QA"}]}, 'Group #2 in the batch needs a "name"'),
            ({"groups": [{"name": "Quality", "prefix": 7}]}, "Group 'Quality': \"prefix\" must be a string"),
            ({"tasks": [ok, "Just a title"]}, "Task #2 in the batch must be an object"),
            ({"tasks": [{"title": 5}]}, 'Task #1 in the batch: "title" must be a string'),
            ({"tasks": [ok, {"group": "Development"}]}, "Task #2 in the batch has no title"),
            ({"tasks": [{"title": "Seven", "desc": 7}]}, "Task 'Seven': \"desc\" must be a string"),
            ({"tasks": [{"title": "Listed", "description": ["a"]}]}, "Task 'Listed': \"description\" must be a string"),
            ({"tasks": [{"title": "Object", "group": {"x": 1}}]}, "Task 'Object': \"group\" must be a string"),
            ({"tasks": [ok, {"title": "Missing", "group": "Nope"}]}, "Task 'Missing': Group 'Nope' not found"),
            ({"tasks": [{"title": "Number", "deps": 3}]}, "Task 'Number': \"deps\" must be a task ID or title"),
            ({"tasks": [{"title": "Mixed", "depends_on": ["DV-02", 3]}]}, "Task 'Mixed': \"depends_on\" must be a task ID or title"),
        ]
        for spec, message in cases:
            with self.subTest(spec=spec):
                code, out, err = self.run_cli("batch", stdin=json.dumps(spec).encode("utf-8"), check=False)
                self.assertEqual(code, 1, out)
                self.assertRegex(err, r"\AError: [^\n]+\n\Z")
                self.assertIn(message, err)
                self.assertEqual(self.raw(), before)

    def test_batch_of_one_task_says_task(self):
        _, out, _ = self.run_cli("batch", stdin=json.dumps([{"title": "Only one", "deps": "DV-02", "desc": None}]).encode())
        self.assertIn("Proposed 1 task. A human must approve it before work starts.", out)
        self.assertEqual((self.get("G-01")["deps"], self.get("G-01")["desc"]), (["DV-02"], ""))

    def test_more_than_99_tasks(self):
        spec = [{"title": f"Step {i}", "group": "Development"} for i in range(1, 101)]
        self.run_cli("batch", stdin=json.dumps(spec).encode("utf-8"))
        ids = [t["id"] for t in self.board()["tasks"]]
        self.assertIn("DV-99", ids)
        self.assertIn("DV-104", ids)
        self.assertEqual(len(set(ids)), len(ids))
        self.run_cli("check")


class RevisionTests(TempBoard):
    """Agents may rework proposals until a human approves them."""

    def initial_board(self):
        board = super().initial_board()
        board["groups"].append({"id": "QA", "name": "Quality"})
        board["tasks"] += [task("DV-05", "proposed", "DV", ["DV-04"]), task("DV-06", "proposed", "DV", ["DV-04", "DV-02"])]
        return board

    def test_edit_revises_a_proposal(self):
        _, out, _ = self.run_cli("edit", "DV-04", "--title", "  Better   title ", "--group", "QA",
                                 "--stdin", "-m", "Narrowed after the discussion", "--as", "codex",
                                 stdin="Finished when it works.\r\n".encode("utf-8"))
        self.assertIn("Revised DV-04: title, group and description, with a note", out)
        t = self.get("DV-04")
        self.assertEqual((t["id"], t["title"], t["group"], t["desc"], t["state"]),
                         ("DV-04", "Better title", "QA", "Finished when it works.", "proposed"))
        self.assertEqual((t["notes"][-1]["by"], t["notes"][-1]["text"]), ("codex", "Narrowed after the discussion"))
        self.run_cli("edit", "DV-04", "--desc", "", "--group", "-")
        self.assertEqual((self.get("DV-04")["desc"], self.get("DV-04")["group"]), ("", None))
        _, out, _ = self.run_cli("edit", "DV-04", "--title", "Better title")
        self.assertIn("nothing", out)
        self.run_cli("check")

    def test_edit_refuses_approved_tasks_and_bad_input(self):
        before = self.raw()
        self.assertIn("only a human can change it", self.fails("edit", "DV-02", "--title", "x"))
        self.assertIn("Nothing to change", self.fails("edit", "DV-04"))
        self.assertIn("not found", self.fails("edit", "DV-04", "--group", "Nope"))
        self.assertIn("title is empty", self.fails("edit", "DV-04", "--title", "  "))
        self.assertIn("unrecognized arguments", self.fails("edit", "DV-04", "stray text"))
        self.assertEqual(self.raw(), before)

    def test_withdraw_removes_proposals_and_their_links(self):
        _, out, _ = self.run_cli("withdraw", "DV-04", "dv-05")
        self.assertIn("Withdrew DV-04, DV-05; DV-06 no longer depend on them", out)
        ids = [t["id"] for t in self.board()["tasks"]]
        self.assertNotIn("DV-04", ids)
        self.assertNotIn("DV-05", ids)
        self.assertEqual(self.get("DV-06")["deps"], ["DV-02"])
        self.run_cli("check")

    def test_withdraw_keeps_what_approved_tasks_depend_on(self):
        self.run_cli("depend", "DV-03", "DV-06")
        before = self.raw()
        self.assertIn("DV-03 is approved and depends on DV-06", self.fails("withdraw", "DV-06"))
        self.assertIn("only a human can change it", self.fails("withdraw", "DV-04", "DV-02"))
        self.assertEqual(self.raw(), before)

    def test_undepend_rewires_a_proposal(self):
        _, out, _ = self.run_cli("undepend", "DV-06", "dv-04")
        self.assertIn("DV-06 no longer depends on DV-04", out)
        self.assertEqual(self.get("DV-06")["deps"], ["DV-02"])
        before = self.raw()
        self.assertIn("does not depend on DV-05", self.fails("undepend", "DV-06", "DV-05"))
        self.assertIn("only a human can change it", self.fails("undepend", "DV-03", "DV-02"))
        self.assertEqual(self.raw(), before)

    def test_proposals_in_a_yolo_chain_stay_proposals(self):
        board = self.board()
        dv3 = kanvas.find_task(board, "DV-03")
        dv3["yolo"], dv3["deps"] = True, ["DV-02", "DV-05"]
        write_board(self.path, board)
        self.run_cli("edit", "DV-05", "--title", "Revised in the chain")
        self.run_cli("undepend", "DV-05", "DV-04")
        before = self.raw()
        self.assertIn("DV-03 is approved and depends on DV-05", self.fails("withdraw", "DV-05"))
        self.assertIn("a human must approve it", self.fails("start", "DV-05"))
        self.assertEqual(self.raw(), before)


class InitTests(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="kanvas-init-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def init(self, *args, cwd=None):
        """Runs `init` on self.dir, or with `args` from `cwd` when either is given."""
        if not args and cwd is None:
            args = (self.dir,)
        proc = subprocess.run([sys.executable, SCRIPT, "init", *args], cwd=cwd, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        return proc.stdout.decode("utf-8")

    def folder(self, *parts):
        path = os.path.join(self.dir, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def test_init_refreshes_skill_copies_from_older_versions(self):
        old = os.path.join(self.dir, ".cursor", "skills", "kanvas", "SKILL.md")
        os.makedirs(os.path.dirname(old))
        with open(old, "w", encoding="utf-8") as f:
            f.write("old skill\n")
        self.init()
        with open(old, encoding="utf-8") as f, open(os.path.join(ROOT, "skill", "SKILL.md"), encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read())
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".gemini")))

    def test_init_installs_the_skill_and_never_overwrites(self):
        mine = "# My agent instructions\n"
        for name in ("AGENTS.md", "CLAUDE.md"):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(mine)
        os.makedirs(os.path.join(self.dir, ".cursor"))
        out = self.init()
        self.assertIn("created   kanvas.json", out)
        self.assertNotIn("created   AGENTS.md", out)
        self.assertIn("add this line to AGENTS.md or CLAUDE.md", out)
        for rel in (".agents/skills/kanvas/kanvas.py", ".agents/skills/kanvas/kanvas.html",
                    ".agents/skills/kanvas/SKILL.md", ".claude/skills/kanvas/SKILL.md"):
            self.assertTrue(os.path.isfile(os.path.join(self.dir, rel)), rel)
        self.assertEqual(os.listdir(os.path.join(self.dir, ".cursor")), [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".gemini")))
        for name in ("AGENTS.md", "CLAUDE.md"):
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                self.assertEqual(f.read(), mine)

        board = os.path.join(self.dir, "kanvas.json")
        data = kanvas.load_board(board)
        data["tasks"].append(task("G-01"))
        write_board(board, data)
        self.assertIn("kept      kanvas.json", self.init())
        self.assertEqual(len(kanvas.load_board(board)["tasks"]), 1)

        installed = os.path.join(self.dir, ".agents", "skills", "kanvas", "kanvas.py")
        proc = subprocess.run([sys.executable, installed, "status"], cwd=self.dir, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_init_in_a_git_subfolder_sets_up_the_top_folder(self):
        os.mkdir(os.path.join(self.dir, ".git"))
        sub = self.folder("src", "deep")
        out = self.init(cwd=sub)
        self.assertEqual(out.splitlines()[0], f"Factory {kanvas.VERSION} in {self.dir}, the top folder of this git repository")
        self.assertIn("created   kanvas.json", out)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "kanvas.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, ".agents", "skills", "kanvas", "SKILL.md")))
        self.assertEqual(os.listdir(sub), [])

    def test_a_git_file_marks_the_top_folder_too(self):
        """Worktrees and submodules have a .git file."""
        with open(os.path.join(self.dir, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/project\n")
        out = self.init(cwd=self.folder("src"))
        self.assertIn("the top folder of this git repository", out.splitlines()[0])
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "kanvas.json")))

    def test_init_dot_keeps_the_current_folder(self):
        os.mkdir(os.path.join(self.dir, ".git"))
        sub = self.folder("docs")
        out = self.init(".", cwd=sub)
        self.assertEqual(out.splitlines()[0], f"Factory {kanvas.VERSION} in {sub}")
        self.assertTrue(os.path.isfile(os.path.join(sub, "kanvas.json")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "kanvas.json")))

    def test_an_existing_board_wins_over_the_git_top_folder(self):
        os.mkdir(os.path.join(self.dir, ".git"))
        app = self.folder("app")
        write_board(os.path.join(app, "kanvas.json"), kanvas.new_board("app"))
        out = self.init(cwd=self.folder("app", "src"))
        self.assertEqual(out.splitlines()[0], f"Factory {kanvas.VERSION} in {app}, the folder of the existing kanvas.json")
        self.assertIn("kept      kanvas.json", out)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "kanvas.json")))
        self.assertEqual(os.listdir(os.path.join(app, "src")), [])

    def test_outside_git_init_sets_up_the_current_folder(self):
        if kanvas.find_git_top(self.dir) or kanvas.find_board(self.dir):
            self.skipTest("the temporary folder is inside a git repository or below a kanvas.json")
        sub = self.folder("notes")
        out = self.init(cwd=sub)
        self.assertEqual(out.splitlines()[0], f"Factory {kanvas.VERSION} in {sub}")
        self.assertTrue(os.path.isfile(os.path.join(sub, "kanvas.json")))
        self.assertEqual(os.listdir(self.dir), ["notes"])

    def test_init_creates_agents_md_when_missing(self):
        out = self.init()
        self.assertIn("created   AGENTS.md", out)
        self.assertNotIn("add this line", out)
        with open(os.path.join(self.dir, "AGENTS.md"), "rb") as f:
            self.assertEqual(f.read(), b"# Agent instructions\n\n"
                                       b"Plan and track work on the Factory board; follow .agents/skills/kanvas/SKILL.md.\n")
        out = self.init()
        self.assertNotIn("AGENTS.md", out)

    def test_a_refused_skill_folder_leaves_the_project_untouched(self):
        """Agent sandboxes such as Codex's keep .agents/ read-only."""
        with open(os.path.join(self.dir, ".agents"), "w", encoding="utf-8") as f:
            f.write("not a folder")
        proc = subprocess.run([sys.executable, SCRIPT, "init", self.dir], capture_output=True)
        err = proc.stderr.decode("utf-8")
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn("Could not write .agents", err)
        self.assertIn("init` outside the sandbox", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(os.listdir(self.dir), [".agents"])

    def test_existing_agent_instructions_stay_byte_identical(self):
        mine = "# Mine\r\nNo newline at the end é".encode("utf-8")
        for name, creates in (("AGENTS.md", False), ("agents.md", False), ("CLAUDE.md", True)):
            with self.subTest(name=name):
                project = tempfile.mkdtemp(dir=self.dir)
                with open(os.path.join(project, name), "wb") as f:
                    f.write(mine)
                out = self.init(project)
                with open(os.path.join(project, name), "rb") as f:
                    self.assertEqual(f.read(), mine)
                self.assertEqual("created   AGENTS.md" in out, creates)
                self.assertEqual("add this line to AGENTS.md" in out, not creates)
                docs = sorted(f for f in os.listdir(project) if f.lower().endswith(".md"))
                self.assertEqual(docs, sorted([name, "AGENTS.md"] if creates else [name]))

    def test_skill_has_frontmatter(self):
        with open(os.path.join(ROOT, "skill", "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("---\nname: kanvas\ndescription: "), text[:80])

    def test_version_file_matches_the_script(self):
        """The package takes its version from kanvas.py; the VERSION file must agree."""
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), kanvas.VERSION)


class InstalledCommandTests(TempBoard):
    """Messages name the command the way it was typed, and the installed command checks the project's agent copy."""

    def running(self, installed=False, on_path=False, argv0="kanvas.py"):
        """Installed with or without `kanvas` on PATH (`uvx kanvas-board`), or run as a script from `argv0`."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(kanvas, "INSTALLED", installed))
        stack.enter_context(mock.patch("shutil.which", return_value="/usr/local/bin/kanvas" if on_path else None))
        stack.enter_context(mock.patch.object(sys, "argv", [argv0]))
        return stack

    def write_copy(self, text):
        skill = os.path.join(self.dir, ".agents", "skills", "kanvas")
        os.makedirs(skill, exist_ok=True)
        with open(os.path.join(skill, "kanvas.py"), "w", encoding="utf-8") as f:
            f.write(f'"""Factory."""\n\n{text}\nFORMAT = 1\n')

    def hint(self, copy_version, version="0.5.0", **mode):
        self.write_copy(f'VERSION = "{copy_version}"')
        with self.running(**mode), mock.patch.object(kanvas, "VERSION", version):
            return kanvas.agent_copy_hint(self.path)

    def status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kanvas.cmd_status(argparse.Namespace(board=self.path))
        return out.getvalue()

    def test_command_line_and_project_command(self):
        with self.running(installed=True, on_path=True):
            self.assertEqual(kanvas.command_line("init"), "kanvas init")
            self.assertEqual(kanvas.command_line(), "kanvas")
            self.assertEqual(kanvas.project_command("serve"), "kanvas serve")
        with self.running(installed=True):
            self.assertEqual(kanvas.command_line("init"), "uvx kanvas-board init")
            self.assertEqual(kanvas.command_line(), "uvx kanvas-board")
            self.assertEqual(kanvas.project_command("serve"), "python .agents/skills/kanvas/kanvas.py serve")
        with self.running(on_path=True, argv0=".agents/skills/kanvas/kanvas.py"):
            self.assertEqual(kanvas.command_line("status"), "python .agents/skills/kanvas/kanvas.py status")
            self.assertEqual(kanvas.project_command(), "python .agents/skills/kanvas/kanvas.py")
        with self.running(argv0="/home/me/My Tools/kanvas.py"):
            self.assertEqual(kanvas.command_line("init"), 'python "/home/me/My Tools/kanvas.py" init')
            self.assertEqual(kanvas.command_line(), 'python "/home/me/My Tools/kanvas.py"')

    def test_the_kanvas_of_a_uvx_run_does_not_count(self):
        """uvx puts its temporary environment on PATH and sets UV; that `kanvas` is gone afterwards."""
        temporary = os.path.join(sys.prefix, "Scripts" if os.name == "nt" else "bin", "kanvas")
        for found, uv, expected in ((temporary, "/usr/bin/uv", "uvx kanvas-board init"),
                                    (temporary, None, "kanvas init"),
                                    ("/usr/local/bin/kanvas", "/usr/bin/uv", "kanvas init")):
            with self.subTest(found=found, uv=uv), mock.patch.object(kanvas, "INSTALLED", True), \
                    mock.patch("shutil.which", return_value=found), \
                    mock.patch.dict(os.environ, {"UV": uv} if uv else {}, clear=False):
                if not uv:
                    os.environ.pop("UV", None)
                self.assertEqual(kanvas.command_line("init"), expected)

    def test_help_names_the_program_as_typed(self):
        with self.running(installed=True):
            text = kanvas.build_parser().format_help()
        self.assertTrue(text.startswith("usage: kanvas [-h]"), text[:40])
        self.assertIn("\n  kanvas status\n", text)
        with self.running(argv0=os.path.join("tools", "kanvas.py")):
            text = kanvas.build_parser().format_help()
        self.assertTrue(text.startswith("usage: kanvas.py [-h]"), text[:40])
        self.assertIn("\n  kanvas.py start DV-02 -m \"Plan: API, data, tests and rollout\" --worktree .\n", text)
        with self.running(argv0=""):
            self.assertTrue(kanvas.build_parser().format_help().startswith("usage: kanvas.py [-h]"))

    def test_serve_help_says_which_ports_it_tries(self):
        with mock.patch.object(sys, "stdout", io.StringIO()) as out, self.assertRaises(SystemExit):
            kanvas.main(["serve", "-h"])
        text = " ".join(out.getvalue().split())
        self.assertIn("--port PORT port to serve on (default: 8765), or the next free one up to 19 higher (8784 by default);", text)
        self.assertIn("when a Factory already runs on any of these ports, serve opens the board there and returns", text)

    def test_missing_board_names_the_init_command(self):
        nowhere = argparse.Namespace()
        with mock.patch.object(kanvas, "find_board", return_value=None), mock.patch.dict(os.environ):
            os.environ.pop("KANVAS_BOARD", None)
            with self.running(installed=True, on_path=True):
                with self.assertRaisesRegex(kanvas.KanvasError, "Create one with `kanvas init`, or pass --board PATH"):
                    kanvas.board_path(nowhere)
            with self.running(installed=True):
                with self.assertRaisesRegex(kanvas.KanvasError, "`uvx kanvas-board init`"):
                    kanvas.board_path(nowhere)
            with self.running(argv0="kanvas.py"):
                with self.assertRaisesRegex(kanvas.KanvasError, "`python kanvas.py init`"):
                    kanvas.board_path(nowhere)

    def test_other_messages_name_the_command(self):
        args = argparse.Namespace(board=self.path, id="DV-02", message=None, stdin=False, file=None)
        with self.running(installed=True, on_path=True):
            with self.assertRaisesRegex(kanvas.KanvasError, 'e.g. `kanvas finish DV-02 -m "Added X; tests pass"`'):
                kanvas.cmd_finish(args)
            with self.assertRaisesRegex(kanvas.KanvasError, "upgrade with `uv tool upgrade kanvas-board`"):
                kanvas.parse_board('{"kanvas": 99}')
        with self.running(argv0="kanvas.py"):
            with self.assertRaisesRegex(kanvas.KanvasError, "`python kanvas.py finish DV-02 -m"):
                kanvas.cmd_finish(args)

    def test_older_agent_copy(self):
        self.assertEqual(self.hint("0.4.0", installed=True, on_path=True),
                         "This project's agent copy is 0.4.0; run `kanvas init` to update it to 0.5.0.")
        self.assertEqual(self.hint("0.9.0", "0.10.0", installed=True),
                         "This project's agent copy is 0.9.0; run `uvx kanvas-board init` to update it to 0.10.0.")

    def test_newer_agent_copy(self):
        self.assertEqual(self.hint("0.5.0", "0.4.0", installed=True, on_path=True),
                         "This project's agent copy is 0.5.0, newer than your kanvas 0.4.0; "
                         "update it with `uv tool upgrade kanvas-board`.")

    def test_test_versions_come_before_their_release(self):
        order = ["0.4.0a1", "0.4.0b2", "0.4.0rc1", "0.4.0rc2", "0.4", "0.4.1", "0.10.0rc1", "0.10.0"]
        self.assertEqual(sorted(order, key=kanvas._version_key), order)
        self.assertIn("update it to 0.4.0.", self.hint("0.4.0rc1", "0.4.0", installed=True, on_path=True))
        self.assertIn("update it to 0.4.0rc2.", self.hint("0.4.0rc1", "0.4.0rc2", installed=True, on_path=True))
        self.assertIn("newer than your kanvas 0.4.0rc1", self.hint("0.4.0", "0.4.0rc1", installed=True, on_path=True))

    def test_no_hint_when_versions_match_or_cannot_be_read(self):
        self.assertIsNone(self.hint("0.5.0", installed=True, on_path=True))
        self.assertIsNone(self.hint("0.5", installed=True, on_path=True))
        self.assertIsNone(self.hint("0.4.0", "0.5.0.dev1", installed=True, on_path=True))
        self.write_copy("# no version here")
        with self.running(installed=True, on_path=True):
            self.assertIsNone(kanvas.agent_copy_hint(self.path))
        shutil.rmtree(os.path.join(self.dir, ".agents"))
        with self.running(installed=True, on_path=True):
            self.assertIsNone(kanvas.agent_copy_hint(self.path))

    def test_no_hint_when_run_as_a_script(self):
        self.assertIsNone(self.hint("0.4.0", on_path=True))

    def test_status_ends_with_the_hint(self):
        self.write_copy('VERSION = "0.0.1"')
        with self.running(installed=True, on_path=True):
            out = self.status()
        self.assertTrue(out.endswith(f"\n\nThis project's agent copy is 0.0.1; run `kanvas init` to update it to "
                                     f"{kanvas.VERSION}.\n"), out)
        with self.running():
            self.assertNotIn("agent copy", self.status())


LEGACY_CANVAS = {
    "nodes": [
        {"id": "g-res", "type": "group", "x": 0, "y": 0, "width": 400, "height": 600, "label": "Research"},
        {"id": "g-dev", "type": "group", "x": 500, "y": 0, "width": 400, "height": 600, "label": "Development"},
        {"id": "legend", "type": "text", "text": "## Legend\nRed = To Do", "x": -500, "y": 0, "width": 300, "height": 200, "color": "0"},
        {"id": "n1", "type": "text", "text": "## RS-01 Read papers\nFind prior work", "x": 20, "y": 20, "width": 300, "height": 120, "color": "4"},
        {"id": "n2", "type": "text", "text": "## RS-02 Summarise", "x": 20, "y": 200, "width": 300, "height": 120, "color": "0"},
        {"id": "n3", "type": "text", "text": "## DV-01 Build it\nLine one\nLine two", "x": 520, "y": 20, "width": 300, "height": 120, "color": "2"},
        {"id": "n4", "type": "text", "text": "## Untitled idea\nSomething new", "x": 520, "y": 200, "width": 300, "height": 120, "color": "6"},
        {"id": "n5", "type": "text", "text": "## DV-01 Duplicate id", "x": 520, "y": 400, "width": 300, "height": 120, "color": "5"},
        {"id": "note", "type": "text", "text": "free text without a heading", "x": 0, "y": 900, "width": 300, "height": 120},
    ],
    "edges": [
        {"id": "e1", "fromNode": "n1", "toNode": "n2"},
        {"id": "e2", "fromNode": "n2", "toNode": "n3"},
        {"id": "e3", "fromNode": "n3", "toNode": "n4"},
    ],
}


class ImportExportTests(TempBoard):
    def initial_board(self):
        return kanvas.new_board("empty")

    def test_import_legacy_canvas(self):
        src = os.path.join(self.dir, "Project.canvas")
        with open(src, "w", encoding="utf-8") as f:
            json.dump(LEGACY_CANVAS, f)
        self.run_cli("import", "Project.canvas")
        board = self.board()
        self.assertEqual(board["title"], "Project")
        self.assertEqual(board["groups"], [{"id": "RS", "name": "Research"}, {"id": "DV", "name": "Development"}])
        by_title = {t["title"]: t for t in board["tasks"]}
        self.assertEqual(set(by_title), {"Read papers", "Summarise", "Build it", "Untitled idea", "Duplicate id"})
        self.assertEqual(by_title["Read papers"]["state"], "done")
        self.assertEqual(by_title["Summarise"]["state"], "todo")
        self.assertEqual(by_title["Build it"]["state"], "doing")
        self.assertEqual(by_title["Build it"]["desc"], "Line one\nLine two")
        self.assertEqual(by_title["Untitled idea"]["state"], "proposed")
        self.assertEqual(by_title["Duplicate id"]["state"], "review")
        self.assertEqual(by_title["Summarise"]["deps"], ["RS-01"])
        self.assertEqual(by_title["Untitled idea"]["deps"], [by_title["Build it"]["id"]])
        ids = [t["id"] for t in board["tasks"]]
        self.assertEqual(len(set(ids)), 5)
        self.assertIn("DV-01", ids)
        self.run_cli("check")
        with open(src, encoding="utf-8") as f:
            self.assertEqual(json.load(f), LEGACY_CANVAS)
        self.assertIn("--force", self.fails("import", "Project.canvas"))
        self.run_cli("import", "Project.canvas", "--force")

    def test_export_snapshot(self):
        board = self.board()
        board["tasks"].append(task("G-01", desc="</script><script>alert(1)</script>"))
        write_board(self.path, board)
        self.run_cli("export", "snap.html")
        with open(os.path.join(self.dir, "snap.html"), encoding="utf-8") as f:
            html = f.read()
        m = re.search(r'<script id="kanvas-snapshot" type="application/json" data-at="[^"]+" data-name="([^"]+)">(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), kanvas.board_name(self.path))
        self.assertNotIn("<", m.group(2))
        self.assertEqual(json.loads(m.group(2))["tasks"][0]["desc"], "</script><script>alert(1)</script>")

    def test_an_untitled_board_goes_by_its_name_in_the_page(self):
        """As on the boards page: the name in its address when served, the one export recorded, else "Factory"."""
        code = ("globalThis.location = { pathname: '/my-project/' };\n"
                "let board, store;\n"
                "process.stdout.write(JSON.stringify(input.map(([title, kind, boardName]) => {\n"
                "  board = { title }; store = kind ? { kind, boardName } : null;\n"
                "  return boardLabel();\n"
                "})));")
        cases = [["Mine", "server", None], ["", "server", None], ["", "snapshot", "exported"], ["", "file", None], ["", None, None]]
        out = run_html_js(self, code, json.dumps(cases), [("const boardHere =", "\nasync function refreshBoards")])
        self.assertEqual(json.loads(out), ["Mine", "my-project", "exported", "Factory", "Factory"])


class ConcurrencyTests(TempBoard):
    def test_parallel_agents_do_not_lose_writes(self):
        workers, per_worker = 6, 4
        script = (
            "import subprocess, sys\n"
            "for i in range(%d):\n"
            "    subprocess.run([sys.executable, %r, 'note', 'DV-02', '--as', sys.argv[1], str(i)], check=True, stdout=subprocess.DEVNULL)\n"
        ) % (per_worker, SCRIPT)
        procs = [subprocess.Popen([sys.executable, "-c", script, f"w{n}"], cwd=self.dir) for n in range(workers)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        self.assertEqual(len(self.get("DV-02")["notes"]), workers * per_worker)
        leftovers = [f for f in os.listdir(self.dir) if f.endswith((".lock", ".tmp"))]
        self.assertEqual(leftovers, [])

    def test_stale_lock_is_cleared(self):
        lock = self.path + ".lock"
        open(lock, "w").close()
        old = time.time() - 60
        os.utime(lock, (old, old))
        self.run_cli("note", "DV-02", "after a crash")
        self.assertFalse(os.path.exists(lock))

    def test_writers_outwait_a_lock_left_by_a_crash(self):
        self.assertLess(kanvas.STALE_LOCK_AGE, kanvas.LOCK_TIMEOUT)
        lock = self.path + ".lock"
        open(lock, "w").close()
        with mock.patch.object(kanvas, "STALE_LOCK_AGE", 0.3), mock.patch.object(kanvas, "LOCK_TIMEOUT", 0.6):
            kanvas.mutate(self.path, lambda board: kanvas.add_note(kanvas.find_task(board, "DV-02"), "after a crash", "codex"))
        self.assertFalse(os.path.exists(lock))
        self.assertEqual(self.get("DV-02")["notes"][-1]["text"], "after a crash")

    def test_busy_lock_gives_up_at_the_deadline(self):
        open(self.path + ".lock", "w").close()
        with mock.patch.object(kanvas, "LOCK_TIMEOUT", 0.2):
            with self.assertRaisesRegex(kanvas.KanvasError, "Board is busy"):
                kanvas.mutate(self.path, lambda board: None)

    def test_unwritable_folder_is_reported(self):
        started = time.monotonic()
        with mock.patch.object(kanvas, "LOCK_TIMEOUT", 0.2), mock.patch("os.open", side_effect=PermissionError):
            with self.assertRaisesRegex(kanvas.KanvasError, "permission denied"):
                kanvas.mutate(self.path, lambda board: None)
        self.assertLess(time.monotonic() - started, 5)


class ServerTests(TempBoard):
    def setUp(self):
        super().setUp()
        self.server = kanvas.start_server(self.path, "127.0.0.1", 0, quiet=True)
        self.port = self.server.server_address[1]
        self.name = kanvas.board_name(self.path)
        self.api = f"/{self.name}/api/board"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def put(self, board, etag=None, ctype="application/json"):
        headers = {"Content-Type": ctype}
        if etag:
            headers["If-Match"] = etag
        return self.request("PUT", self.api, json.dumps(board).encode("utf-8"), headers)

    def test_the_mockup_of_a_design_is_served_with_a_policy_that_gives_it_no_origin_and_no_network(self):
        page = "<!doctype html><html><body><p id=x>hi</p><script>document.getElementById('x')</script></body></html>"
        board = self.board()
        kanvas.find_task(board, "DV-02")["work"] = {"design": {"status": "pending", "text": "words", "mockup": page}}
        kanvas.find_task(board, "DV-03")["work"] = {"design": {"status": "pending", "text": "words only"}}
        write_board(self.path, board)
        status, headers, body = self.request("GET", f"/{self.name}/api/design/DV-02")
        self.assertEqual((status, body.decode("utf-8")), (200, page))
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        policy = headers["Content-Security-Policy"]
        for part in ("sandbox allow-scripts", "default-src 'none'", "img-src data:"):
            self.assertIn(part, policy)
        self.assertNotIn("allow-same-origin", policy)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.request("GET", f"/{self.name}/api/design/DV-03")[0], 404)
        older = "<!doctype html><html><body>older</body></html>"
        board = self.board()
        kanvas.find_task(board, "DV-02")["work"]["design"].update(round=2, history=[
            {"type": "proposal", "round": 1, "at": "t", "by": "designer", "text": "v1", "mockup": older},
            {"type": "proposal", "round": 2, "at": "t", "by": "designer", "text": "v2", "mockup": page}])
        write_board(self.path, board)
        self.assertEqual(self.request("GET", f"/{self.name}/api/design/DV-02?round=1")[2].decode("utf-8"), older)
        self.assertEqual(self.request("GET", f"/{self.name}/api/design/DV-02?round=2")[2].decode("utf-8"), page)
        self.assertEqual(self.request("GET", f"/{self.name}/api/design/DV-02")[2].decode("utf-8"), page)
        self.assertEqual(self.request("GET", f"/{self.name}/api/design/DV-02?round=9")[0], 404)
        self.assertEqual(self.request("GET", f"/{self.name}/api/design/NOPE-1")[0], 404)

    def inbox_request(self, method, path, body=None, headers=None):
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        return self.request(method, f"/{self.name}/api/inbox{path}", payload, {"Content-Type": "application/json", **(headers or {})})

    def test_the_inbox_page_and_api_answer_this_computer(self):
        status, headers, body = self.request("GET", f"/{self.name}/inbox")
        self.assertEqual(status, 200)
        self.assertIn(b"An idea, or an incident", body)
        fake = {"reply": "ok", "ready": False, "kind": "idea", "questions": [], "duplicates": [], "task": ""}
        with mock.patch.object(kanvas, "inbox_po_turn", side_effect=lambda path, tid: {**fake, "task": tid}), \
             mock.patch.object(kanvas.shutil, "which", return_value="claude"):
            status, _, raw = self.inbox_request("POST", "", {"text": "Una idea", "source": "web"})
            self.assertEqual(status, 202)
            job = json.loads(raw)
            for _ in range(50):
                _, _, polled = self.inbox_request("GET", f"/job/{job['job']}")
                if json.loads(polled)["status"] != "running":
                    break
                time.sleep(0.1)
        self.assertEqual(json.loads(polled)["status"], "done")
        listed = json.loads(self.inbox_request("GET", "")[2])["items"]
        self.assertEqual([i["id"] for i in listed], [job["task"]])
        self.assertEqual(listed[0]["thread"][0]["text"], "Una idea")
        self.assertEqual(self.inbox_request("POST", f"/{job['task']}/drop", {})[0], 200)
        self.assertEqual(json.loads(self.inbox_request("GET", "")[2])["items"], [])

    def test_a_phone_needs_the_paired_token_to_use_the_inbox(self):
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {"KANVAS_STATE_DIR": state}):
            phone = {"X-Forwarded-For": "100.64.0.9"}
            self.assertEqual(self.inbox_request("GET", "", headers=phone)[0], 403)
            self.assertEqual(self.inbox_request("POST", "", {"text": "x"}, phone)[0], 403)
            token = kanvas.new_inbox_token()
            self.assertEqual(self.inbox_request("GET", "", headers={**phone, "X-Kanvas-Token": "wrong"})[0], 403)
            self.assertEqual(self.inbox_request("GET", "", headers={**phone, "X-Kanvas-Token": token})[0], 200)
            with open(kanvas.inbox_token_file(), encoding="utf-8") as f:
                self.assertNotIn(token, f.read())
            newer = kanvas.new_inbox_token()
            self.assertEqual(self.inbox_request("GET", "", headers={**phone, "X-Kanvas-Token": token})[0], 403)
            self.assertEqual(self.inbox_request("GET", "", headers={**phone, "X-Kanvas-Token": newer})[0], 200)

    def telegram_request(self, body=None, headers=None):
        method = "POST" if body is not None else "GET"
        return self.request(method, f"/{self.name}/api/telegram", json.dumps(body).encode("utf-8") if body is not None else None,
                            {"Content-Type": "application/json", **(headers or {})})

    def test_connecting_telegram_from_the_board_keeps_the_token_and_shows_a_pairing_code(self):
        token = "123456789:" + "A" * 30

        def fake_api(given):
            self.assertEqual(given, token)
            return lambda method, params=None, timeout=40: {"username": "mybot"}

        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {"KANVAS_STATE_DIR": state, "KANVAS_TELEGRAM_TOKEN": "",
                                                                                   "KANVAS_TELEGRAM_USERS": ""}), \
             mock.patch.object(kanvas, "telegram_api", side_effect=fake_api), mock.patch.object(kanvas.TelegramBridge, "run"):
            self.assertEqual(json.loads(self.telegram_request()[2])["connected"], False)
            self.assertEqual(self.telegram_request({"action": "connect", "token": "nope"})[0], 400)
            status, _, raw = self.telegram_request({"action": "connect", "token": token})
            info = json.loads(raw)
            self.assertEqual(status, 200)
            self.assertEqual((info["connected"], info["bot"], info["users"], info["running"]), (True, "mybot", 0, True))
            self.assertRegex(info["code"], r"^\d{6}$")
            self.assertNotIn(token, raw.decode("utf-8"))
            saved = kanvas.telegram_config()
            self.assertEqual((saved["token"], saved["bot"], saved["users"], saved["board"]), (token, "mybot", [], self.path))
            self.assertEqual(saved["pairing"]["code"], info["code"])
            again = json.loads(self.telegram_request({"action": "pair"})[2])
            self.assertTrue(again["code"])
            self.assertEqual(json.loads(self.telegram_request({"action": "disconnect"})[2])["connected"], False)
            self.assertFalse(os.path.exists(kanvas.telegram_file()))
            self.assertEqual(self.telegram_request({"action": "dance"})[0], 400)

    def test_only_this_computer_may_connect_telegram(self):
        phone = {"X-Forwarded-For": "100.64.0.9"}
        self.assertEqual(self.telegram_request(headers=phone)[0], 403)
        self.assertEqual(self.telegram_request({"action": "disconnect"}, phone)[0], 403)

    def test_a_token_telegram_refuses_is_reported_without_saving_it(self):
        token = "123456789:" + "B" * 30

        def refusing(given):
            def call(method, params=None, timeout=40):
                raise kanvas.TelegramError("Unauthorized")
            return call

        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {"KANVAS_STATE_DIR": state, "KANVAS_TELEGRAM_TOKEN": "",
                                                                                   "KANVAS_TELEGRAM_USERS": ""}), \
             mock.patch.object(kanvas, "telegram_api", side_effect=refusing):
            status, _, raw = self.telegram_request({"action": "connect", "token": token})
            self.assertEqual(status, 400)
            self.assertIn("Unauthorized", raw.decode("utf-8"))
            self.assertFalse(os.path.exists(kanvas.telegram_file()))

    def test_starting_skips_the_host_name_lookup(self):
        """socket.getfqdn, which HTTPServer calls on start, can take half a minute on macOS."""
        with mock.patch("socket.getfqdn", side_effect=AssertionError("getfqdn was called")):
            server = kanvas.start_server(self.path, "127.0.0.1", 0, quiet=True)
        server.server_close()
        self.assertEqual(server.server_name, "127.0.0.1")

    def test_a_busy_port_moves_kanvas_to_the_next_free_one(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            server = kanvas.start_server(self.path, "127.0.0.1", port, quiet=True)
        server.server_close()
        self.assertIn(server.server_address[1], range(port + 1, port + 20))

    def test_serves_the_page_and_the_board(self):
        status, headers, body = self.request("GET", f"/{self.name}/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Factory</title>", body)
        status, headers, body = self.request("GET", self.api)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.raw())
        status, _, body = self.request("GET", self.api, headers={"If-None-Match": headers["ETag"]})
        self.assertEqual((status, body), (304, b""))

    def put_evidence(self, files, task_id="DV-02", folder=os.path.join(".kanvas", "evidence")):
        base = os.path.join(self.dir, folder, task_id)
        for name, data in files.items():
            target = os.path.join(base, *name.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)

    def test_lists_and_serves_the_evidence_of_a_task(self):
        self.put_evidence({"01-start.png": b"\x89PNG-data", "run/video.webm": b"0123456789", "notes.txt": b"secret",
                           ".hidden.png": b"x"})
        status, _, body = self.request("GET", f"/{self.name}/api/evidence/DV-02")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["files"], [
            {"path": "01-start.png", "kind": "image", "size": 9},
            {"path": "run/video.webm", "kind": "video", "size": 10},
        ])
        status, headers, body = self.request("GET", f"/{self.name}/evidence/DV-02/01-start.png")
        self.assertEqual((status, headers["Content-Type"], body), (200, "image/png", b"\x89PNG-data"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        video = f"/{self.name}/evidence/DV-02/run/video.webm"
        status, headers, body = self.request("GET", video)
        self.assertEqual((status, headers["Content-Type"], headers["Accept-Ranges"], body), (200, "video/webm", "bytes", b"0123456789"))
        status, headers, body = self.request("GET", video, headers={"Range": "bytes=2-5"})
        self.assertEqual((status, body, headers["Content-Range"]), (206, b"2345", "bytes 2-5/10"))
        status, headers, body = self.request("GET", video, headers={"Range": "bytes=7-"})
        self.assertEqual((status, body, headers["Content-Range"]), (206, b"789", "bytes 7-9/10"))
        status, headers, body = self.request("GET", video, headers={"Range": "bytes=-4"})
        self.assertEqual((status, body, headers["Content-Range"]), (206, b"6789", "bytes 6-9/10"))
        status, headers, _ = self.request("GET", video, headers={"Range": "bytes=50-60"})
        self.assertEqual((status, headers["Content-Range"]), (416, "bytes */10"))

    def test_a_task_without_evidence_lists_nothing(self):
        for task_id in ("DV-02", "DV-99", ".."):
            with self.subTest(task_id=task_id):
                status, _, body = self.request("GET", f"/{self.name}/api/evidence/{task_id}")
                self.assertEqual((status, json.loads(body)), (200, {"files": []}))
        self.assertEqual(self.request("GET", f"/{self.name}/api/evidence/a%2Fb")[0], 404)

    def test_evidence_stays_inside_its_folder_and_is_only_screenshots_and_videos(self):
        self.put_evidence({"a.png": b"x", "notes.txt": b"secret", "page.html": b"<script>1</script>"})
        with open(os.path.join(self.dir, "outside.png"), "wb") as f:
            f.write(b"outside")
        base = f"/{self.name}/evidence"
        for path in ("DV-02/notes.txt", "DV-02/page.html", "DV-02/missing.png", "DV-99/a.png", "DV-02/", "DV-02",
                     "DV-02/../outside.png", "DV-02/%2e%2e/outside.png", "DV-02/..%2foutside.png",
                     "DV-02/..%5coutside.png", "../outside.png", "%2e%2e/DV-02/a.png", "DV-02/C:/a.png"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", f"{base}/{path}")[0], 404)
        self.assertEqual(self.request("GET", f"{base}/DV-02/a.png")[0], 200)

    def test_evidence_refuses_a_link_that_leaves_its_folder(self):
        outside = os.path.join(self.dir, "outside.png")
        with open(outside, "wb") as f:
            f.write(b"outside")
        self.put_evidence({"a.png": b"x"})
        link = os.path.join(self.dir, ".kanvas", "evidence", "DV-02", "link.png")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("this system does not allow symbolic links")
        self.assertEqual(self.request("GET", f"/{self.name}/evidence/DV-02/link.png")[0], 404)
        status, _, body = self.request("GET", f"/{self.name}/api/evidence/DV-02")
        self.assertEqual([f["path"] for f in json.loads(body)["files"]], ["a.png"])

    def test_evidence_follows_the_folder_the_board_names(self):
        board = self.board()
        board["automation"] = {"project_root": "work", "evidence_root": "shots"}
        write_board(self.path, board)
        self.put_evidence({"a.png": b"x"}, folder=os.path.join("work", "shots"))
        status, _, body = self.request("GET", f"/{self.name}/api/evidence/DV-02")
        self.assertEqual([f["path"] for f in json.loads(body)["files"]], ["a.png"])
        self.assertEqual(self.request("GET", f"/{self.name}/evidence/DV-02/a.png")[0], 200)

    def test_evidence_only_answers_to_this_computers_names(self):
        self.put_evidence({"a.png": b"x"})
        for path in (f"/{self.name}/api/evidence/DV-02", f"/{self.name}/evidence/DV-02/a.png"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, headers={"Host": "evil.example:8765"})[0], 403)

    def fake_agents(self, replies, gate=None):
        """Replaces the claude process of the agents' help: each call takes the next reply, which may be text, a
        (return code, stdout) pair or an exception. `gate`, an Event, holds every call until it is set."""
        self.agent_calls = []
        queue = list(replies)

        def run(command, **kwargs):
            self.agent_calls.append({"command": command, "prompt": kwargs.get("input"), "cwd": kwargs.get("cwd"),
                                     "env": kwargs.get("env")})
            if gate:
                gate.wait(10)
            reply = queue.pop(0)
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, tuple):
                return mock.Mock(returncode=reply[0], stdout=reply[1], stderr="")
            stdout = json.dumps({"type": "result", "result": reply, "total_cost_usd": 0.01, "num_turns": 2, "is_error": False})
            return mock.Mock(returncode=0, stdout=stdout, stderr="")
        return contextlib.ExitStack(), [mock.patch.object(kanvas.subprocess, "run", side_effect=run),
                                        mock.patch.object(kanvas.shutil, "which", return_value="claude")]

    def ask_agents(self, replies, kind, task_id=None, fields=None, gate=None, env=None, phase=None, answers=None):
        """Asks for the agents' help with their replies faked, and returns the finished job."""
        stack, patches = self.fake_agents(replies, gate)
        with stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(mock.patch.dict(os.environ, env or {}))
            request = {"kind": kind, "id": task_id, "fields": fields or {}, **({"phase": phase} if phase else {}),
                       **({"answers": answers} if answers is not None else {})}
            status, _, body = self.post_json("api/assist", request)
            self.assertEqual(status, 202, body)
            return self.finished_job(json.loads(body)["job"])

    def post_json(self, path, data, headers=None):
        return self.request("POST", f"/{self.name}/{path}", json.dumps(data).encode("utf-8"),
                            {"Content-Type": "application/json", **(headers or {})})

    def finished_job(self, job_id):
        deadline = time.monotonic() + 10
        while True:
            status, _, body = self.request("GET", f"/{self.name}/api/assist/{job_id}")
            job = json.loads(body)
            if status != 200 or job["status"] != "running" or time.monotonic() > deadline:
                return job
            time.sleep(0.05)

    @staticmethod
    def reply(prose, data):
        return f"{prose}\n\n```json\n{json.dumps(data)}\n```"

    def test_the_product_owner_drafts_a_new_task_read_only(self):
        draft = {"title": "Fix the crash on login", "group": "DV", "desc": "Login no longer crashes.", "duplicates": ["DV-02", "XX-9"],
                 "questions": ["Which phones?", 7]}
        job = self.ask_agents([self.reply("This is a bug.", draft)], "task", fields={"title": "app crashes on login"},
                              env={"GH_TOKEN": "secret", "KANVAS_GITHUB_TOKEN": "secret"})
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["draft"], {"title": "Fix the crash on login", "group": "DV", "desc": "Login no longer crashes."})
        self.assertEqual((job["result"]["duplicates"], [q["text"] for q in job["result"]["questions"]]), (["DV-02"], ["Which phones?"]))
        self.assertEqual([m["agent"] for m in job["messages"]], ["product-owner"])
        self.assertEqual(job["messages"][0]["text"], "This is a bug.")
        call = self.agent_calls[0]
        command = call["command"]
        self.assertEqual(command[command.index("--agent") + 1], "product-owner")
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertNotIn("Edit", command)
        self.assertNotIn("Bash", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("app crashes on login", call["prompt"])
        self.assertIn("- DV: Development", call["prompt"])
        self.assertIn("DV-02", call["prompt"])
        self.assertEqual(call["cwd"], self.dir)
        self.assertNotIn("GH_TOKEN", call["env"])
        self.assertNotIn("KANVAS_GITHUB_TOKEN", call["env"])

    def test_a_group_the_board_does_not_have_is_dropped_from_the_draft(self):
        job = self.ask_agents([self.reply("ok", {"title": "T", "group": "ZZ", "desc": ""})], "task")
        self.assertEqual(job["result"]["draft"]["group"], "")

    def test_the_product_owner_drafts_a_definition_with_only_what_the_board_has(self):
        repo = os.path.join(self.dir, "front")
        os.makedirs(repo)
        board = self.board()
        board["automation"] = {"enabled": True, "project_root": self.dir,
                               "repositories": [{"id": "front", "name": "Frontend", "path": repo, "base": "HEAD"}]}
        write_board(self.path, board)
        draft = {"outcome": "Clubs see X", "needs": "Per club", "acceptance": "Y works", "non_goals": "",
                 "repositories": ["front", "ghost"], "agent": "complex"}
        job = self.ask_agents([self.reply("Questions below.", {"questions": ["Which club?"], "draft": draft})], "definition",
                              "DV-04", {"outcome": "mine"})
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["draft"]["repositories"], ["front"])
        self.assertEqual(job["result"]["draft"]["agent"], "complex")
        self.assertEqual(job["result"]["questions"], ["Which club?"])
        prompt = self.agent_calls[0]["prompt"]
        self.assertIn("Task DV-04", prompt)
        self.assertIn("Outcome: mine", prompt)
        self.assertIn("front: Frontend", prompt)
        job = self.ask_agents([self.reply("x", {"draft": dict(draft, agent="reviewer")})], "definition", "DV-04")
        self.assertEqual(job["result"]["draft"]["agent"], "implementer")

    def test_the_product_owner_asks_before_drafting_and_always_asks_the_priority(self):
        asked = {"questions": [{"text": "Which club?", "options": ["All", "Voltrega", "", 3]}, "What about old data?", {"text": " "}, 9]}
        job = self.ask_agents([self.reply("Need a few answers.", asked)], "definition", "DV-04", phase="questions")
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["phase"], "questions")
        questions = job["result"]["questions"]
        self.assertEqual([q["id"] for q in questions], ["priority", "q1", "q2"])
        self.assertEqual(questions[0]["options"], ["Urgent", "High", "Normal", "Low"])
        self.assertEqual(questions[1], {"id": "q1", "text": "Which club?", "options": ["All", "Voltrega"]})
        self.assertEqual(questions[2]["options"], [])
        self.assertEqual(len(self.agent_calls), 1)
        self.assertIn("Do not draft the definition yet", self.agent_calls[0]["prompt"])
        job = self.ask_agents([self.reply("Nothing to ask.", {"questions": []})], "definition", "DV-04", phase="questions")
        self.assertEqual([q["id"] for q in job["result"]["questions"]], ["priority"])

    def test_the_answers_reach_the_definition_and_set_its_priority(self):
        draft = {"outcome": "O", "needs": "N", "acceptance": "A", "non_goals": "", "repositories": [], "agent": "implementer"}
        answers = [{"id": "priority", "q": "How urgent is this?", "a": "High"}, {"id": "q1", "q": "Which club?", "a": "Only Voltrega"},
                   {"id": "q2", "q": "Old data?", "a": "   "}]
        job = self.ask_agents([self.reply("Done.", {"questions": ["Push too?"], "draft": draft})], "definition", "DV-04",
                              phase="draft", answers=answers)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["draft"]["priority"], "high")
        self.assertEqual(job["result"]["questions"], ["Push too?"])
        prompt = self.agent_calls[0]["prompt"]
        self.assertIn("Which club?" + chr(10) + "  Answer: Only Voltrega", prompt)
        self.assertNotIn("Old data?", prompt)
        job = self.ask_agents([self.reply("Done.", {"draft": draft})], "definition", "DV-04", phase="draft")
        self.assertEqual(job["result"]["draft"]["priority"], "")

    def test_a_phase_the_help_does_not_have_is_refused(self):
        stack, patches = self.fake_agents([])
        with stack:
            for p in patches:
                stack.enter_context(p)
            for body in ({"kind": "definition", "id": "DV-04", "phase": "later"}, {"kind": "plan", "id": "DV-04", "phase": "questions"},
                         {"kind": "task", "phase": "questions"}):
                with self.subTest(body=body):
                    self.assertEqual(self.post_json("api/assist", body)[0], 400)

    def test_the_priority_is_kept_with_the_definition_and_shown_by_the_cli(self):
        board = self.board()
        board["tasks"][3]["definition"] = {"outcome": "O", "needs": "N", "acceptance": "A", "priority": "urgent", "approved": True}
        write_board(self.path, board)
        _, out, _ = self.run_cli("show", "DV-04")
        self.assertIn("Priority: Urgent", out)

    def test_the_architect_and_the_senior_developer_talk_before_the_plan(self):
        final = {"questions": ["Who may do it?"], "draft": {"plan": "1. Add the endpoint. 2. Test the club case."}}
        job = self.ask_agents(["Architect: use the service layer.", "Senior: reuse ClubAccessService.",
                               self.reply("Settled.", final)], "plan", "DV-02", {"plan": "my idea"})
        self.assertEqual(job["status"], "done", job)
        self.assertEqual([m["agent"] for m in job["messages"]], ["architect", "senior-dev", "architect"])
        agents = [c["command"][c["command"].index("--agent") + 1] for c in self.agent_calls]
        self.assertEqual(agents, ["architect", "senior-dev", "architect"])
        self.assertIn("my idea", self.agent_calls[0]["prompt"])
        self.assertIn("Architect: use the service layer.", self.agent_calls[1]["prompt"])
        self.assertIn("Senior: reuse ClubAccessService.", self.agent_calls[2]["prompt"])
        self.assertIn("Architect: use the service layer.", self.agent_calls[2]["prompt"])
        self.assertEqual(job["result"]["draft"], {"plan": "1. Add the endpoint. 2. Test the club case."})
        self.assertEqual([q["text"] for q in job["result"]["questions"]], ["Who may do it?"])
        self.assertEqual((job["turns"], round(job["cost"], 2)), (6, 0.03))
        for call in self.agent_calls:
            self.assertEqual(call["command"][call["command"].index("--tools") + 1], "Read,Glob,Grep")

    def test_the_agents_failures_reach_the_page_instead_of_hanging(self):
        for replies, expected in (([(1, "")], "could not answer"), ([(0, "not json")], "could not answer"),
                                  (["No draft here."], "did not return a draft"),
                                  ([subprocess.TimeoutExpired("claude", 1)], "took too long"),
                                  ([self.reply("x", {"title": ""})], "did not return a draft")):
            with self.subTest(expected=expected):
                job = self.ask_agents(replies, "task")
                self.assertEqual(job["status"], "failed")
                self.assertIn(expected, job["error"])
                self.assertEqual(job["working"], "")

    def test_the_agents_help_is_refused_for_what_it_cannot_do(self):
        stack, patches = self.fake_agents([])
        with stack:
            for p in patches:
                stack.enter_context(p)
            for body, expected in (({"kind": "story"}, 400), ({"kind": "definition", "id": "NOPE"}, 400),
                                   ({"kind": "plan", "id": "DV-04"}, 400), ({"kind": "plan"}, 400)):
                with self.subTest(body=body):
                    self.assertEqual(self.post_json("api/assist", body)[0], expected)
            self.assertEqual(self.request("POST", f"/{self.name}/api/assist", b"{}", {"Content-Type": "text/plain"})[0], 415)
            self.assertEqual(self.post_json("api/assist", {"kind": "task"}, {"Host": "evil.example:8765"})[0], 403)
            self.assertEqual(self.post_json("api/assist", {"kind": "task"}, {"X-Forwarded-For": "100.64.0.9"})[0], 403)
            self.assertEqual(self.request("POST", "/nope/api/assist", b"{}", {"Content-Type": "application/json"})[0], 404)
            self.assertEqual(self.request("GET", f"/{self.name}/api/assist/missing")[0], 404)
        with mock.patch.object(kanvas.shutil, "which", return_value=None):
            status, _, body = self.post_json("api/assist", {"kind": "task"})
        self.assertEqual(status, 400)
        self.assertIn("not installed", json.loads(body)["error"])
        self.assertEqual(self.agent_calls, [])

    def test_only_two_agent_conversations_run_at_a_time(self):
        gate = threading.Event()
        stack, patches = self.fake_agents([self.reply(x, {"title": x.upper()}) for x in "abc"], gate)
        with stack:
            for p in patches:
                stack.enter_context(p)
            ids = []
            for _ in range(2):
                status, _, body = self.post_json("api/assist", {"kind": "task"})
                self.assertEqual(status, 202)
                ids.append(json.loads(body)["job"])
            status, _, body = self.post_json("api/assist", {"kind": "task"})
            self.assertEqual(status, 429)
            self.assertIn("already running", json.loads(body)["error"])
            gate.set()
            self.assertEqual([self.finished_job(i)["status"] for i in ids], ["done", "done"])
            status, _, body = self.post_json("api/assist", {"kind": "task"})
            self.assertEqual(status, 202)
            self.assertEqual(self.finished_job(json.loads(body)["job"])["status"], "done")

    def test_compare_and_swap(self):
        _, headers, body = self.request("GET", self.api)
        etag = headers["ETag"]
        board = json.loads(body)
        self.run_cli("note", "DV-02", "agent wrote first")
        board["tasks"][3]["state"] = "todo"
        status, headers, body = self.put(board, etag)
        self.assertEqual(status, 409)
        current = json.loads(body)
        self.assertEqual(current["tasks"][1]["notes"][0]["text"], "agent wrote first")
        current["tasks"][3]["state"] = "todo"
        status, headers2, body = self.put(current, headers["ETag"])
        self.assertEqual(status, 200)
        self.assertEqual(body, self.raw())
        self.assertEqual(headers2["ETag"], kanvas.etag_of(self.raw()))
        self.assertEqual(self.get("DV-04")["state"], "todo")
        self.assertEqual(self.get("DV-02")["notes"][0]["text"], "agent wrote first")

    def test_rejects_unsafe_or_invalid_writes(self):
        before = self.raw()
        board = json.loads(before)
        self.assertEqual(self.put(board, ctype="text/plain")[0], 415)
        bad = json.loads(before)
        bad["tasks"][0]["state"] = "finished"
        self.assertEqual(self.put(bad, kanvas.etag_of(before))[0], 400)
        dup = json.loads(before)
        dup["tasks"].append(dict(dup["tasks"][0]))
        self.assertEqual(self.put(dup, kanvas.etag_of(before))[0], 400)
        status, _, _ = self.request("GET", self.api, headers={"Host": "evil.example:8765"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("PUT", self.api, b"{}", {"Host": "evil.example", "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(self.raw(), before)

    def test_writes_need_the_version_they_change_and_a_task_list(self):
        before = self.raw()
        board = json.loads(before)
        board["tasks"][3]["state"] = "todo"
        status, _, body = self.put(board)
        self.assertEqual(status, 428)
        self.assertIn("If-Match", json.loads(body)["error"])
        for partial in ({}, {"kanvas": 1, "title": "Test", "groups": []}):
            with self.subTest(partial=partial):
                status, _, body = self.put(partial, kanvas.etag_of(before))
                self.assertEqual((status, json.loads(body)["error"]), (400, "Expected a board with its task list"))
        self.assertEqual(self.put({"tasks": None}, kanvas.etag_of(before))[0], 400)
        self.assertEqual(self.raw(), before)
        status, headers, _ = self.put(board, kanvas.etag_of(before))
        self.assertEqual(status, 200)
        # A board whose tasks were all deleted still has its task list.
        self.assertEqual(self.put(dict(board, tasks=[]), headers["ETag"])[0], 200)
        self.assertEqual(self.board()["tasks"], [])

    def test_a_malformed_content_length_is_answered_at_once(self):
        before = self.raw()
        for length in ("abc", "-1", "1.5"):
            for method, path in (("PUT", self.api), ("POST", "/api/boards")):
                with self.subTest(length=length, method=method), \
                        socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
                    s.sendall(f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
                              f"If-Match: {kanvas.etag_of(before)}\r\nContent-Length: {length}\r\n\r\n".encode("ascii"))
                    status = s.makefile("rb").readline().split()[1]
                    self.assertEqual(status, b"400")
        self.assertEqual(self.raw(), before)

    def other_project(self, folder="other", title="Other"):
        path = os.path.join(self.dir, folder, "kanvas.json")
        os.makedirs(os.path.dirname(path))
        write_board(path, dict(self.initial_board(), title=title))
        return path

    def open_board(self, path, host="127.0.0.1"):
        body = json.dumps({"path": path}).encode("utf-8")
        return self.request("POST", "/api/boards", body, {"Content-Type": "application/json", "Host": host})

    def test_serves_several_boards_each_at_its_own_address(self):
        status, headers, _ = self.request("GET", "/")
        self.assertEqual((status, headers["Location"]), (302, f"/{self.name}/"))
        other = self.other_project()
        status, _, body = self.open_board(other)
        self.assertEqual((status, json.loads(body)), (200, {"name": "other", "url": "/other/"}))
        with open(other, "rb") as f:
            self.assertEqual(self.request("GET", "/other/api/board")[2], f.read())
        self.assertEqual(self.request("GET", self.api)[2], self.raw())
        _, headers, body = self.request("GET", "/other/api/board")
        board = json.loads(body)
        board["tasks"][3]["state"] = "todo"
        status, _, _ = self.request("PUT", "/other/api/board", json.dumps(board).encode("utf-8"),
                                    {"Content-Type": "application/json", "If-Match": headers["ETag"]})
        self.assertEqual(status, 200)
        with open(other) as f:
            self.assertEqual(json.load(f)["tasks"][3]["state"], "todo")
        self.assertEqual(self.get("DV-04")["state"], "proposed")

        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'href="/other/"', body)
        self.assertIn(f'href="/{self.name}/"'.encode(), body)
        listing = json.loads(self.request("GET", "/api/boards")[2])
        self.assertEqual(listing["kanvas"], kanvas.VERSION)
        self.assertEqual([(b["name"], b["title"]) for b in listing["boards"]], [(self.name, "Test"), ("other", "Other")])
        self.assertEqual(self.request("GET", "/other")[1]["Location"], "/other/")
        self.assertIn(b"<title>Factory</title>", self.request("GET", "/other/")[2])

    def test_names_boards_after_their_folders(self):
        self.assertEqual(json.loads(self.open_board(self.path)[2])["name"], self.name)
        first = self.other_project(os.path.join("a", "Some project!"))
        second = self.other_project(os.path.join("b", "Some project!"))
        api = self.other_project("api")
        self.assertEqual([json.loads(self.open_board(p)[2])["name"] for p in (first, second, api, first)],
                         ["Some-project", "Some-project-2", "api-2", "Some-project"])

    def test_unknown_boards_say_how_to_open_them(self):
        status, _, body = self.request("GET", "/nothing/")
        self.assertEqual(status, 404)
        self.assertIn(b"Run <code>kanvas serve</code> in its project", body)
        self.assertIn(b"its project folder may have been moved or renamed", body)
        self.assertNotIn(b"forgets", body)
        status, _, body = self.request("GET", "/nothing/api/board")
        self.assertEqual(status, 404)
        self.assertIn("kanvas serve", json.loads(body)["error"])
        self.assertEqual(self.request("PUT", "/nothing/api/board", b"{}", {"Content-Type": "application/json"})[0], 404)

    def test_the_boards_page_says_once_how_to_open_a_board(self):
        hint = b"Run <code>kanvas serve</code> in a project to open its board here."
        empty = kanvas.start_server(None, "127.0.0.1", 0, quiet=True)
        threading.Thread(target=empty.serve_forever, daemon=True).start()
        self.addCleanup(empty.server_close)
        self.addCleanup(empty.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", empty.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", "/")
        body = conn.getresponse().read()
        self.assertIn(b"No boards are open.", body)
        self.assertEqual(body.count(hint), 1)
        self.open_board(self.other_project())
        self.assertEqual(self.request("GET", "/")[2].count(hint), 1)

    def test_opening_a_board_needs_a_board_file_and_this_computer(self):
        other = self.other_project()
        self.assertEqual(self.open_board(other, host="evil.example")[0], 403)
        self.assertEqual(self.request("POST", "/api/boards", json.dumps({"path": other}).encode(), {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.open_board("other/kanvas.json")[0], 400)
        notes = os.path.join(self.dir, "notes.json")
        with open(notes, "w") as f:
            f.write('{"hello": 1}')
        self.assertEqual(self.open_board(notes)[0], 400)
        self.assertEqual([b["name"] for b in json.loads(self.request("GET", "/api/boards")[2])["boards"]], [self.name])

    def test_serve_in_another_project_opens_its_board_in_the_running_kanvas(self):
        other = self.other_project()
        code, out, err = self.run_cli("serve", "--no-open", "--port", str(self.port), cwd=os.path.dirname(other))
        self.assertIn(f"Opened in the Factory already running: http://127.0.0.1:{self.port}/other/", out)
        # The second project's own run sees its folder with symlinks resolved, as /private/var on macOS.
        self.assertEqual([os.path.realpath(b["path"]) for b in json.loads(self.request("GET", "/api/boards")[2])["boards"]],
                         [os.path.realpath(self.path), os.path.realpath(other)])

    def test_serve_finds_a_kanvas_that_took_a_later_port(self):
        """A Factory that took port + 2 while the ports below were busy is found once they are free again."""
        for _ in range(10):
            server = kanvas.start_server(self.path, "127.0.0.1", 0, quiet=True)
            port = server.server_address[1]
            if kanvas.port_free("127.0.0.1", port - 2) and kanvas.port_free("127.0.0.1", port - 1):
                break
            server.server_close()
        else:
            self.skipTest("no test port with two free ports below it")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        other = self.other_project()
        env = dict(os.environ, KANVAS_AGENT="")
        env.pop("KANVAS_BOARD", None)
        # A timeout, since a serve that misses the running Factory starts its own and keeps running.
        proc = subprocess.run([sys.executable, SCRIPT, "serve", "--no-open", "--port", str(port - 2)],
                              cwd=os.path.dirname(other), input=b"", capture_output=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        self.assertIn(f"Opened in the Factory already running: http://127.0.0.1:{port}/other/", proc.stdout.decode("utf-8"))
        self.assertEqual([b["name"] for b in server.board_list()], [self.name, "other"])
        self.assertTrue(kanvas.port_free("127.0.0.1", port - 2))

    def test_finding_a_kanvas_probes_only_taken_ports(self):
        """Connecting to a closed port takes seconds on Windows."""
        answer = mock.MagicMock()
        answer.__enter__.return_value.read.return_value = json.dumps({"kanvas": "9.9", "boards": []}).encode("utf-8")
        with mock.patch.object(kanvas, "port_free", side_effect=lambda host, p: p != 40019), \
                mock.patch.object(kanvas.LOCAL, "open", return_value=answer) as probe:
            self.assertEqual(kanvas.find_running(40000), ("http://127.0.0.1:40019/", "9.9"))
        self.assertEqual([c[0][0] for c in probe.call_args_list], ["http://127.0.0.1:40019/api/boards"])
        with mock.patch.object(kanvas, "port_free", return_value=True), mock.patch.object(kanvas.LOCAL, "open") as probe:
            self.assertIsNone(kanvas.find_running(40000))
        probe.assert_not_called()

    def phone_server(self):
        """A listener like the one `serve --tailscale` adds, here on another local port."""
        phone = kanvas.BoardServer(("127.0.0.1", 0), hosts=frozenset({"box.tail1.ts.net", "box", "100.64.0.7"}), quiet=True, share=self.server)
        threading.Thread(target=phone.serve_forever, daemon=True).start()
        self.addCleanup(phone.server_close)
        self.addCleanup(phone.shutdown)
        return phone.server_address[1]

    def phone_request(self, port, method, path, host, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=dict(headers or {}, Host=host))
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def test_a_phone_on_tailscale_sees_the_same_boards_by_this_computers_names_only(self):
        port = self.phone_server()
        for host in ("box.tail1.ts.net", f"box.tail1.ts.net:{port}", "box", "100.64.0.7:8765", "box.tail1.ts.net."):
            with self.subTest(host=host):
                self.assertEqual(self.phone_request(port, "GET", self.api, host), (200, self.raw()))
        for host in ("evil.example", "127.0.0.1", "localhost", "box.evil.example"):
            with self.subTest(host=host):
                self.assertEqual(self.phone_request(port, "GET", self.api, host)[0], 403)
        other = self.other_project()
        self.assertEqual(self.open_board(other)[0], 200)
        with open(other, "rb") as f:
            self.assertEqual(self.phone_request(port, "GET", "/other/api/board", "box"), (200, f.read()))
        board = json.loads(self.raw())
        board["tasks"][3]["state"] = "todo"
        status, _ = self.phone_request(port, "PUT", self.api, "box", json.dumps(board).encode("utf-8"),
                                       {"Content-Type": "application/json", "If-Match": kanvas.etag_of(self.raw())})
        self.assertEqual(status, 200)
        self.assertEqual(self.get("DV-04")["state"], "todo")

    def test_a_phone_cannot_open_boards(self):
        port = self.phone_server()
        body = json.dumps({"path": self.other_project()}).encode("utf-8")
        status, reply = self.phone_request(port, "POST", "/api/boards", "box", body, {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertIn("only from the computer running Factory", json.loads(reply)["error"])
        self.assertEqual([b["name"] for b in json.loads(self.request("GET", "/api/boards")[2])["boards"]], [self.name])

    def test_only_this_computer_sees_where_the_boards_live(self):
        other = self.other_project()
        self.open_board(other)
        path = self.path.encode("utf-8")
        self.assertEqual([b["path"] for b in json.loads(self.request("GET", "/api/boards")[2])["boards"]], [self.path, other])
        self.assertIn(path, self.request("GET", "/")[2])
        names_only = [{"name": self.name, "title": "Test"}, {"name": "other", "title": "Other"}]
        port = self.phone_server()
        status, body = self.phone_request(port, "GET", "/api/boards", "box")
        self.assertEqual((status, json.loads(body)["boards"]), (200, names_only))
        status, body = self.phone_request(port, "GET", "/", "box")
        self.assertEqual(status, 200)
        self.assertIn(b'href="/other/"', body)
        self.assertNotIn(path, body)
        # `tailscale serve` passes phones on to this computer's own address, by its Tailscale name.
        self.open_to_phone()
        self.assertEqual(json.loads(self.request("GET", "/api/boards", headers={"Host": "box.tail1.ts.net"})[2])["boards"], names_only)
        handler = kanvas.BoardHandler.__new__(kanvas.BoardHandler)
        for address, host, local in (("127.0.0.1", "127.0.0.1:8765", True), ("::1", "[::1]:8765", True), ("127.0.0.1", "localhost", True),
                                     ("100.64.0.9", "127.0.0.1:8765", False), ("127.0.0.1", "box.tail1.ts.net", False),
                                     ("192.168.1.5", "localhost:8765", False), ("127.0.0.1", "", False)):
            with self.subTest(address=address, host=host):
                handler.client_address, handler.headers = (address, 50000), {"Host": host}
                self.assertEqual(handler._from_this_computer(), local)

    def test_a_phone_is_not_told_where_a_board_it_could_not_save_lives(self):
        port = self.phone_server()
        board = json.loads(self.raw())
        board["tasks"][3]["state"] = "todo"
        body, headers = json.dumps(board).encode("utf-8"), {"Content-Type": "application/json", "If-Match": kanvas.etag_of(self.raw())}
        open(self.path + ".lock", "w").close()
        self.addCleanup(os.remove, self.path + ".lock")
        with mock.patch.object(kanvas, "LOCK_TIMEOUT", 0.2):
            status, reply = self.phone_request(port, "PUT", self.api, "box", body, headers)
            self.assertEqual((status, json.loads(reply)["error"]), (503, "The computer running Factory could not save the board"))
            status, _, reply = self.request("PUT", self.api, body, headers)
            self.assertEqual(status, 503)
            self.assertIn("lock file", json.loads(reply)["error"])

    def open_to_phone(self, magic=True):
        """POST to open the boards to phones, with Tailscale and its listener stood in for."""
        listener = mock.Mock()
        with mock.patch.object(kanvas, "tailscale_self", return_value=("100.64.0.7", "box.tail1.ts.net", magic)), \
                mock.patch.object(kanvas, "BoardServer", return_value=listener) as made:
            status, _, body = self.request("POST", f"/{self.name}/api/phone", b"{}", {"Content-Type": "application/json"})
        self.addCleanup(setattr, self.server, "phone_listener", None)
        return status, json.loads(body), made, listener

    def test_the_page_opens_the_boards_to_phones_and_gets_a_qr_code(self):
        self.assertEqual(json.loads(self.request("GET", f"/{self.name}/api/phone")[2]), {"urls": [], "qr": None, "manage": True})
        self.assertEqual(self.request("POST", f"/{self.name}/api/phone", b"{}", {"Content-Type": "text/plain"})[0], 415)
        status, info, made, listener = self.open_to_phone()
        self.assertEqual(status, 200)
        self.assertEqual(info["urls"], [f"http://box.tail1.ts.net:{self.port}/{self.name}/", f"http://100.64.0.7:{self.port}/{self.name}/"])
        self.assertTrue(info["qr"].startswith("<svg"))
        self.assertEqual(made.call_args[0][0], ("100.64.0.7", self.port))
        self.assertEqual(made.call_args[1]["hosts"], {"100.64.0.7", "box.tail1.ts.net", "box"})
        self.assertIs(made.call_args[1]["share"], self.server)
        self.assertEqual(json.loads(self.request("GET", f"/{self.name}/api/phone")[2])["urls"], info["urls"])
        # `tailscale serve` forwards requests for this computer's name to its own address.
        self.assertEqual(self.request("GET", self.api, headers={"Host": "box.tail1.ts.net"})[0], 200)
        self.assertEqual(self.request("GET", self.api, headers={"Host": "evil.example"})[0], 403)

    def test_without_magic_dns_phones_get_the_address_only(self):
        status, info, _, _ = self.open_to_phone(magic=False)
        self.assertEqual(info["urls"], [f"http://100.64.0.7:{self.port}/{self.name}/"])

    def test_opening_to_phones_says_what_tailscale_is_missing(self):
        with mock.patch.object(kanvas, "tailscale_self", side_effect=kanvas.KanvasError("Tailscale is not installed here.")):
            status, _, body = self.request("POST", f"/{self.name}/api/phone", b"{}", {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)["error"]), (503, "Tailscale is not installed here."))

    def test_a_phone_cannot_open_the_boards_to_phones(self):
        port = self.phone_server()
        status, _ = self.phone_request(port, "POST", f"/{self.name}/api/phone", "box", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        status, _ = self.phone_request(port, "DELETE", f"/{self.name}/api/phone", "box", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertIs(json.loads(self.phone_request(port, "GET", f"/{self.name}/api/phone", "box")[1])["manage"], False)

    def test_a_refusal_reads_the_body_first(self):
        """An answer sent with the request body unread lets Windows reset the connection and lose the answer."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(f"POST /{self.name}/api/phone HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Forwarded-For: 100.64.0.9\r\nContent-Type: application/json\r\n"
                      "Content-Length: 2\r\n\r\n".encode("ascii"))
            s.settimeout(0.3)
            with self.assertRaises(socket.timeout):
                s.recv(1)
            s.settimeout(5)
            s.sendall(b"{}")
            reply = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                reply += chunk
        self.assertTrue(reply.startswith(b"HTTP/1.0 403"), reply[:40])
        self.assertIn(b"Only the computer running Factory can open it to phones", reply)

    def test_phones_that_come_through_tailscale_serve_cannot_open_boards_or_change_phone_access(self):
        """`tailscale serve` passes phones on to this computer's own address, by its Tailscale name and with X-Forwarded headers."""
        self.open_to_phone()
        other = self.other_project()
        body, json_type = json.dumps({"path": other}).encode("utf-8"), {"Content-Type": "application/json"}
        for headers in ({"Host": "box.tail1.ts.net"}, {"Host": f"box.tail1.ts.net:{self.port}"}, {"Host": "box"}, {"Host": "100.64.0.7"},
                        {"Host": f"127.0.0.1:{self.port}", "X-Forwarded-For": "100.64.0.9"}, {"Host": "localhost", "X-Forwarded-Host": "box.tail1.ts.net"}):
            with self.subTest(headers=headers):
                status, _, reply = self.request("POST", "/api/boards", body, dict(headers, **json_type))
                self.assertEqual((status, json.loads(reply)["error"]), (403, "Boards open only from the computer running Factory"))
                status, _, reply = self.request("POST", f"/{self.name}/api/phone", b"{}", dict(headers, **json_type))
                self.assertEqual((status, json.loads(reply)["error"]), (403, "Only the computer running Factory can open it to phones"))
                status, _, reply = self.request("DELETE", f"/{self.name}/api/phone", b"{}", dict(headers, **json_type))
                self.assertEqual((status, json.loads(reply)["error"]), (403, "Only the computer running Factory can close it to phones"))
                status, _, reply = self.request("GET", f"/{self.name}/api/phone", headers=headers)
                self.assertEqual((status, len(json.loads(reply)["urls"]), json.loads(reply)["manage"]), (200, 2, False))
                status, _, reply = self.request("GET", "/api/boards", headers=headers)
                self.assertEqual((status, json.loads(reply)["boards"]), (200, [{"name": self.name, "title": "Test"}]))
        self.assertEqual([b["name"] for b in self.server.board_list()], [self.name])
        self.assertTrue(self.server.phone)
        status, headers, data = self.request("GET", self.api, headers={"Host": "box.tail1.ts.net", "X-Forwarded-For": "100.64.0.9"})
        self.assertEqual((status, data), (200, self.raw()))
        board = json.loads(data)
        board["tasks"][3]["state"] = "todo"
        status, _, _ = self.request("PUT", self.api, json.dumps(board).encode("utf-8"),
                                    dict(json_type, Host="box.tail1.ts.net", **{"If-Match": headers["ETag"], "X-Forwarded-For": "100.64.0.9"}))
        self.assertEqual(status, 200)
        self.assertEqual(self.get("DV-04")["state"], "todo")
        # This computer itself still opens boards and opens and closes Factory to phones, by each of its names.
        self.assertIs(json.loads(self.request("GET", f"/{self.name}/api/phone")[2])["manage"], True)
        status, _, reply = self.open_board(other, host=f"localhost:{self.port}")
        self.assertEqual((status, json.loads(reply)["name"]), (200, "other"))
        status, _, reply = self.request("DELETE", f"/{self.name}/api/phone", b"{}", dict(json_type, Host="[::1]"))
        self.assertEqual((status, json.loads(reply)), (200, {"urls": [], "qr": None, "manage": True}))
        self.assertEqual(self.open_to_phone()[0], 200)

    def test_the_page_closes_the_boards_to_phones_again(self):
        _, _, _, listener = self.open_to_phone()
        self.assertEqual(self.request("DELETE", f"/{self.name}/api/phone", b"{}", {"Content-Type": "text/plain"})[0], 415)
        status, _, body = self.request("DELETE", f"/{self.name}/api/phone", b"{}", {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)), (200, {"urls": [], "qr": None, "manage": True}))
        listener.shutdown.assert_called_once_with()
        listener.server_close.assert_called_once_with()
        self.assertEqual(self.request("GET", self.api, headers={"Host": "box.tail1.ts.net"})[0], 403)
        self.assertEqual(self.request("GET", self.api)[0], 200)
        self.assertEqual(self.request("DELETE", f"/{self.name}/api/phone", b"{}", {"Content-Type": "application/json"})[0], 200)

    def test_qr_codes_hold_the_address(self):
        m = kanvas.qr_matrix("http://box.tail1.ts.net:8765/kanvas/")
        self.assertEqual(len(m), 29)  # version 3
        for x, y in ((0, 0), (len(m) - 7, 0), (0, len(m) - 7)):
            self.assertEqual([m[y][x + i] for i in range(7)], [True] * 7)  # finder patterns
        self.assertEqual(len(kanvas.qr_matrix("http://h/" + "x" * 300)), 69)  # version 13
        lines = kanvas.qr_text("http://h/").splitlines()
        self.assertEqual(len(lines), (21 + 5) // 2)

    def test_a_board_opened_in_a_kanvas_open_to_phones_gets_its_phone_addresses(self):
        self.server.phone = ["http://box.tail1.ts.net:8765/", "http://100.64.0.7:8765/"]
        status, _, body = self.open_board(self.other_project())
        self.assertEqual(json.loads(body)["phone"], ["http://box.tail1.ts.net:8765/other/", "http://100.64.0.7:8765/other/"])
        url = f"http://127.0.0.1:{self.port}/"
        self.assertEqual(kanvas.join_running(url, self.path), (url + f"{self.name}/", [f"http://box.tail1.ts.net:8765/{self.name}/", f"http://100.64.0.7:8765/{self.name}/"]))

    def browser_after_agent(self, op, *agent_args):
        """The page loads the board, an agent runs a command, then the page saves `op`.

        `op` is JavaScript for the change the human makes, with `seen(id)` giving a task as the page showed it.
        """
        code = (
            "const base = `http://127.0.0.1:${input.port}/${input.name}/`;\n"
            "const fetch = (url, opts) => globalThis.fetch(new URL(url, base), opts);\n"
            "(async () => {\n"
            "  const store = serverStore(), shown = await store.load();\n"
            "  const seen = (id) => shown.tasks.find((x) => x.id === id);\n"
            "  require('child_process').execFileSync(input.python, [input.script, ...input.agent], { cwd: input.dir });\n"
            f"  try {{ await store.save({op}, shown); process.stdout.write('saved'); }}\n"
            "  catch (e) { process.stdout.write(`refused: ${e.message}`); }\n"
            "})();"
        )
        sections = [("function serverStore()", "function fileStore("), ("function mustFind(", "\n// ----"), ("function inlineMd(", "\n// ----")]
        args = {"port": self.port, "name": self.name, "python": sys.executable, "script": SCRIPT, "dir": self.dir, "agent": list(agent_args)}
        return run_html_js(self, code, json.dumps(args), sections)

    def browser_move_after_agent(self, state, *agent_args):
        """The page loads the board, an agent runs a command, then the human moves DV-02 to `state`."""
        op = "ops.startWork('DV-02', 'Plan')" if state == "doing" else f"ops.state('DV-02', seenOf(seen('DV-02')), {json.dumps(state)})"
        return self.browser_after_agent(op, *agent_args)

    def test_a_browser_move_does_not_overwrite_an_agent_move(self):
        self.run_cli("start", "DV-02", "-m", "Plan")
        out = self.browser_move_after_agent("todo", "finish", "DV-02", "-m", "Done; tests pass")
        self.assertEqual(out, "refused: DV-02 is in Review now, not Doing")
        self.assertEqual(self.get("DV-02")["state"], "review")

    def test_a_browser_move_is_replayed_over_other_agent_changes(self):
        out = self.browser_move_after_agent("doing", "note", "DV-03", "agent wrote first")
        self.assertEqual(out, "saved")
        self.assertEqual(self.get("DV-02")["state"], "doing")
        self.assertEqual(self.get("DV-03")["notes"][0]["text"], "agent wrote first")

    def test_approving_refuses_a_proposal_an_agent_revised(self):
        approve = "ops.define('DV-04', seenOf(seen('DV-04')), { approved: true, outcome: 'Ship it', needs: 'Safe', acceptance: 'Tests pass' })"
        out = self.browser_after_agent(approve, "edit", "DV-04", "--desc", "Now it also rewrites the parser")
        self.assertEqual(out, "refused: DV-04 was revised since you opened it; read the new version first")
        self.assertEqual(self.get("DV-04")["state"], "proposed")
        self.run_cli("propose", "Development", "Base")
        self.assertIn("refused: DV-04 was revised", self.browser_after_agent(approve, "depend", "DV-04", "DV-05"))
        self.assertEqual(self.browser_after_agent(approve, "note", "DV-04", "Only a note"), "saved")
        self.assertEqual(self.get("DV-04")["state"], "todo")

    def test_bulk_approval_cannot_bypass_product_definitions(self):
        self.run_cli("propose", "Development", "Second")
        self.run_cli("propose", "Development", "Third")
        approve = "ops.approve(['DV-04', 'DV-05', 'DV-06'].map((id) => seenOf(seen(id))))"
        out = self.browser_after_agent(approve, "edit", "DV-05", "--title", "Second, reworded")
        self.assertEqual(out, "refused: DV-04, DV-05, DV-06 need their PO/TL product definition first")
        self.assertEqual([self.get(i)["state"] for i in ("DV-04", "DV-05", "DV-06")], ["proposed"] * 3)

    def test_a_browser_edit_does_not_overwrite_an_agent_edit(self):
        edit = "ops.set('DV-04', { title: 'Mine' }, { title: seen('DV-04').title })"
        out = self.browser_after_agent(edit, "edit", "DV-04", "--title", "Theirs")
        self.assertEqual(out, "refused: the title of DV-04 changed meanwhile")
        self.assertEqual(self.get("DV-04")["title"], "Theirs")
        desc = "ops.set('DV-04', { desc: 'Mine' }, { desc: seen('DV-04').desc })"
        self.assertEqual(self.browser_after_agent(desc, "edit", "DV-04", "--desc", "Theirs"),
                         "refused: the description of DV-04 changed meanwhile")
        self.assertEqual(self.browser_after_agent(edit, "edit", "DV-04", "--desc", "Theirs, again"), "saved")
        self.assertEqual((self.get("DV-04")["title"], self.get("DV-04")["desc"]), ("Mine", "Theirs, again"))

    def test_a_ticked_box_follows_its_item_through_an_agent_edit(self):
        self.run_cli("edit", "DV-04", "--desc", "- [ ] a\n- [ ] b")
        tick = "ops.checkItem('DV-04', 'b', 0, true)"
        self.assertEqual(self.browser_after_agent(tick, "edit", "DV-04", "--desc", "Steps:\n\n- [ ] new\n- [ ] a\n- [ ] b"), "saved")
        self.assertEqual(self.get("DV-04")["desc"], "Steps:\n\n- [ ] new\n- [ ] a\n- [x] b")
        self.assertEqual(self.browser_after_agent(tick, "edit", "DV-04", "--desc", "- [ ] a\n- [ ] c"),
                         "refused: That checklist item in DV-04 changed meanwhile")
        self.assertEqual(self.get("DV-04")["desc"], "- [ ] a\n- [ ] c")

    def test_browser_writes_are_canonical(self):
        board = json.loads(self.raw())
        board["tasks"][3]["state"] = "todo"
        status, _, _ = self.request("PUT", self.api, json.dumps(board, indent=None).encode("utf-8"),
                                    {"Content-Type": "application/json", "If-Match": kanvas.etag_of(self.raw())})
        self.assertEqual(status, 200)
        text = self.raw().decode("utf-8")
        self.assertEqual(text, kanvas.dumps(kanvas.parse_board(text)))


class RememberTests(TempBoard):
    """A Factory started again reopens the boards and the phone access it had."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.dir, "state", "boards.json")

    def start(self, path=None):
        server = kanvas.start_server(path, "127.0.0.1", 0, quiet=True, remember=self.state)
        self.addCleanup(server.server_close)
        return server

    def other(self, folder):
        path = os.path.join(self.dir, folder, "kanvas.json")
        os.makedirs(os.path.dirname(path))
        write_board(path, self.initial_board())
        return path

    def remembered(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def test_open_boards_come_back_after_a_restart(self):
        first = self.start(self.path)
        other, gone = self.other("other"), self.other("gone")
        first.add_board(other)
        first.add_board(gone)
        self.assertEqual(self.remembered(), {"boards": [self.path, other, gone], "phone": False})
        first.server_close()
        os.remove(gone)
        again = self.start()
        self.assertEqual([(b["name"], b["path"]) for b in again.board_list()], [(kanvas.board_name(self.path), self.path), ("other", other)])
        self.assertFalse(again.phone_wanted)
        self.assertEqual(self.remembered()["boards"], [self.path, other])

    def test_a_board_served_again_keeps_its_place(self):
        other = self.other("other")
        self.start(self.path).add_board(other)
        self.assertEqual([b["path"] for b in self.start(other).board_list()], [self.path, other])

    def test_only_boards_are_reopened(self):
        notes = os.path.join(self.dir, "notes.json")
        with open(notes, "w") as f:
            f.write('{"hello": 1}')
        for state in ('{"boards": [%s, 7, null], "phone": "yes"}' % json.dumps(notes), "[]", "not json", '{"boards": "x"}'):
            with self.subTest(state=state):
                os.makedirs(os.path.dirname(self.state), exist_ok=True)
                with open(self.state, "w") as f:
                    f.write(state)
                server = self.start()
                self.assertEqual(server.board_list(), [])
                self.assertFalse(server.phone_wanted)
                server.server_close()

    def test_phone_access_is_remembered(self):
        server = self.start(self.path)
        with mock.patch.object(kanvas, "tailscale_self", return_value=("100.64.0.7", "box.tail1.ts.net", True)), \
                mock.patch.object(kanvas, "BoardServer"):
            server.open_to_phone()
        self.assertTrue(self.remembered()["phone"])
        self.assertTrue(self.start().phone_wanted)
        server.close_to_phone()
        self.assertFalse(self.remembered()["phone"])

    def test_phone_access_waits_for_tailscale(self):
        server = self.start(self.path)
        answers = [kanvas.KanvasError("Tailscale is not connected (Stopped)."), ("100.64.0.7", "box.tail1.ts.net", True)]
        with mock.patch.object(kanvas, "tailscale_self", side_effect=answers), mock.patch.object(kanvas, "BoardServer"):
            server.open_to_phone_soon(tries=3, wait=0.01)
            for _ in range(200):
                if server.phone:
                    break
                time.sleep(0.01)
        self.assertEqual(server.phone[-1], f"http://100.64.0.7:{server.server_address[1]}/")
        server.phone_listener = None

    def test_the_command_is_called_factory_and_kanvas_still_works(self):
        for typed, expected in (("factory", "factory"), (r"C:\bin\factory.exe", "factory"), ("/usr/bin/kanvas", "kanvas"),
                                ("kanvas-board", "kanvas"), ("", "kanvas")):
            with self.subTest(typed=typed), mock.patch.object(kanvas.sys, "argv", [typed]):
                self.assertEqual(kanvas.command_name(), expected)
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
            scripts = f.read().split("[project.scripts]")[1].split("[tool")[0]
        for name in ("factory", "kanvas", "kanvas-board"):
            self.assertIn(f'{name} = "kanvas_board:main"', scripts)

    def test_the_product_is_called_factory_where_people_read_it(self):
        with open(os.path.join(ROOT, "kanvas.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn("<title>Factory</title>", page)
        self.assertNotIn("<title>Kanvas</title>", page)
        code, out, _ = self.run_cli("--version")
        self.assertTrue(out.startswith("Factory "))
        self.assertIn("Factory", self.run_cli("--help")[1].splitlines()[2] + self.run_cli("--help")[1])

    def test_the_state_lives_in_the_users_own_folders(self):
        cases = (("posix", "linux", {"XDG_STATE_HOME": "/x/state"}, "/x/state/kanvas/boards.json"),
                 ("posix", "linux", {}, os.path.expanduser("~/.local/state/kanvas/boards.json")),
                 ("posix", "darwin", {}, os.path.expanduser("~/Library/Application Support/Kanvas/boards.json")),
                 ("nt", "win32", {"LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local"}, os.path.join("C:\\Users\\a\\AppData\\Local", "Kanvas", "boards.json")),
                 ("posix", "linux", {"KANVAS_STATE_DIR": "/elsewhere"}, "/elsewhere/boards.json"))
        for name, platform, env, expected in cases:
            with self.subTest(platform=platform, env=env), mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(kanvas.os, "name", name), mock.patch.object(kanvas.sys, "platform", platform):
                if "KANVAS_STATE_DIR" not in env:
                    os.environ.pop("KANVAS_STATE_DIR")
                if not env:
                    os.environ.pop("XDG_STATE_HOME", None)
                # Run on Windows, the other systems' paths come out with backslashes.
                self.assertEqual(os.path.normpath(kanvas.state_file()), os.path.normpath(expected))


class ServiceTests(unittest.TestCase):
    """`kanvas service` starts `kanvas serve --saved` at login on each system."""

    CMD = ["/home/a b/.local/bin/kanvas", "serve", "--saved", "--no-open"]

    def test_the_systemd_unit_quotes_the_command(self):
        unit = kanvas.systemd_unit(["/x/100% sure/kanvas", 'say "hi"'])
        self.assertIn('ExecStart="/x/100%% sure/kanvas" "say \\"hi\\""\n', unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_the_launch_agent_runs_at_login(self):
        import plistlib
        plist = plistlib.loads(kanvas.launchd_plist(self.CMD))
        self.assertEqual((plist["Label"], plist["ProgramArguments"], plist["RunAtLoad"]), ("kanvas", self.CMD, True))

    def test_the_windows_task_runs_at_logon(self):
        import xml.etree.ElementTree as ET
        with mock.patch.dict(os.environ, {"USERDOMAIN": "PC", "USERNAME": "a&b"}):
            xml = kanvas.windows_task(["C:\\Py thon\\pythonw.exe", "C:\\k\\kanvas.py", "serve", "--saved"])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        root = ET.fromstring(xml.split("\n", 1)[1])
        self.assertEqual(root.find(".//t:LogonTrigger/t:UserId", ns).text, "PC\\a&b")
        self.assertEqual(root.find(".//t:Exec/t:Command", ns).text, "C:\\Py thon\\pythonw.exe")
        self.assertEqual(root.find(".//t:Exec/t:Arguments", ns).text, "C:\\k\\kanvas.py serve --saved")

    def test_a_kanvas_run_by_uvx_is_not_installed_as_a_service(self):
        with mock.patch.object(kanvas, "INSTALLED", True), \
                mock.patch.object(kanvas.sys, "prefix", os.path.join(os.sep, "c", "uv", "archive-v0", "x")):
            with self.assertRaisesRegex(kanvas.KanvasError, "uv tool install kanvas-board"):
                kanvas.service_command()

    @unittest.skipIf(os.name == "nt" or sys.platform == "darwin", "the systemd service")
    def test_install_and_uninstall_on_linux(self):
        config = tempfile.mkdtemp(prefix="kanvas-config-")
        self.addCleanup(shutil.rmtree, config, True)
        unit = os.path.join(config, "systemd", "user", "kanvas.service")
        out = io.StringIO()
        running = ("http://127.0.0.1:40000/", kanvas.VERSION)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config}), \
                mock.patch.object(kanvas, "run_quiet", return_value=(0, "")) as run, \
                mock.patch.object(kanvas, "find_running", side_effect=[None, running, None]) as find, \
                mock.patch.object(sys, "stdout", out):
            calls = mock.Mock()
            calls.attach_mock(run, "run")
            calls.attach_mock(find, "find")
            self.assertEqual(kanvas.main(["service", "install", "--tailscale"]), 0)
            with open(unit) as f:
                self.assertIn("serve --saved --no-open", f.read().replace('"', ""))
            # The search for a Factory started by hand comes after the service's own one stops, and before it starts again.
            self.assertEqual(calls.mock_calls, [mock.call.run(["systemctl", "--user", "stop", "kanvas.service"]),
                                                mock.call.find(kanvas.DEFAULT_PORT),
                                                mock.call.run(["systemctl", "--user", "daemon-reload"]),
                                                mock.call.run(["systemctl", "--user", "enable", "--now", "kanvas.service"]),
                                                mock.call.find(kanvas.DEFAULT_PORT)])
            self.assertIn("Open http://127.0.0.1:40000/\n", out.getvalue())
            with open(kanvas.state_file()) as f:
                self.assertTrue(json.load(f)["phone"])
            run.return_value = (1, "Failed to connect to bus")
            with self.assertRaisesRegex(kanvas.KanvasError, "Failed to connect to bus"):
                kanvas.service_install(False)
            run.return_value = (0, "")
            self.assertEqual(kanvas.main(["service", "uninstall"]), 0)
            self.assertFalse(os.path.exists(unit))
        os.remove(kanvas.state_file())
        self.assertIn("Factory now starts when you log in", out.getvalue())
        self.assertIn("Factory no longer starts when you log in.", out.getvalue())

    @contextlib.contextmanager
    def system(self, port):
        """Service files in a temporary folder, system commands that succeed without running, and Factory's ports from `port` on."""
        folder = tempfile.mkdtemp(prefix="kanvas-config-")
        self.addCleanup(shutil.rmtree, folder, True)
        with mock.patch.object(kanvas, "service_paths", return_value=os.path.join(folder, "kanvas.service")), \
                mock.patch.object(kanvas, "run_quiet", return_value=(0, "")), \
                mock.patch.object(kanvas, "DEFAULT_PORT", port), mock.patch.object(sys, "stdout", io.StringIO()) as out:
            yield out

    def kanvas_above_free_ports(self):
        """Start a Factory on a port with the two ports below it free, as one that took port + 2; return its port."""
        for _ in range(10):
            server = kanvas.start_server(None, "127.0.0.1", 0, quiet=True)
            port = server.server_address[1]
            if kanvas.port_free("127.0.0.1", port - 2) and kanvas.port_free("127.0.0.1", port - 1):
                break
            server.server_close()
        else:
            self.skipTest("no test port with two free ports below it")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return port

    def test_status_sees_a_kanvas_on_any_of_its_ports(self):
        port = self.kanvas_above_free_ports()
        with self.system(port - 2) as out:
            self.assertEqual(kanvas.main(["service", "status"]), 0)
        self.assertIn(f"Running: yes, at http://127.0.0.1:{port}/\n", out.getvalue())

    def test_install_waits_for_the_service_to_answer(self):
        running = ("http://127.0.0.1:40002/", kanvas.VERSION)
        with self.system(40000) as out, mock.patch.object(kanvas, "find_running", side_effect=[None, None, None, running]) as find:
            self.assertEqual(kanvas.main(["service", "install"]), 0)
        self.assertEqual(find.call_args_list, [mock.call(40000)] * 4)
        self.assertIn("Open http://127.0.0.1:40002/\n", out.getvalue())
        with self.system(40000) as out, mock.patch.object(kanvas, "find_running", return_value=None):
            kanvas.service_install(False, wait=0)
        self.assertIn("It has not answered yet; `kanvas service status` says when it runs.\n", out.getvalue())

    def test_install_says_when_a_kanvas_started_by_hand_keeps_the_boards(self):
        """The service's `serve --saved` would join that Factory and exit, so nothing serves the boards once it stops."""
        port = self.kanvas_above_free_ports()
        for args, again in (([], "kanvas service install"), (["--tailscale"], "kanvas service install --tailscale")):
            with self.subTest(args=args), self.system(port - 2) as out:
                self.assertEqual(kanvas.main(["service", "install", *args]), 0)
                text = out.getvalue()
                self.assertIn(f"Factory already runs at http://127.0.0.1:{port}/, started with `kanvas serve`", text)
                self.assertIn(f"stop it (Ctrl+C in its terminal) and run `{again}` again.", text)
                self.assertNotIn("Open ", text)
        os.remove(kanvas.state_file())



class Terminal(io.StringIO):
    def isatty(self):
        return True


class ServeWithoutBoardTests(unittest.TestCase):
    """serve offers to create a missing board, and only asks a person at the terminal."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="kanvas-serve-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(os.chdir, os.getcwd())
        if kanvas.find_board(self.dir):
            self.skipTest("a kanvas.json above the temporary folder would be served")

    def project(self):
        """A fresh git project, with the current folder in its src/ subfolder."""
        top = tempfile.mkdtemp(dir=self.dir)
        os.mkdir(os.path.join(top, ".git"))
        os.mkdir(os.path.join(top, "src"))
        os.chdir(os.path.join(top, "src"))
        return top

    def serve(self, answer, *args, env=None, start=None):
        """Runs `serve` at a pretend terminal, answering its question with `answer` (text or an exception).
        `start` stands in for start_server."""
        self.server = mock.Mock(server_address=("127.0.0.1", 8765), check_host=True, phone=[], quiet=False, phone_wanted=False,
                                **{"add_board.return_value": "project", "board_list.return_value": []})
        stdin, stdout, stderr = mock.Mock(), Terminal(), io.StringIO()
        stdin.isatty.return_value = True
        reply = {"return_value": answer} if isinstance(answer, str) else {"side_effect": answer}
        started = {"side_effect": start} if start else {"return_value": self.server}
        with mock.patch.dict(os.environ), mock.patch("builtins.input", **reply) as self.ask, \
                mock.patch.object(kanvas, "start_server", **started) as self.start, \
                mock.patch.object(kanvas, "find_running", return_value=None), \
                mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", stderr):
            os.environ.pop("KANVAS_BOARD", None)
            os.environ.update(env or {})
            code = kanvas.main(["serve", "--no-open", *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_without_a_terminal_serve_names_init_and_creates_nothing(self):
        top = self.project()
        env = dict(os.environ)
        env.pop("KANVAS_BOARD", None)
        proc = subprocess.run([sys.executable, SCRIPT, "serve", "--no-open", "--port", "0"],
                              cwd=os.path.join(top, "src"), input=b"", capture_output=True, env=env, timeout=60)
        err = proc.stderr.decode("utf-8")
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn("No kanvas.json found", err)
        self.assertIn(" init", err)
        self.assertNotIn("[Y/n]", proc.stdout.decode("utf-8"))
        self.assertEqual(sorted(os.listdir(top)), [".git", "src"])
        self.assertEqual(os.listdir(os.path.join(top, "src")), [])

    def test_at_a_terminal_yes_sets_up_the_project_and_serves_it(self):
        for answer in ("", "y", "YES"):
            with self.subTest(answer=answer):
                top = self.project()
                code, out, err = self.serve(answer)
                self.assertEqual(code, 0, err)
                self.ask.assert_called_once_with(f"No kanvas.json here. Create one in {top}? [Y/n] ")
                board = os.path.join(top, "kanvas.json")
                self.assertTrue(os.path.isfile(board))
                self.assertTrue(os.path.isfile(os.path.join(top, ".agents", "skills", "kanvas", "SKILL.md")))
                self.assertIn(f"Factory {kanvas.VERSION} in {top}, the top folder of this git repository", out)
                self.assertIn("created   kanvas.json", out)
                self.assertNotIn("Open the board:", out)
                self.assertIn(f"Factory board: {board}", out)
                self.assertEqual(self.start.call_args[0][0], board)
                self.server.serve_forever.assert_called_once_with()

    def test_at_a_terminal_no_creates_nothing(self):
        for answer in ("n", "No", EOFError, KeyboardInterrupt):
            with self.subTest(answer=answer):
                top = self.project()
                code, out, err = self.serve(answer)
                self.assertEqual(code, 1, out)
                self.assertIn("No kanvas.json found", err)
                self.assertEqual(sorted(os.listdir(top)), [".git", "src"])
                self.start.assert_not_called()

    def test_a_missing_board_given_by_name_is_not_offered(self):
        self.project()
        for args, env in ((["--board", "missing.json"], None), ([], {"KANVAS_BOARD": "missing.json"})):
            with self.subTest(args=args, env=env):
                code, _, err = self.serve("y", *args, env=env)
                self.assertEqual(code, 1)
                self.assertIn("Board not found", err)
                self.ask.assert_not_called()
                self.assertFalse(os.path.exists("missing.json"))

    def test_tailscale_serves_on_the_tailscale_address_and_prints_the_phone_addresses(self):
        top = self.project()
        write_board(os.path.join(top, "kanvas.json"), {"title": "", "groups": [], "tasks": []})
        name = os.path.basename(top)
        for magic, expected in ((True, f"On your phone (Tailscale): http://box.tail1.ts.net:8765/{name}/  or  http://100.64.0.7:8765/{name}/"),
                                (False, f"On your phone (Tailscale): http://100.64.0.7:8765/{name}/\n")):
            with self.subTest(magic=magic), \
                    mock.patch.object(kanvas, "tailscale_self", return_value=("100.64.0.7", "box.tail1.ts.net", magic)):
                code, out, err = self.serve("n", "--tailscale", start=self.real_server)
                self.assertEqual(code, 0, err)
                self.assertIn(expected, out)
                self.assertIn("Scan it with your phone's camera:", out)
                self.assertIn("\u2580", out)
                self.assertIn("Anyone on your Tailscale network can open and edit your boards.", out)
                self.assertEqual(self.made.call_args[0][0], ("100.64.0.7", 8765))
                self.made.return_value.server_close.assert_called_once_with()
                self.made_patch.stop()

    def real_server(self, path, host, port, remember=None):
        """A BoardServer that does not listen, whose phone listener is a stand-in."""
        with mock.patch.object(socketserver.TCPServer, "server_bind"), mock.patch.object(socketserver.TCPServer, "server_activate"):
            server = kanvas.BoardServer((host, port), path, quiet=True)
        server.server_address = ("127.0.0.1", 8765)
        server.phone_wanted = False
        server.serve_forever = lambda: None
        self.made_patch = mock.patch.object(kanvas, "BoardServer")
        self.made = self.made_patch.start()
        self.addCleanup(mock.patch.stopall)
        return server

    def test_saved_serves_the_remembered_boards_without_a_project(self):
        os.chdir(self.dir)
        code, out, err = self.serve("n", "--saved")
        self.assertEqual(code, 0, err)
        self.ask.assert_not_called()
        self.assertIsNone(self.start.call_args[0][0])
        self.assertEqual(self.start.call_args[1]["remember"], kanvas.state_file())
        self.assertIn("Open http://127.0.0.1:8765/  (Ctrl+C to stop)", out)
        with mock.patch.object(kanvas, "find_running", return_value=("http://127.0.0.1:8765/", kanvas.VERSION)), \
                mock.patch.object(sys, "stdout", io.StringIO()) as stdout:
            self.assertEqual(kanvas.main(["serve", "--saved", "--no-open"]), 0)
        self.assertEqual(stdout.getvalue(), "Factory already runs at http://127.0.0.1:8765/\n")

    def test_tailscale_and_host_do_not_mix(self):
        top = self.project()
        write_board(os.path.join(top, "kanvas.json"), {"title": "", "groups": [], "tasks": []})
        code, _, err = self.serve("n", "--tailscale", "--host", "0.0.0.0")
        self.assertEqual(code, 1)
        self.assertIn("--tailscale or --host, not both", err)
        self.start.assert_not_called()


class TailscaleTests(unittest.TestCase):
    def status(self, info, which="/usr/bin/tailscale"):
        out = mock.Mock(stdout=json.dumps(info).encode("utf-8") if isinstance(info, dict) else info)
        with mock.patch("shutil.which", return_value=which), mock.patch("os.path.isfile", return_value=False), \
                mock.patch("subprocess.run", return_value=out) as run:
            result = kanvas.tailscale_self()
        self.assertEqual(run.call_args[0][0], [which, "status", "--json"])
        return result

    def test_reads_this_computers_address_and_name(self):
        info = {"BackendState": "Running", "Self": {"DNSName": "Box.tail1.ts.net.", "TailscaleIPs": ["fd7a:115c:a1e0::7", "100.64.0.7"]},
                "CurrentTailnet": {"MagicDNSEnabled": True}}
        self.assertEqual(self.status(info), ("100.64.0.7", "box.tail1.ts.net", True))
        info["CurrentTailnet"]["MagicDNSEnabled"] = False
        self.assertEqual(self.status(info), ("100.64.0.7", "box.tail1.ts.net", False))
        self.assertEqual(kanvas.tailscale_names("100.64.0.7", "box.tail1.ts.net"), {"100.64.0.7", "box.tail1.ts.net", "box"})

    def test_says_what_is_missing(self):
        with mock.patch("shutil.which", return_value=None), mock.patch("os.path.isfile", return_value=False):
            with self.assertRaisesRegex(kanvas.KanvasError, "Tailscale is not installed"):
                kanvas.tailscale_self()
        with self.assertRaisesRegex(kanvas.KanvasError, r"not connected \(Stopped\)"):
            self.status({"BackendState": "Stopped", "Self": {}})
        with self.assertRaisesRegex(kanvas.KanvasError, "did not say"):
            self.status(b"failed to connect to local tailscaled")
        with self.assertRaisesRegex(kanvas.KanvasError, "no IPv4 address"):
            self.status({"BackendState": "Running", "Self": {"TailscaleIPs": ["fd7a:115c:a1e0::7"]}})


def page_css():
    with open(os.path.join(ROOT, "kanvas.html"), encoding="utf-8") as f:
        html = f.read()
    return html[html.index("<style>") + 7:html.index("</style>")]


def css_block(css, head):
    """The text of the at-rule starting with `head`, through its closing brace."""
    start = css.index(head)
    i, depth = css.index("{", start) + 1, 1
    while depth:
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        i += 1
    return css[start:i]


def css_args(text):
    """`text` split at its top-level commas."""
    parts, depth, cur = [], 0, ""
    for c in text:
        depth += {"(": 1, ")": -1}.get(c, 0)
        if c == "," and not depth:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += c
    return parts + [cur.strip()]


def css_rules(css):
    """(selectors, declarations) of each innermost rule; keyframe steps come out as rules too."""
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        sel = re.sub(r"/\*.*?\*/", "", sel, flags=re.S).strip()
        yield [p for p in css_args(sel) if p], body


def css_rule(css, selector):
    """The declarations of the rules outside at-rules whose selector list is `selector`, later ones winning."""
    while "@media" in css:
        css = css.replace(css_block(css, "@media"), "")
    found = [body for sels, body in css_rules(css) if ", ".join(sels) == selector]
    if not found:
        raise KeyError(selector)
    return {k.strip(): v.strip() for body in found for k, _, v in (d.partition(":") for d in body.split(";")) if v}


def theme_env(css):
    """The custom properties of the light and the dark theme, as written."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    pick = lambda head: {k: v.strip() for k, v in re.findall(r"(--[\w-]+):\s*([^;{}]+);", css_block(css, head))}
    light = pick(":root {")
    return {"light": light, "dark": {**light, **pick('[data-theme="dark"] {')}}


def css_color(value, env):
    """(r, g, b, alpha) of a color written with hex, rgb(), transparent, var() and color-mix(in srgb, ...)."""
    v = value.strip()
    if v.startswith("var("):
        name, _, fallback = v[4:-1].partition(",")
        return css_color(env.get(name.strip(), fallback), env)
    if v.startswith("color-mix("):
        space, *args = css_args(v[10:-1])
        assert space == "in srgb", space
        parts = []
        for arg in args:
            m = re.match(r"(.+?)\s+(var\([^()]*\)|[\d.]+%)$", arg)
            color, share = (m.group(1), m.group(2)) if m else (arg, None)
            share = env[share[4:-1]] if share and share.startswith("var(") else share
            parts.append((css_color(color, env), float(share[:-1]) / 100 if share else None))
        (a, p), (b, q) = parts
        p = p if p is not None else 1 - q if q is not None else .5
        q = 1 - p if q is None else q
        alpha = a[3] * p + b[3] * q
        return tuple((x * a[3] * p + y * b[3] * q) / alpha if alpha else 0 for x, y in zip(a, b))[:3] + (alpha,)
    if v == "transparent":
        return (0, 0, 0, 0)
    m = re.match(r"rgb\((\d+) (\d+) (\d+)(?: / ([\d.]+))?\)$", v)
    if m:
        return tuple(float(x) for x in m.groups()[:3]) + (float(m.group(4) or 1),)
    h = v.lstrip("#")
    h = "".join(c * 2 for c in h) if len(h) == 3 else h
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (1,)


def over(top, under):
    return tuple(x * top[3] + y * (1 - top[3]) for x, y in zip(top[:3], under[:3])) + (1,)


def theme_colors(css):
    """The hex color variables of the light and the dark theme."""
    pick = lambda head: dict(re.findall(r"--([\w-]+):\s*(#[0-9a-f]{3,6})\b", css_block(css, head)))
    light = pick(":root {")
    return {"light": light, "dark": {**light, **pick('[data-theme="dark"] {')}}


def linear_rgb(color):
    if isinstance(color, str):
        h = color.lstrip("#")
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        color = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    return [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in (c / 255 for c in color[:3])]


def contrast(a, b):
    """The WCAG contrast ratio of two colors."""
    lum = lambda c: sum(w * v for w, v in zip((.2126, .7152, .0722), linear_rgb(c)))
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + .05) / (lo + .05)


def oklab_distance(a, b):
    def lab(c):
        r, g, b = linear_rgb(c)
        l, m, s = (x ** (1 / 3) for x in (.4122214708 * r + .5363325363 * g + .0514459929 * b,
                                          .2119034982 * r + .6806995451 * g + .1073969566 * b,
                                          .0883024619 * r + .2817188376 * g + .6299787005 * b))
        return (.2104542553 * l + .7936177850 * m - .0040720468 * s,
                1.9779984951 * l - 2.4285922050 * m + .4505937099 * s,
                .0259040371 * l + .7827717662 * m - .8086757660 * s)
    return sum((x - y) ** 2 for x, y in zip(lab(a), lab(b))) ** .5


class StyleTests(unittest.TestCase):
    MOVES = r"\b(scale|translate|rotate|transform|background-position|stroke-dashoffset|flex-grow)\b"

    def test_reduced_motion_holds_everything_still(self):
        css = page_css()
        still = css_block(css, "@media (prefers-reduced-motion: reduce)")
        rest = css.replace(still, "")
        keyframes = set(re.findall(r"@keyframes ([\w-]+)", rest))
        compound = lambda s: set(re.findall(r":is\([^)]*\)|::?[\w-]+|[.#][\w-]+|^[a-z]+", s))
        # A rule in the reduced-motion block covers a selector when it names a part of everything the selector names.
        covered = lambda sel, prop: any(compound(s) <= compound(sel) for sels, body in css_rules(still) if re.search(prop, body) for s in sels)
        for sels, body in css_rules(rest):
            for value in re.findall(r"(?:^|;)\s*animation:([^;]+)", body):
                name = next((w for w in value.split() if w in keyframes), None)
                for sel in sels if name else ():
                    with self.subTest(selector=sel, animation=name):
                        if "infinite" in value:
                            self.assertTrue(covered(sel, r"animation:\s*none"), "loops must stop")
                        else:
                            # One-shot animations keep their name, so animationend still clears the classes that start them.
                            self.assertIn(f"@keyframes {name} ", still)
                            self.assertNotRegex(css_block(still, f"@keyframes {name} "), self.MOVES)
            for value in re.findall(r"(?:^|;)\s*transition:([^;]+)", body):
                if re.search(self.MOVES, value):
                    for sel in sels:
                        with self.subTest(selector=sel, transition=value):
                            self.assertTrue(covered(sel, r"transition:(?![^;]*" + self.MOVES + ")"), "moving transitions must stop")

    def test_groups_past_the_tenth_get_colors_of_their_own(self):
        first_ten = ["#6366f1", "#0ea5e9", "#14b8a6", "#f59e0b", "#ec4899", "#84cc16", "#a855f7", "#f43f5e", "#06b6d4", "#78716c"]
        agents = ["claude", "codex", "cursor", "cursor-visual", "gemini", "aider"]
        out = json.loads(run_html_js(self, "console.log(JSON.stringify([GROUP_COLORS, input.map(colorFor)]))", json.dumps(agents)))
        groups, agent_colors = out
        self.assertEqual(groups[:10], first_ten, "boards keep their groups' colors")
        self.assertGreaterEqual(len(groups), 12)
        self.assertEqual(len(set(groups)), len(groups))
        for name, color in zip(agents, agent_colors):
            n = 0
            for ch in name:
                n = (n * 31 + ord(ch)) & 0xFFFFFFFF
            self.assertEqual(color, first_ten[n % 10], f"{name} keeps its color")
        themes = theme_colors(page_css())
        states = [t[k] for t in themes.values() for k in ("proposed", "todo", "blocked", "doing", "review", "done", "yolo", "accent")]
        for color in groups[10:]:
            with self.subTest(color=color):
                for theme, tokens in themes.items():
                    self.assertGreaterEqual(contrast(color, tokens["surface"]), 3, f"tags read on {theme} cards")
                self.assertGreaterEqual(min(oklab_distance(color, s) for s in states), .1, "unlike every state color")
                self.assertGreaterEqual(min(oklab_distance(color, g) for g in groups if g != color), .1, "unlike every other group")

    def test_text_reaches_wcag_aa_in_both_themes(self):
        css = page_css()
        rule = lambda selector: css_rule(css, selector)
        states = {s: {k: v for k, v in rule(f".s-{s}").items() if k.startswith("--")} for s in ("proposed", "todo", "blocked", "doing", "review", "done")}
        hover = float(re.search(r"brightness\(([\d.]+)\)", rule("button.primary:hover")["filter"]).group(1))
        brighter = lambda c: tuple(min(255, v * hover) for v in c[:3]) + (1,)
        stripes = css_args(re.search(r"repeating-linear-gradient\((.*)\)", rule(':is(.card, .kcard).s-blocked, [data-theme="dark"] :is(.card, .kcard).s-blocked')["background"]).group(1))[1:]
        for theme, env in theme_env(css).items():
            color = lambda value, **more: css_color(value, {**env, **more})

            def check(what, fg, under, need=4.5):
                with self.subTest(theme=theme, what=what):
                    self.assertGreaterEqual(contrast(fg, under), need)

            surface, bg = color("var(--surface)"), color("var(--bg)")
            grounds = {"surface": surface, "surface-2": color("var(--surface-2)"), "bg": bg, "lane": over(color("var(--lane)"), bg)}
            for token in ("text", "muted", "faint"):
                for name, ground in grounds.items():
                    check(f"--{token} on {name}", color(f"var(--{token})"), ground)
            check("placeholders", color(rule("::placeholder")["color"]), surface)
            for name in ("surface", "surface-2"):
                check(f"danger text on {name}", color(rule("button.danger")["color"]), grounds[name])
            for selector in ("button.primary", "button.danger.primary", "#inbox b", ".toast.error"):
                fill = color(rule(selector)["background"])
                check(selector, color(rule(selector)["color"]), fill)
                if selector.startswith("button"):
                    check(f"{selector} on hover", color(rule(selector)["color"]), brighter(fill))
            asking = rule(":is(.card, .kcard).asking .badge")
            check("waiting for you", color(asking["color"], **{"--c": "var(--accent)"}), color(asking["background"], **{"--c": "var(--accent)"}))
            badge, toast_badge, filled, button = rule(".badge"), rule(".toast .badge"), rule(":is(.card, .kcard):is(.s-doing, .s-review, .s-develop, .s-production) .badge"), rule("button.primary.to")
            for state, own in states.items():
                cards = [color(rule(".card")["background"], **own)]
                if state == "blocked":
                    cards = [color(re.sub(r"(\s+[\d.]+(px)?)+$", "", stop), **own) for stop in stripes]
                for card in cards + [surface]:
                    check(f"{state} badge", color(badge["color"], **own), over(color(badge["background"], **own), card))
                    check(f"YOLO mark on {state}", color(rule(".yolo-mark")["color"]), card, 3)
                mine = {**own, "--c-on": toast_badge["--c-on"]}
                check(f"{state} badge in a toast", color(toast_badge["color"], **mine), over(color(toast_badge["background"], **mine), color(rule(".toast")["background"])))
                if state in ("doing", "review"):
                    check(f"filled {state} badge", color(filled["color"], **own), color(filled["background"], **own))
                if "--c-fill" in own:
                    fill = color(button["background"], **own)
                    check(f"button to {state}", color(button["color"], **own), fill)
                    check(f"button to {state} on hover", color(button["color"], **own), brighter(fill))
        # Opacity would take the text of the zoom bar and of empty state chips below these ratios.
        for sels, body in css_rules(css):
            if re.search(r"(^|;)\s*opacity:\s*(?!1\b)", body):
                for sel in sels:
                    subject = re.sub(r"\([^()]*\)", "", re.sub(r"\([^()]*\)", "", sel)).split()[-1]
                    self.assertNotRegex(subject, r"^(\.zoombar|\.schip\.zero)(:[\w-]+)*$", f"{sel} fades its text")

    def test_states_read_apart_without_color(self):
        css = page_css()
        rule = lambda selector: css_rule(css, selector)
        states = {s: {k: v for k, v in rule(f".s-{s}").items() if k.startswith("--")} for s in ("proposed", "blocked", "todo", "doing", "review", "done")}
        marks = [own.get("--mark") for own in states.values()]
        self.assertTrue(all(marks), "every state has a mark")
        self.assertEqual(len(set(marks)), len(marks), "each mark has a shape of its own")
        self.assertIn("var(--mark)", rule(".dot")["mask"])
        for theme, env in theme_env(css).items():
            color = lambda value, **more: css_color(value, {**env, **more})
            surface_2 = color("var(--surface-2)")
            for state, own in states.items():
                grounds = {
                    "surface": color("var(--surface)"), "surface-2": surface_2, "a chosen tab": color(rule(".track > button.on")["background"]),
                    "a chip on hover": over(color(rule(".track > button:hover")["background"]), surface_2),
                    "the chosen chip": color(rule(".track > .schip.on")["background"], **own),
                    "an arrow menu row": color(rule(".ctx-menu .ctx-task")["background"], **own),
                    "an arrow menu row on hover": color(rule(".ctx-menu .ctx-task:hover, .ctx-menu .ctx-task:focus-visible")["background"], **own),
                    "a picker chip": color(rule(".pick-chip")["background"], **own),
                    "the picker's active option": color(rule(".pick-opt.on")["background"]),
                    "a column under a drag": color(rule(".col.drop")["background"]),
                }
                fgs = {"mark": (color(rule(".dot")["background"], **own), grounds)}
                fgs["mark of an empty chip"] = (color(rule(".schip.zero:not(.on) .dot")["background"]), {k: v for k, v in grounds.items() if k != "the chosen chip"})
                if state == "done":
                    fgs["hidden finished tasks"] = (color(rule(".schip.off .icon")["color"], **own), grounds)
                for what, (fg, under) in fgs.items():
                    for where, ground in under.items():
                        with self.subTest(theme=theme, state=state, mark=what, on=where):
                            self.assertGreaterEqual(contrast(fg, ground), 3)


class LayoutTests(unittest.TestCase):
    """The graph layout must not get more cluttered. Lower the ceilings when it improves;
    `python tests/layout_stats.py -v` shows the current numbers and what causes them, and
    `--unfolded` the same with every lane's finished tasks shown."""

    # (board, flows down, finished tasks unfolded): (crossing arrow pairs, arrows running under cards, bends, space over card area)
    CEILINGS = {
        ("sample.json", False, False): (3, 0, 12, 3.8),
        ("sample.json", True, False): (3, 0, 12, 3.8),
        ("tetris.json", False, False): (18, 0, 26, 6.1),
        ("tetris.json", True, False): (18, 0, 26, 6.7),
        ("phases.json", False, False): (3, 0, 14, 4.5),
        ("phases.json", True, False): (3, 0, 14, 4.7),
        ("sample.json", False, True): (3, 0, 12, 3.8),
        ("sample.json", True, True): (3, 0, 12, 3.8),
        ("tetris.json", False, True): (18, 0, 26, 6.1),
        ("tetris.json", True, True): (18, 0, 26, 6.7),
        ("phases.json", False, True): (5, 0, 21, 5.5),
        ("phases.json", True, True): (5, 0, 21, 6.7),
    }

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")

    def test_clutter_stays_under_ceilings(self):
        import layout_stats
        for unfolded in (False, True):
            for (path, vertical), m in layout_stats.measure(layout_stats.DEFAULT_BOARDS, unfolded=unfolded).items():
                crossings, behind, bends, space = self.CEILINGS[(os.path.basename(path), vertical, unfolded)]
                with self.subTest(board=os.path.basename(path), vertical=vertical, unfolded=unfolded):
                    self.assertLessEqual(m["crossings"], crossings)
                    self.assertLessEqual(m["behind"], behind)
                    self.assertLessEqual(m["bends"], bends)
                    self.assertLessEqual(m["space"], space)

    def test_lanes_that_share_a_track_stay_apart(self):
        code = (
            "const out = [];\n"
            "for (const text of input) {\n"
            "  const b = normalize(JSON.parse(text)), open = b.tasks.filter((t) => t.state !== 'done'), fold = foldDone(b.tasks, b, new Set());\n"
            "  for (const vertical of [false, true]) for (const levelGap of [30, 50, 70, 500]) for (const manual of [false, true]) for (const [tasks, stubs] of [[b.tasks, []], [open, []], [fold.tasks, fold.stubs]]) {\n"
            "    const L = layout(tasks, b, { vertical, levelGap, manual, emptyLanes: tasks !== open, stubs });\n"
            "    const box = (l) => [l.left, l.top, l.left + l.width, l.top + l.height];\n"
            "    const where = `${b.title} ${vertical ? 'down' : 'right'} ${levelGap}${manual ? ' manual' : ''}`;\n"
            "    L.lanes.forEach((l, i) => L.lanes.slice(i + 1).forEach((o) => {\n"
            "      const [a, c] = [box(l), box(o)];\n"
            "      if (a[0] < c[2] && c[0] < a[2] && a[1] < c[3] && c[1] < a[3]) out.push(`${where}: lanes ${l.id} and ${o.id} overlap`);\n"
            "    }));\n"
            "    for (const t of tasks) {\n"
            "      const p = L.pos.get(key(t.id)), l = L.lanes.find((x) => x.id === (b.groups.some((g) => g.id === t.group) ? t.group : null)), r = box(l);\n"
            "      if (p.x < r[0] || p.y < r[1] + LANE_HEAD || p.x + CW > r[2] || p.y + CH > r[3]) out.push(`${where}: ${t.id} is outside its lane`);\n"
            "    }\n"
            "    for (const s of L.stubs) {\n"
            "      const r = box(L.lanes.find((x) => x.id === s.group));\n"
            "      if (s.x < r[0] || s.y < r[1] + LANE_HEAD || s.x + CW > r[2] || s.y + STUB > r[3]) out.push(`${where}: the stub of ${s.group} is outside its lane`);\n"
            "    }\n"
            "    const boxes = [...[...L.pos].map(([k, p]) => [k, p.x, p.y, CH]), ...L.stubs.map((s) => [s.key, s.x, s.y, STUB])];\n"
            "    boxes.forEach((a, i) => boxes.slice(i + 1).forEach((c) => {\n"
            "      if (a[1] < c[1] + CW && c[1] < a[1] + CW && a[2] < c[2] + c[3] && c[2] < a[2] + a[3]) out.push(`${where}: ${a[0]} and ${c[0]} overlap`);\n"
            "    }));\n"
            "  }\n"
            "}\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        import layout_stats
        texts = []
        for path in layout_stats.DEFAULT_BOARDS:
            with open(path, encoding="utf-8") as f:
                texts.append(f.read())
        self.assertEqual(json.loads(run_html_js(self, code, json.dumps(texts))), [])

    def test_hiding_finished_tasks_drops_groups_with_nothing_left(self):
        code = (
            "const b = normalize(input), open = b.tasks.filter((t) => t.state !== 'done');\n"
            "const lanes = (tasks, emptyLanes) => layout(tasks, b, { vertical: false, levelGap: 70, emptyLanes }).lanes.map((l) => `${l.id}:${l.color}`);\n"
            "process.stdout.write(JSON.stringify({ all: lanes(b.tasks, false), shown: lanes(open, true), hidden: lanes(open, false) }));"
        )
        with open(os.path.join(ROOT, "examples", "sample.json"), encoding="utf-8") as f:
            out = json.loads(run_html_js(self, code, f.read()))
        self.assertEqual(len(out["all"]), 4)
        self.assertEqual(out["shown"], out["all"])
        self.assertEqual(out["hidden"], out["all"][1:])
        self.assertTrue(out["all"][0].startswith("RS:"))

    def test_finished_tasks_fold_into_a_stub_per_lane(self):
        board = {"kanvas": 1, "title": "Fold", "groups": [{"id": "A", "name": "Alpha"}, {"id": "B", "name": "Beta"}, {"id": "C", "name": "Gamma"}], "tasks": [
            task("A-1", "done", "A"), task("A-2", "done", "A", ["A-1"]), task("A-3", "done", "A", ["A-2"]),
            task("A-4", "todo", "A", ["A-3"]), task("A-5", "todo", "A"),
            task("B-1", "done", "B"), task("B-2", "done", "B"), task("B-3", "todo", "B", ["B-1", "A-2"]),
            task("C-1", "done", "C"), task("C-2", "done", "C"), task("C-3", "done", "C", ["C-1"]),
            task("X-1", "done"), task("X-2", "done"), task("X-3", "done"),
        ]}
        code = (
            "const b = normalize(input), out = {};\n"
            "const run = (name, open, keep, opts = {}) => {\n"
            "  const f = foldDone(b.tasks, b, new Set(open), keep), L = layout(f.tasks, b, { vertical: false, levelGap: 70, stubs: f.stubs, ...opts });\n"
            "  const stub = (g) => L.stubs.find((s) => s.group === g), lane = (g) => L.lanes.find((l) => l.id === g);\n"
            "  const inLane = (s) => { const l = lane(s.group); return s.x >= l.left && s.y >= l.top + LANE_HEAD && s.x + CW <= l.left + l.width && s.y + STUB <= l.top + l.height; };\n"
            "  const leads = (s) => [...L.pos].filter(([k]) => b.tasks.some((t) => key(t.id) === k && (t.group || null) === s.group) && L.pos.get(k).x === s.x).every(([, p]) => p.y >= s.y + STUB + GAP_Y);\n"
            "  out[name] = { shown: f.tasks.map((t) => t.id), stubs: L.stubs.map((s) => [s.group, s.ids.length, s.folded, s.open]),\n"
            "    edges: L.edges.filter((e) => !e.implied).map((e) => `${e.from}>${e.to}`).sort(), inLanes: L.stubs.every(inLane), lead: L.stubs.every(leads),\n"
            "    first: L.stubs.every((s) => s.x === L.pos.get('A-5').x), empty: L.lanes.filter((l) => l.empty).map((l) => l.id), behind: layoutMetrics(L).behind };\n"
            "};\n"
            "run('folded', [], null);\n"
            "run('manual', [], null, { manual: true });\n"
            "run('down', [], null, { vertical: true });\n"
            "run('open', ['A'], null);\n"
            "run('kept', [], 'C-3');\n"
            "out.hidden = foldDone(b.tasks.filter((t) => t.state !== 'done'), b, new Set()).stubs.length;\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        out = json.loads(run_html_js(self, code, json.dumps(board)))
        folded = out["folded"]
        # Lanes with three or more finished tasks fold them; Beta, with two, shows its own.
        self.assertEqual(folded["shown"], ["A-4", "A-5", "B-1", "B-2", "B-3"])
        self.assertEqual(folded["stubs"], [["A", 3, ["A-1", "A-2", "A-3"], False], ["C", 3, ["C-1", "C-2", "C-3"], False], [None, 3, ["X-1", "X-2", "X-3"], False]])
        # Arrows from folded tasks go with them, so A-4 starts its lane, after the stub.
        self.assertEqual(folded["edges"], ["B-1>B-3"])
        self.assertEqual(folded["empty"], [])
        for name in ("folded", "manual", "down", "open", "kept"):
            with self.subTest(name):
                self.assertTrue(out[name]["inLanes"])
                self.assertTrue(out[name]["lead"])
                self.assertEqual(out[name]["behind"], 0)
        self.assertTrue(folded["first"])
        self.assertEqual(out["manual"]["shown"], folded["shown"])
        # An opened lane shows its finished tasks and their arrows, and keeps its stub to fold them again.
        self.assertEqual(out["open"]["stubs"][0], ["A", 3, [], True])
        self.assertEqual(out["open"]["edges"], ["A-1>A-2", "A-2>A-3", "A-2>B-3", "A-3>A-4", "B-1>B-3"])
        # The task the human selected stays shown in a folded lane.
        self.assertIn("C-3", out["kept"]["shown"])
        self.assertEqual(out["kept"]["stubs"][1], ["C", 3, ["C-1", "C-2"], False])
        # With finished tasks hidden there is nothing to fold.
        self.assertEqual(out["hidden"], 0)

    def test_graph_opens_readable_on_the_work_that_waits(self):
        code = (
            "const b = normalize(input), L = layout(b.tasks, b, { vertical: false, levelGap: 70 }), ix = indexOf(b);\n"
            "const ids = (s) => b.tasks.filter((t) => vstateIn(ix, t) === s).map((t) => key(t.id));\n"
            "const room = (w, h) => ({ left: 24, top: 24, right: 24 + w, bottom: 24 + h });\n"
            "const shown = (r, v, ks) => ks.every((k) => { const p = L.pos.get(k), x = v.x + p.x * v.k, y = v.y + p.y * v.k;\n"
            "  return x >= r.left && y >= r.top && x + CW * v.k <= r.right && y + CH * v.k <= r.bottom; });\n"
            "const tiers = [ids('review'), ids('doing'), ids('proposed')], out = {};\n"
            "for (const [name, w, h] of [['small', 0.5, 0.5], ['narrow', 0.2, 0.6]]) {\n"
            "  const r = room(L.width * w, L.height * h), v = readableView(r, L, tiers, 0.7);\n"
            "  out[name] = { k: v.k, review: shown(r, v, tiers[0]), doing: shown(r, v, tiers[1]), proposed: shown(r, v, tiers[2]) };\n"
            "}\n"
            "const tall = readableView(room(L.width * 0.9, L.height * 0.5), L, tiers, 0.7);\n"
            "const all = readableView(room(L.width * 0.2, L.height * 0.6), L, [b.tasks.map((t) => key(t.id))], 0.7);\n"
            "out.tall = { k: tall.k, x: tall.x, review: shown(room(L.width * 0.9, L.height * 0.5), tall, tiers[0]) };\n"
            "out.all = { x: all.x, y: all.y };\n"
            "process.stdout.write(JSON.stringify(out));"
        )
        with open(os.path.join(ROOT, "examples", "sample.json"), encoding="utf-8") as f:
            out = json.loads(run_html_js(self, code, f.read()))
        # Too large to fit at a readable zoom: the work waiting for the human comes first, then what fits with it.
        self.assertEqual(out["small"], {"k": 0.7, "review": True, "doing": True, "proposed": False})
        self.assertEqual((out["narrow"]["k"], out["narrow"]["review"], out["narrow"]["proposed"]), (0.7, True, False))
        # A tall graph fits its width and scrolls to the review.
        self.assertAlmostEqual(out["tall"]["k"], 0.9)
        self.assertAlmostEqual(out["tall"]["x"], 24)
        self.assertTrue(out["tall"]["review"])
        # A focus larger than the room shows its start.
        self.assertEqual(out["all"], {"x": 24, "y": 24})

    def test_manual_layout_keeps_file_order(self):
        import layout_stats
        for (path, vertical), m in layout_stats.measure(layout_stats.DEFAULT_BOARDS, manual=True).items():
            with self.subTest(board=os.path.basename(path), vertical=vertical):
                self.assertEqual(m["misordered"], 0)
                self.assertEqual(m["behind"], 0)


class DesignCheckTests(unittest.TestCase):
    config = {"design": {"enabled": True, "repositories": ["front"]}}

    def task(self, needs_design=None, repositories=("api",)):
        work = {"repositories": list(repositories)}
        if needs_design is not None:
            work["needs_design"] = needs_design
        return {"id": "X-1", "state": "doing", "definition": {"repositories": list(repositories)}, "work": work}

    def test_the_check_of_the_plan_decides_when_it_is_set(self):
        self.assertTrue(kanvas.design_applies(self.task(True), self.config))
        self.assertFalse(kanvas.design_applies(self.task(False, ["front"]), self.config))

    def test_a_task_without_the_check_follows_its_repositories(self):
        self.assertFalse(kanvas.design_applies(self.task(), self.config))
        self.assertTrue(kanvas.design_applies(self.task(repositories=["front"]), self.config))

    def test_the_answers_of_the_owner_reach_the_plan_prompt(self):
        board = {"title": "T", "groups": [], "tasks": []}
        task = {"id": "X-1", "title": "t", "state": "todo"}
        prompt = kanvas.assist_prompt("plan", 0, board, task, {}, "/x", {}, {}, [], "draft",
                                      [{"id": "q1", "q": "Which clubs first?", "a": "Only Voltrega"}])
        self.assertIn("Which clubs first?", prompt)
        self.assertIn("Only Voltrega", prompt)


if __name__ == "__main__":
    unittest.main()
