"""Smoke tests of kanvas.html in headless Chrome against `kanvas.py serve`. Run with the suite:

    python -m unittest discover -s tests -v

They look for Chrome or Chromium on PATH and in the usual install folders, and skip without one. KANVAS_TEST_CHROME
names the browser to use (a command or a path); when it is set, a missing browser fails the tests instead.
"""

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import kanvas  # noqa: E402

SCRIPT = os.path.join(ROOT, "kanvas.py")
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

# A stand-in for `claude`, which the agents' help in the dialogs runs: it answers by what the prompt asks.
FAKE_CLAUDE = '''
import json, sys
prompt = sys.stdin.read()
def out(text):
    print(json.dumps({"type": "result", "result": text, "total_cost_usd": 0.01, "num_turns": 2, "is_error": False}))
def fenced(data):
    return "```json\\n" + json.dumps(data) + "\\n```"
if "The architect speaks first" in prompt:
    out("Architect proposes the service layer.")
elif "The senior developer speaks second" in prompt:
    out("Senior: reuse the access service.")
elif "The architect speaks last" in prompt:
    plan = "Plan for Voltrega only." if "Answer: Voltrega" in prompt else "Final plan from the conversation."
    out("Settled.\\n" + fenced({"questions": [{"text": "Which clubs first?", "options": ["All", "Voltrega"]}], "draft": {"plan": plan}}))
elif "defining the product need" in prompt:
    out("Questions below.\\n" + fenced({"questions": ["Which roles?"], "draft": {"outcome": "Outcome from the agent",
        "needs": "Needs from the agent", "acceptance": "Acceptance from the agent", "non_goals": "", "repositories": [],
        "agent": "implementer"}}))
else:
    out("A bug.\\n" + fenced({"title": "Fix the login crash", "group": "", "desc": "It no longer crashes.",
        "duplicates": [], "questions": ["Which phones?"]}))
'''


def chrome_paths():
    yield "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    yield "/Applications/Chromium.app/Contents/MacOS/Chromium"
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(var):
            yield os.path.join(os.environ[var], "Google", "Chrome", "Application", "chrome.exe")


def find_chrome(test):
    wanted = os.environ.get("KANVAS_TEST_CHROME")
    if wanted:
        found = shutil.which(wanted)
        if not found:
            test.fail(f"KANVAS_TEST_CHROME is {wanted!r}, which is not a program")
        return found
    found = next(filter(None, map(shutil.which, CHROME_NAMES)), None) or next(filter(os.path.isfile, chrome_paths()), None)
    if not found:
        test.skipTest(f"Chrome is not installed (looked for {', '.join(CHROME_NAMES)} and the usual install folders); "
                      "set KANVAS_TEST_CHROME to run the browser tests")
    return found


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def described(details):
    return details.get("exception", {}).get("description") or details["text"]


def masked(data, key):
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes((key * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")


class DevTools:
    """A connection to Chrome's DevTools protocol. Python's standard library has no WebSocket client, and a local
    Chrome needs only a small part of one: masked text frames out, whole or fragmented frames in."""

    def __init__(self, url, timeout=10):
        parts = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
        self.buf = bytearray()
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock.sendall(f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode("ascii"))
        while b"\r\n\r\n" not in self.buf:
            self._recv()
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        if head.split(b" ")[1:2] != [b"101"]:
            raise ConnectionError(f"Chrome refused the DevTools connection: {head.decode('latin-1')}")
        self.buf = bytearray(rest)
        self.last = 0
        self.handlers = {}

    def close(self):
        self.sock.close()

    def _recv(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("Chrome closed the DevTools connection")
        self.buf += chunk

    def _take(self, n):
        while len(self.buf) < n:
            self._recv()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _frame(self, op, payload):
        n = len(payload)
        size = bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 \
            else bytes([0x80 | 127]) + struct.pack(">Q", n)
        key = os.urandom(4)
        self.sock.sendall(bytes([0x80 | op]) + size + key + masked(payload, key))

    def _message(self):
        data = b""
        while True:
            b0, b1 = self._take(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            key = self._take(4) if b1 & 0x80 else None
            payload = self._take(n)
            if key:
                payload = masked(payload, key)
            op = b0 & 0x0F
            if op == 8:
                raise ConnectionError("Chrome closed the DevTools connection")
            if op == 9:
                self._frame(10, payload)
            elif op in (0, 1, 2):
                data += payload
                if b0 & 0x80:
                    return json.loads(data.decode("utf-8"))

    def post(self, method, **params):
        """Sends a command without waiting for its answer, as event handlers must."""
        self.last += 1
        self._frame(1, json.dumps({"id": self.last, "method": method, "params": params}).encode("utf-8"))
        return self.last

    def send(self, method, **params):
        mine = self.post(method, **params)
        while True:
            msg = self._message()
            if msg.get("id") == mine:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error'].get('message')}")
                return msg.get("result", {})
            if msg.get("method") in self.handlers:
                self.handlers[msg["method"]](msg.get("params", {}))

    def on(self, event, handler):
        self.handlers[event] = handler


class Page(DevTools):
    """A tab, with what it logged: console errors, uncaught exceptions and failed requests."""

    def __init__(self, url):
        super().__init__(url)
        self.errors = []
        self.on("Runtime.consoleAPICalled", self.console)
        self.on("Runtime.exceptionThrown", lambda p: self.errors.append(f"exception: {described(p['exceptionDetails'])}"))
        self.on("Log.entryAdded", self.log)
        self.send("Runtime.enable")
        self.send("Log.enable")

    def console(self, p):
        if p["type"] in ("error", "assert"):
            self.errors.append(f"console.{p['type']}: " + " ".join(str(a.get("value", a.get("description"))) for a in p["args"]))

    def log(self, p):
        e = p["entry"]
        if e["level"] == "error":
            self.errors.append(f"{e['source']}: {e['text']} {e.get('url', '')}".rstrip())

    def logged(self):
        """Everything logged so far: events come before the answer to a later command."""
        self.js("0")
        return self.errors

    def js(self, expression):
        r = self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise AssertionError(f"{expression}\nthrew {described(r['exceptionDetails'])}")
        return r["result"].get("value")


NOTE_BOX = "document.querySelector('#panel .note-form textarea')"
ADD_NOTE = "[...document.querySelectorAll('#panel .note-form button')].find((b) => b.textContent.includes('Add note')).click()"


class BrowserSmokeTests(unittest.TestCase):
    """The page in headless Chrome: it loads cleanly, opens a task, saves notes, keeps a note whose save failed, and
    shows what agents change through the CLI."""

    def setUp(self):
        self.chrome = find_chrome(self)
        self.dir = tempfile.mkdtemp(prefix="kanvas-browser-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        os.mkdir(os.path.join(self.dir, "demo"))
        self.path = os.path.join(self.dir, "demo", "kanvas.json")
        shutil.copyfile(os.path.join(ROOT, "examples", "sample.json"), self.path)
        self.env = dict(os.environ, KANVAS_STATE_DIR=os.path.join(self.dir, "state"), PYTHONUNBUFFERED="1")
        self.env.pop("KANVAS_BOARD", None)
        self.install_fake_claude()
        self.url = self.start_server()
        self.page = self.start_chrome()

    def install_fake_claude(self):
        """Puts a fake `claude` first on the PATH of the server, so the agents' help never reaches the real one."""
        folder = os.path.join(self.dir, "bin")
        os.mkdir(folder)
        script = os.path.join(folder, "fake_claude.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(FAKE_CLAUDE)
        if os.name == "nt":
            with open(os.path.join(folder, "claude.bat"), "w", encoding="ascii", newline="\r\n") as f:
                f.write(f'@echo off\n"{sys.executable}" "{script}"\n')
        else:
            launcher = os.path.join(folder, "claude")
            with open(launcher, "w", encoding="utf-8") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
            os.chmod(launcher, 0o755)
        self.env["PATH"] = folder + os.pathsep + self.env.get("PATH", "")

    def wait_for(self, what, check, timeout=10, details=lambda: ""):
        deadline = time.monotonic() + timeout
        while True:
            value = check()
            if value:
                return value
            if time.monotonic() > deadline:
                self.fail(f"Waited {timeout} s for {what}. {details()}")
            time.sleep(0.1)

    def until(self, what, expression, timeout=10):
        """Waits until `expression` is true on the page."""
        return self.wait_for(what, lambda: self.page.js(expression), timeout,
                             lambda: f"The page logged: {self.page.errors}. Its panel shows: "
                                     f"{self.page.js('document.querySelector(`#panel`).innerText')!r}")

    def start_server(self):
        log = os.path.join(self.dir, "serve.log")
        with open(log, "w", encoding="utf-8") as out:
            self.server = subprocess.Popen([sys.executable, SCRIPT, "--board", self.path, "serve", "--no-open", "--port", "0"],
                                           cwd=self.dir, env=self.env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        self.addCleanup(stop, self.server)

        def address():
            if self.server.poll() is not None:
                self.fail(f"serve exited with {self.server.returncode}:\n{read(log)}")
            return re.search(r"^Open (http://\S+)", read(log), re.M)
        return self.wait_for("serve to print its address", address, timeout=30, details=lambda: read(log)).group(1)

    def start_chrome(self):
        log, profile = os.path.join(self.dir, "chrome.log"), os.path.join(self.dir, "chrome")
        with open(log, "w", encoding="utf-8") as out:
            chrome = subprocess.Popen([self.chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                                       "--no-first-run", "--no-default-browser-check", "--no-sandbox",
                                       "--disable-background-networking", "--disable-component-update", "--disable-sync",
                                       "--password-store=basic", "--use-mock-keychain", "--window-size=1280,900", "about:blank"],
                                      stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        self.addCleanup(stop, chrome)

        # A browser confined to its own /tmp (Chromium as a snap) writes DevToolsActivePort where this test cannot
        # read it, and still prints the address.
        def devtools_port():
            if chrome.poll() is not None:
                self.fail(f"Chrome exited with {chrome.returncode}:\n{read(log)}")
            active = os.path.join(profile, "DevToolsActivePort")
            try:
                lines = read(active).split()
            except OSError:  # not written yet, or on Windows still open for writing
                lines = []
            if len(lines) == 2:
                return lines[0]
            printed = re.search(r"DevTools listening on ws://[^/]+:(\d+)/", read(log))
            return printed and printed.group(1)
        port = self.wait_for("Chrome's DevTools port", devtools_port, timeout=30, details=lambda: read(log))
        lists = f"http://127.0.0.1:{port}/json/list"

        def tab():
            with kanvas.LOCAL.open(lists, timeout=5) as r:
                return next((t["webSocketDebuggerUrl"] for t in json.loads(r.read()) if t["type"] == "page"), None)
        page = Page(self.wait_for("Chrome's first tab", tab))
        self.addCleanup(page.close)
        self.addCleanup(self.close_chrome, page, chrome)
        return page

    def close_chrome(self, page, chrome):
        """Lets Chrome shut down by itself, so it leaves no process behind holding its profile folder."""
        try:
            page.post("Browser.close")
            chrome.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def load(self, hash=""):
        self.page.send("Page.navigate", url=self.url + hash)
        self.until("the board's cards", "document.readyState === 'complete' && !!document.querySelector('[data-id=\"BE-03\"]')")

    def type_note(self, text):
        self.page.js(f"{NOTE_BOX}.focus()")
        self.page.send("Input.insertText", text=text)
        self.page.js(ADD_NOTE)

    def notes(self, tid):
        return [n["text"] for n in kanvas.find_task(kanvas.load_board(self.path), tid)["notes"]]

    def cli(self, *args):
        proc = subprocess.run([sys.executable, SCRIPT, "--board", self.path, *args, "--as", "smoke-test"],
                              env=self.env, stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_a_task_opens_saves_a_note_and_shows_agent_changes(self):
        self.load()
        self.assertTrue(self.page.js("document.querySelector('#panel').hidden"))
        self.page.js("location.hash = 'BE-03'")
        self.until("the panel of BE-03", "!document.querySelector('#panel').hidden && "
                                         "document.querySelector('#panel .cid').textContent === 'BE-03'")
        self.assertEqual(self.page.js("document.querySelector('#panel .p-title').value"), "City search endpoint")

        note = "Checked the search: Zürich and São Paulo both resolve."
        self.type_note(note)
        self.wait_for("the note in kanvas.json", lambda: note in self.notes("BE-03"))
        self.until("the saved note in the panel", f"document.querySelector('#panel').innerText.includes({json.dumps(note)})"
                                                  f" && {NOTE_BOX}.value === ''")

        self.cli("note", "BE-03", "Agent note from the CLI")
        self.cli("start", "FE-04", "-m", "Implement theme tokens, persistence and contrast tests")
        self.until("the agent's note and start", "document.querySelector('#panel').innerText.includes('Agent note from the CLI')"
                                                 " && document.querySelector('[data-id=\"FE-04\"]').classList.contains('s-doing')",
                   timeout=5)
        self.assertEqual(self.page.logged(), [])

    def test_the_panel_shows_the_screenshots_and_videos_a_task_left(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
        folder = os.path.join(self.dir, "demo", ".kanvas", "evidence", "BE-03", "run")
        os.makedirs(folder)
        for name, data in (("01-step.png", png), ("video.webm", b"not a real video")):
            with open(os.path.join(folder, name), "wb") as f:
                f.write(data)
        self.load("#BE-03")
        self.until("the evidence of BE-03", "!!document.querySelector('#panel .ev-grid img')")
        self.assertEqual(self.page.js("document.querySelector('#panel .ev-clip video').getAttribute('src')"),
                         "evidence/BE-03/run/video.webm")
        self.assertEqual(self.page.js("document.querySelector('#panel .ev-shot').getAttribute('href')"),
                         "evidence/BE-03/run/01-step.png")
        self.until("the screenshot to load", "document.querySelector('#panel .ev-grid img').naturalWidth === 1")
        self.assertTrue(self.page.js("document.querySelector('#panel .ev-grid').parentElement"
                                     ".querySelector('.section-head').textContent.startsWith('Evidence')"))

    def use_the_draft(self):
        self.until("the agents' draft", "!!document.querySelector('dialog .assist-use')", timeout=30)

    def test_the_product_owner_drafts_a_new_task_from_its_dialog(self):
        self.load()
        self.page.js("document.querySelector('#new-task').click()")
        self.until("the New task dialog", "!!document.querySelector('dialog .assist')")
        self.page.js("document.querySelector('dialog input[name=title]').value = 'app crashes on login'")
        self.page.js("document.querySelector('dialog .assist-bar button').click()")
        self.until("the questions", "!!document.querySelector('dialog .assist-questions')", timeout=30)
        self.assertIn("Which phones?", self.page.js("document.querySelector('dialog .assist-questions').innerText"))
        self.use_the_draft()
        self.assertEqual(self.page.js("document.querySelector('dialog input[name=title]').value"), "Fix the login crash")
        self.assertEqual(self.page.js("document.querySelector('dialog textarea[name=desc]').value"), "It no longer crashes.")
        self.assertEqual(self.page.logged(), [])

    def test_the_product_owner_drafts_a_definition_from_its_dialog(self):
        self.load("#BE-05")
        self.until("the panel of BE-05", "document.querySelector('#panel .cid')?.textContent === 'BE-05'")
        self.page.js("[...document.querySelectorAll('#panel button')].find(b => b.textContent.includes('PO/TL Grill me')).click()")
        self.until("the definition dialog", "!!document.querySelector('dialog textarea[name=outcome]')")
        self.page.js("[...document.querySelectorAll('dialog button')].find(b => b.textContent.includes('Ask the product owner')).click()")
        self.until("the questions", "!!document.querySelector('dialog .qa-item')", timeout=30)
        self.page.js("[...document.querySelectorAll('dialog button')].find(b => b.textContent.includes('Reformulate')).click()")
        self.until("the draft", "document.querySelector('dialog textarea[name=outcome]').value === 'Outcome from the agent'", timeout=30)
        self.assertEqual(self.page.js("document.querySelector('dialog textarea[name=outcome]').value"), "Outcome from the agent")
        self.assertEqual(self.page.js("document.querySelector('dialog textarea[name=acceptance]').value"), "Acceptance from the agent")
        self.assertEqual(self.page.logged(), [])

    def test_the_architect_and_the_senior_talk_in_the_plan_dialog(self):
        self.load("#FE-04")
        self.until("the panel of FE-04", "document.querySelector('#panel .cid')?.textContent === 'FE-04'")
        self.page.js("[...document.querySelectorAll('#panel button')].find(b => b.textContent.includes('Architect/Senior Grill me')).click()")
        self.until("the plan dialog", "!!document.querySelector('dialog textarea[name=plan]')")
        self.assertTrue(self.page.js("!!document.querySelector('dialog input[name=design]')"))
        self.page.js("document.querySelector('dialog .assist-bar button').click()")
        self.use_the_draft()
        speakers = self.page.js("[...document.querySelectorAll('dialog .assist-msg > summary > b')].map(b => b.textContent)")
        self.assertEqual(speakers, ["Architect", "Senior developer", "Architect"])
        self.assertEqual(self.page.js("document.querySelector('dialog .assist-ask > b').textContent"), "1 question for you")
        self.assertFalse(self.page.js("document.querySelector('dialog .assist-msg').open"))
        self.assertEqual(self.page.js("document.querySelector('dialog textarea[name=plan]').value"), "Final plan from the conversation.")
        # Each question offers its likely answers and a last box for anything else.
        self.assertEqual(self.page.js("[...document.querySelectorAll('dialog .qa-item .qa-chips button')].map(b => b.textContent)"), ["All", "Voltrega"])
        self.assertIn("Something else", self.page.js("document.querySelector('dialog .qa-item input').placeholder"))
        self.page.js("[...document.querySelectorAll('dialog .qa-item .qa-chips button')].find(b => b.textContent === 'Voltrega').click()")
        self.page.js("document.querySelector('dialog .assist-bar button').click()")
        self.until("the plan to follow the answer", "document.querySelector('dialog textarea[name=plan]').value === 'Plan for Voltrega only.'", timeout=30)
        self.assertIn("Which clubs first?", self.page.js("document.querySelector('dialog .assist-questions').innerText"))
        self.assertEqual(self.page.logged(), [])

    def test_the_human_approves_the_design_a_task_waits_for(self):
        def waiting(board):
            task = kanvas.find_task(board, "BE-03")
            task["state"] = "doing"
            task["work"] = {"by": "human", "plan": "Build it", "design": {
                "status": "pending", "text": "## Goal\nA filter\n\n[ city v ]", "by": "designer", "round": 1}}
        kanvas.mutate(self.path, waiting)
        self.load("#BE-03")
        self.until("the design of BE-03", "document.querySelector('#panel').textContent.includes('UI/UX design')")
        panel = self.page.js("document.querySelector('#panel').textContent")
        self.assertIn("A filter", panel)
        self.assertIn("waiting for you", panel)
        self.page.js("[...document.querySelectorAll('#panel button')].find((b) => b.textContent.includes('Approve design')).click()")
        self.wait_for("the approved design in kanvas.json", lambda: kanvas.find_task(
            kanvas.load_board(self.path), "BE-03")["work"]["design"]["status"] == "approved")
        self.assertEqual(self.page.logged(), [])

    def test_the_factory_look_is_the_default_and_plain_brings_the_old_one_back(self):
        self.load()
        self.assertEqual(self.page.js("document.documentElement.dataset.skin + '/' + document.documentElement.dataset.theme"), "factory/dark")
        self.page.js("localStorage.setItem('kanvas:prefs', JSON.stringify({ skin: 'plain', theme: 'light' }))")
        self.load()
        self.assertEqual(self.page.js("document.documentElement.dataset.skin + '/' + document.documentElement.dataset.theme"), "plain/light")
        self.assertEqual(self.page.logged(), [])

    def test_the_factory_floor_puts_each_task_on_its_station_and_the_press_turns_when_an_agent_works(self):
        def staged(board):
            busy = kanvas.find_task(board, "BE-03")
            busy["state"] = "doing"
            busy["work"] = {"by": "human", "plan": "Build it", "busy": {"by": "designer", "at": kanvas.now_iso()}}
            waiting = kanvas.find_task(board, "BE-05")
            waiting["state"] = "review"
            waiting["review"] = {"agent": {"status": "approved"}, "manual": {"status": "pending"}}
        kanvas.mutate(self.path, staged)
        self.load()
        self.page.js("document.querySelector('#view-plant').click()")
        self.until("the stations", "document.querySelectorAll('#plant .station').length === 6")
        station = lambda state, task: self.page.js(f"!!document.querySelector('#plant .st-{state} .crate[data-id=\"{task}\"]')")
        self.assertTrue(station("doing", "BE-03"))
        self.assertTrue(station("review", "BE-05"))
        self.assertTrue(self.page.js("!!document.querySelector('#plant .st-doing.running')"))
        self.assertFalse(self.page.js("!!document.querySelector('#plant .st-review.running')"))
        self.assertTrue(self.page.js("!!document.querySelector('#plant .belt.flow')"))
        self.assertTrue(self.page.js("!!document.querySelector('#plant .crate[data-id=\"BE-03\"] .lamp.on-green')"))
        self.assertIn("Waits for your review", self.page.js("document.querySelector('#plant .crate[data-id=\"BE-05\"]').textContent"))
        gauges = self.page.js("[...document.querySelectorAll('#plant .gauge')].map((g) => g.getAttribute('aria-label')).join('|')")
        self.assertIn("Working: 1", gauges)
        self.assertIn("Waiting for you: 1", gauges)
        self.page.js("document.querySelector('#plant .crate[data-id=\"BE-05\"]').click()")
        self.until("the panel", "document.querySelector('#panel .cid')?.textContent === 'BE-05'")
        self.assertEqual(self.page.logged(), [])

    def test_the_siren_switch_is_kept_and_nothing_sounds_by_itself(self):
        self.load()
        self.page.js("document.querySelector('#view-plant').click()")
        self.until("the siren", "!!document.querySelector('#plant .plant-siren')")
        self.assertIn("Siren off", self.page.js("document.querySelector('#plant .plant-siren').textContent"))
        self.page.js("document.querySelector('#plant .plant-siren').click()")
        self.until("the siren on", "document.querySelector('#plant .plant-siren').textContent.includes('Siren on')")
        self.assertTrue(self.page.js("JSON.parse(localStorage.getItem('kanvas:prefs')).siren === true"))
        self.assertEqual(self.page.logged(), [])

    def test_the_design_timeline_shows_each_comment_and_the_proposal_that_answers_it(self):
        def talked(board):
            task = kanvas.find_task(board, "BE-03")
            task["state"] = "doing"
            task["work"] = {"by": "human", "plan": "Build it", "design": {
                "status": "pending", "text": "v2 words", "by": "designer", "round": 2, "at": "2026-11-05T10:05:00Z", "history": [
                    {"type": "proposal", "round": 1, "at": "2026-11-05T10:00:00Z", "by": "designer", "text": "v1 words"},
                    {"type": "feedback", "round": 1, "at": "2026-11-05T10:02:00Z", "by": "human", "text": "make the chips bigger"},
                    {"type": "proposal", "round": 2, "at": "2026-11-05T10:05:00Z", "by": "designer", "text": "v2 words"}]}}
        kanvas.mutate(self.path, talked)
        self.load("#BE-03")
        self.until("the timeline", "!!document.querySelector('#panel .tl')")
        text = self.page.js("document.querySelector('#panel .tl').textContent")
        self.assertLess(text.index("v1"), text.index("make the chips bigger"))
        self.assertLess(text.index("make the chips bigger"), text.index("Answers your comment on v1"))
        self.assertIn("Waiting for you", text)
        self.assertEqual(self.page.logged(), [])

    def test_the_design_studio_shows_the_three_devices_and_the_answer_without_stray_text(self):
        page = "<!doctype html><html><body><p>Mockup</p></body></html>"

        def designed(board):
            task = kanvas.find_task(board, "BE-03")
            task["state"] = "doing"
            task["work"] = {"by": "human", "plan": "Build it", "design": {
                "status": "pending", "text": "words", "mockup": page, "by": "designer", "round": 1, "at": "2026-11-05T10:00:00Z"}}
        kanvas.mutate(self.path, designed)
        self.load("#BE-03")
        self.until("the design card", "!!document.querySelector('#panel .design-open')")
        self.page.js("document.querySelector('#panel .design-open').click()")
        self.until("the studio", "document.querySelectorAll('.studio .device-frame').length === 3")
        bar = self.page.js("document.querySelector('.studio-bar').textContent")
        self.assertNotIn("null", bar)
        self.assertIn("Approve design", bar)
        self.assertIn("Ask for changes", bar)
        self.assertEqual(self.page.js("[...document.querySelectorAll('.studio iframe')].map((f) => f.getAttribute('sandbox')).join()"),
                         "allow-scripts,allow-scripts,allow-scripts")
        self.page.js("[...document.querySelectorAll('.studio .seg button')].find((b) => b.textContent === 'iPad').click()")
        self.until("one device", "document.querySelectorAll('.studio .device-frame').length === 1")
        self.assertEqual(self.page.logged(), [])

    def test_the_board_menu_connects_telegram_with_a_token_field_and_nothing_secret_shown(self):
        self.load()
        self.until("the board menu", "!!document.querySelector('button.board-title')")
        self.page.js("document.querySelector('button.board-title').click()")
        self.until("the menu item", "[...document.querySelectorAll('[role=menuitem]')].some((i) => i.textContent.includes('Connect Telegram'))")
        self.page.js("[...document.querySelectorAll('[role=menuitem]')].find((i) => i.textContent.includes('Connect Telegram')).click()")
        self.until("the dialog", "!!document.querySelector('dialog.dlg[open] input[name=token]')")
        dialog = self.page.js("document.querySelector('dialog.dlg[open]').textContent")
        self.assertIn("@BotFather", dialog)
        self.assertIn("/newbot", dialog)
        self.assertEqual(self.page.js("document.querySelector('dialog.dlg[open] input[name=token]').type"), "password")
        self.page.js("document.querySelector('dialog.dlg[open] input[name=token]').value = 'nope'; "
                     "[...document.querySelectorAll('dialog.dlg[open] button')].find((b) => b.textContent === 'Connect').click()")
        self.until("the refusal", "document.querySelector('dialog.dlg[open] .phone-error')?.textContent.includes('bot token')")
        # The 400 for the made-up token is the one thing the page may log.
        self.assertEqual([m for m in self.page.logged() if "400" not in m], [])

    def test_the_studio_shows_what_was_built_next_to_the_design(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
        folder = os.path.join(self.dir, "demo", ".kanvas", "evidence", "BE-03", "final")
        os.makedirs(folder)
        for name in ("mobile", "ipad", "web"):
            with open(os.path.join(folder, f"design-{name}.png"), "wb") as f:
                f.write(png)

        def built(board):
            task = kanvas.find_task(board, "BE-03")
            task["state"] = "review"
            task["work"] = {"by": "human", "plan": "Build it", "design": {
                "status": "approved", "text": "words", "mockup": "<!doctype html><html><body>m</body></html>", "round": 1}}
        kanvas.mutate(self.path, built)
        self.load("#BE-03")
        self.until("the design card", "!!document.querySelector('#panel .design-open')")
        self.page.js("document.querySelector('#panel .design-open').click()")
        self.until("what was built", "document.querySelectorAll('.studio .device-frame img').length === 3")
        self.assertIn("what was built", self.page.js("document.querySelector('.studio-title').textContent"))
        self.page.js("[...document.querySelectorAll('.studio [data-source]')].find((b) => b.dataset.source === 'mockup').click()")
        self.until("the mockup again", "document.querySelectorAll('.studio .device-frame iframe').length === 3")
        self.assertEqual(self.page.logged(), [])

    def test_a_task_an_agent_is_working_on_can_only_be_looked_at(self):
        def busy(board):
            task = kanvas.find_task(board, "BE-03")
            task["state"] = "doing"
            task["work"] = {"by": "human", "plan": "Build it", "busy": {"by": "reviewer", "at": kanvas.now_iso()}}
        kanvas.mutate(self.path, busy)
        self.load("#BE-03")
        self.until("the notice", "!!document.querySelector('#panel .busy-note')")
        self.assertIn("reviewer is working", self.page.js("document.querySelector('#panel .busy-note').textContent"))
        self.assertFalse(self.page.js("!!document.querySelector('#panel .note-form')"))
        self.assertFalse(self.page.js("[...document.querySelectorAll('#panel button')].some((b) => /Send to review|Pause/.test(b.textContent))"))
        before = kanvas.find_task(kanvas.load_board(self.path), "BE-03")["state"]
        self.page.js("document.querySelector('[data-id=\"BE-03\"]').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 50, clientY: 50 }))")
        time.sleep(0.3)
        self.assertEqual(kanvas.find_task(kanvas.load_board(self.path), "BE-03")["state"], before)

    def test_a_task_the_agent_builds_goes_to_review_by_itself_and_a_failed_run_has_retry(self):
        def automated(status):
            def op(board):
                board["automation"] = {"repositories": [{"id": "front", "name": "front", "path": self.dir}]}
                task = kanvas.find_task(board, "BE-03")
                task["state"] = "doing"
                task["work"] = {"by": "human", "plan": "Build it", "repositories": ["front"],
                                "automation": {"status": status, "round": 1, "updated": kanvas.now_iso()}}
            return op
        buttons = "[...document.querySelectorAll('#panel button')].map((b) => b.textContent.trim())"
        kanvas.mutate(self.path, automated("completed"))
        self.load("#BE-03")
        self.until("the panel", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        self.assertFalse(self.page.js(f"{buttons}.some((b) => /Send to review|Retry/.test(b))"))
        kanvas.mutate(self.path, automated("failed"))
        self.load("#BE-03")
        self.until("Retry", f"{buttons}.some((b) => b === 'Retry')")
        self.assertTrue(self.page.js(f"{buttons}.some((b) => /finished it by hand/.test(b))"))
        self.page.js("[...document.querySelectorAll('#panel button')].find((b) => b.textContent.trim() === 'Retry').click()")
        self.until("the retry", "true", timeout=5)
        for _ in range(30):
            time.sleep(0.2)
            if "automation" not in (kanvas.find_task(kanvas.load_board(self.path), "BE-03")["work"] or {}) or                     kanvas.find_task(kanvas.load_board(self.path), "BE-03")["work"]["automation"].get("status") != "failed":
                break
        run = kanvas.find_task(kanvas.load_board(self.path), "BE-03")["work"].get("automation", {})
        self.assertNotEqual(run.get("status"), "failed")
        self.assertEqual(run.get("round"), 1)
        self.assertEqual(self.page.logged(), [])

    def test_the_earlier_notes_start_with_the_newest(self):
        def notes(board):
            task = kanvas.find_task(board, "BE-03")
            task["notes"] = [{"at": f"2026-11-0{i}T10:00:00Z", "by": "human", "text": f"note {i}"} for i in range(1, 6)]
        kanvas.mutate(self.path, notes)
        self.load("#BE-03")
        self.until("the notes", "document.querySelectorAll('#panel .notes .note-text').length >= 3")
        texts = self.page.js("[...document.querySelectorAll('#panel .notes .note-text')].map((n) => n.textContent.trim())")
        self.assertEqual(texts, sorted(texts, reverse=True))

    def test_sending_a_task_back_starts_a_new_count_of_rounds_and_lifts_the_round_limit(self):
        def stopped(board):
            task = kanvas.find_task(board, "BE-03")
            task["state"] = "review"
            task["review"] = {"agent": {"status": "approved"}, "manual": {"status": "pending"}}
            task["work"] = {"by": "human", "plan": "Build it", "repositories": ["front"], "automation": {
                "status": "failed", "round": 3, "cycle_start": 1, "since": "2026-11-02T10:00:00Z",
                "summary": "Stopped after 3 rounds (automation.max_rounds). Continue by hand."}}
        kanvas.mutate(self.path, stopped)
        self.load("#BE-03")
        self.until("the panel", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        self.page.js("[...document.querySelectorAll('#panel button')].find((b) => b.textContent.trim() === 'Send back').click()")
        self.until("the dialog", "!!document.querySelector('dialog textarea[name=text]')")
        self.page.js("document.querySelector('dialog textarea[name=text]').value = 'Please also fix the empty case'")
        self.page.js("[...document.querySelectorAll('dialog button')].find((b) => b.textContent.trim() === 'Send back').click()")
        for _ in range(30):
            time.sleep(0.2)
            task = kanvas.find_task(kanvas.load_board(self.path), "BE-03")
            if task["state"] == "doing":
                break
        run = task["work"]["automation"]
        self.assertEqual((task["state"], run.get("status"), run["round"], run["cycle_start"]), ("doing", None, 3, 3))

    def test_the_description_definition_and_plan_fold_with_one_click_and_the_pull_requests_sit_under_the_status(self):
        def work(board):
            task = kanvas.find_task(board, "BE-03")
            task["state"] = "doing"
            task["desc"] = "A description to read."
            task["definition"] = {"outcome": "An outcome", "needs": "n", "acceptance": "a", "approved": True}
            task["work"] = {"by": "human", "plan": "The plan to read", "automation": {"status": "completed", "round": 1, "pull_requests": [
                {"id": "front", "name": "Front", "branch": "feature/BE-03", "url": "https://github.com/a/b/pull/1"}]}}
        kanvas.mutate(self.path, work)
        self.load("#BE-03")
        self.until("the panel", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        self.assertTrue(self.page.js("document.querySelector('#panel .now').nextElementSibling.classList.contains('pr-strip')"))
        self.assertIn("pull/1", self.page.js("document.querySelector('#panel .pr-strip a').href"))
        visible = "(text) => [...document.querySelectorAll('#panel .md, #panel .p-sec > div')].some((e) => e.textContent.includes(text) && !e.hidden && e.offsetParent !== null)"
        for text, head in (("A description to read.", "Description"), ("An outcome", "Product definition"), ("The plan to read", "Construction plan")):
            self.assertTrue(self.page.js(f"({visible})('{text}')"), text)
            self.page.js(f"[...document.querySelectorAll('#panel .section-head.foldable')].find((h) => h.textContent.startsWith('{head}')).click()")
            self.assertFalse(self.page.js(f"({visible})('{text}')"), f"{head} should be folded")
        self.load("#BE-03")
        self.until("the panel again", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        self.assertFalse(self.page.js(f"({visible})('The plan to read')"))
        self.page.js("[...document.querySelectorAll('#panel .section-head.foldable')].find((h) => h.textContent.startsWith('Construction plan')).click()")
        self.assertTrue(self.page.js(f"({visible})('The plan to read')"))
        self.assertEqual(self.page.logged(), [])

    def test_the_one_thing_allowed_on_a_busy_task_is_asking_the_agent_to_stop(self):
        def busy(board):
            task = kanvas.find_task(board, "BE-03")
            task["state"] = "doing"
            task["work"] = {"by": "human", "plan": "Build it", "busy": {"by": "designer", "at": kanvas.now_iso()}}
        kanvas.mutate(self.path, busy)
        self.load("#BE-03")
        # The panel is drawn again as the page learns more about the task: wait for the button and click it exactly once.
        self.until("the Stop button", "(() => { const b = document.querySelector('#panel .busy-note button'); if (!b) return false; b.click(); return true; })()")
        self.until("the dialog", "!!document.querySelector('dialog.dlg[open]')")
        self.page.js("document.querySelector('dialog.dlg[open] button[value=ok]').click()")
        self.wait_for("the stop request in kanvas.json", lambda: bool(kanvas.find_task(
            kanvas.load_board(self.path), "BE-03")["work"].get("stop")),
            details=lambda: f"The page shows {self.page.js('document.body.innerText.slice(-600)')!r} and logged {self.page.errors}")
        self.assertEqual(self.page.logged(), [])

    def test_an_old_mark_does_not_lock_a_task(self):
        def stale(board):
            task = kanvas.find_task(board, "BE-03")
            task["state"] = "doing"
            task["work"] = {"by": "human", "plan": "Build it", "busy": {"by": "reviewer", "at": "2026-01-01T00:00:00Z"}}
        kanvas.mutate(self.path, stale)
        self.load("#BE-03")
        self.until("the panel", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        self.assertFalse(self.page.js("!!document.querySelector('#panel .busy-note')"))
        self.assertTrue(self.page.js("!!document.querySelector('#panel .note-form')"))

    def test_approving_the_manual_review_needs_no_comment(self):
        def reviewing(board):
            task = kanvas.find_task(board, "BE-03")
            task["state"] = "review"
            task["work"] = {"by": "human", "plan": "Build it"}
            task["review"] = {"agent": {"status": "approved"}, "manual": {"status": "pending"}}
        kanvas.mutate(self.path, reviewing)
        self.load("#BE-03")
        self.until("the panel", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        self.page.js("[...document.querySelectorAll('#panel button')].find((b) => b.textContent.includes('Approve manual review')).click()")
        self.until("the dialog", "!!document.querySelector('dialog.dlg[open]')")
        self.page.js("document.querySelector('dialog.dlg[open] button[value=ok]').click()")
        self.wait_for("the manual review in kanvas.json", lambda: kanvas.find_task(
            kanvas.load_board(self.path), "BE-03")["review"]["manual"]["status"] == "approved")
        self.assertEqual(self.page.logged(), [])

    def test_a_task_without_evidence_shows_no_evidence_section(self):
        self.load("#BE-03")
        self.until("the panel of BE-03", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        time.sleep(0.5)
        self.assertFalse(self.page.js("!!document.querySelector('#panel .ev-clip, #panel .ev-grid')"))
        self.assertNotIn("Evidence", self.page.js("document.querySelector('#panel').innerText.replace('Delivery evidence', '')"))

    def test_a_failed_save_gives_the_typed_note_back(self):
        self.load("#BE-03")
        self.until("the panel of BE-03", "document.querySelector('#panel .cid')?.textContent === 'BE-03'")
        self.assertEqual(self.page.logged(), [])
        before = self.notes("BE-03")

        refusal = base64.b64encode(json.dumps({"error": "the disk is full"}).encode("utf-8")).decode("ascii")

        def refuse_saves(p):
            if p["request"]["method"] == "PUT":
                self.page.post("Fetch.fulfillRequest", requestId=p["requestId"], responseCode=500, body=refusal,
                               responseHeaders=[{"name": "Content-Type", "value": "application/json"}])
            else:
                self.page.post("Fetch.continueRequest", requestId=p["requestId"])
        self.page.on("Fetch.requestPaused", refuse_saves)
        self.page.send("Fetch.enable", patterns=[{"urlPattern": "*/api/board"}])
        note = "A note typed while saving fails."
        self.type_note(note)
        self.until("the note back in its box", f"{NOTE_BOX}.value === {json.dumps(note)}")
        self.assertIn("Not saved: the disk is full", self.page.js("document.querySelector('#toasts').innerText"))
        self.assertEqual(self.notes("BE-03"), before)
        self.page.send("Fetch.disable")
        self.page.js(ADD_NOTE)
        self.wait_for("the note in kanvas.json once saving works", lambda: self.notes("BE-03") == before + [note])
        # Stopping the server before its answer arrives would leave the page unsure the note was saved, so it keeps it.
        self.until("the saved note on the page", f"document.querySelector('#panel .p-body').innerText.includes({json.dumps(note)}) && {NOTE_BOX}.value === ''")

        stop(self.server)
        self.until("the lost connection banner", "!document.querySelector('#lost').hidden")
        lost = "A note typed while Factory is stopped."
        self.type_note(lost)
        self.until("the note back in its box", f"{NOTE_BOX}.value === {json.dumps(lost)}")
        self.assertIn("Not saved", self.page.js("document.querySelector('#toasts').innerText"))
        self.assertEqual(self.notes("BE-03"), before + [note])
        self.assertEqual([e for e in self.page.logged() if not e.startswith("network: ")], [])


if __name__ == "__main__":
    unittest.main()
