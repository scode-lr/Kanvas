<h1>
<picture>
<source media="(prefers-color-scheme: dark)" srcset="img/kanvas-dark.svg">
<img src="img/kanvas.svg" alt="Kanvas" height="56">
</picture>
</h1>

A task board you share with your AI coding agents. Agents plan and report through a small CLI; you approve, steer and review in an interactive board in your browser.

One JSON file, one Python script, one HTML page. No accounts, no build step, no dependencies.

![The dependency graph of a project that trains a drone to fly itself: simulation, training, evaluation and flight tests](img/graph.png)

## Why

AI agents do their best work in a terminal, but a project needs a plan that outlives a single chat: what is left, what depends on what, what was decided and why. Kanvas keeps that plan in `kanvas.json` next to your code, where you and any number of agents work on it together.

- **Delivery, not wishful statuses.** A task needs a product definition before To do, a technical plan before Doing, two review gates before Done, and deployment evidence before Production. YOLO automates attention, never those gates.
- **Agents cannot break the board.** They change it through `kanvas.py`, which enforces the workflow and rejects invalid moves with a clear message.
- **Live.** Agent changes appear in the browser within a second, and your edits are on disk immediately for the agent's next command.
- **Agent-agnostic.** Claude Code, Codex, Cursor, Gemini CLI, OpenCode, GitHub Copilot, or anything that runs shell commands. Use several at once; notes record who did what.
- **Git-friendly.** The board is plain, stably formatted JSON. Commit it with the code and your history shows how the plan evolved.

## Quick start

### The easy way: let your agent set it up

The easiest way to start is just to open Claude Code, Codex, Cursor, Gemini CLI or OpenCode in your project's folder and paste into the chat:

> Set up Kanvas in this project with `uvx --from git+https://github.com/scode-lr/Kanvas kanvas-board init` (https://github.com/scode-lr/Kanvas), then follow `.agents/skills/kanvas/SKILL.md` and plan the work with me on the board.

The agent should then guide you.

### Formal quickstart

Kanvas works on Linux, macOS and Windows. You need a browser and [uv](https://docs.astral.sh/uv/), which installs Kanvas and downloads Python too if your computer has none.

**1. Install uv** (once). On macOS and Linux, run this in a terminal:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Windows, run this in PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then close the terminal and open a new one.

**2. Install Kanvas** (once):

```bash
uv tool install kanvas-board
```

This gives you the `kanvas` command. If the terminal later cannot find it, run `uv tool update-shell` and open a new terminal.

**3. Open the board.** Go to your project's folder and run:

```bash
cd path/to/your-project
kanvas init
kanvas serve
```

`init` creates the board, `kanvas.json`, and installs the agent skill in `.agents/skills/kanvas/`: instructions for your agents and their own copy of Kanvas, so agents need nothing installed. Commit `kanvas.json` and `.agents/` with your project.

Agents that support skills pick the skill up automatically, and `init` creates an `AGENTS.md` that points the others to it. If your project already has an `AGENTS.md`, `init` leaves it untouched and, when the file does not mention the skill yet, prints this line for you to add:

```
Plan and track work on the Kanvas board; follow .agents/skills/kanvas/SKILL.md.
```

`serve` opens the board in your browser. Keep it running while you work; Ctrl+C stops it. Run `kanvas serve` in your other projects too: their boards open in the same Kanvas, each at its own address such as `http://127.0.0.1:8765/my-project/`, and the terminal is free again right away. Switch boards from the menu on the board's name, or with Shift+← and Shift+→, or keep each in a tab of its own.

**4. Ask your agent:**

> Interview me about this project and plan it on the Kanvas board.

The plan arrives as proposals. Approve the ones you want, and tell the agent to start working.

### Other ways to install and update

Try Kanvas without installing it:

```bash
uvx kanvas-board init
uvx kanvas-board serve
```

Install with pipx, or straight from GitHub without PyPI:

```bash
pipx install kanvas-board
uv tool install git+https://github.com/scode-lr/Kanvas
```

Without uv, you need Python 3.8 or newer. [Download](https://github.com/scode-lr/Kanvas/archive/refs/heads/main.zip) or clone this repository and run its `kanvas.py` in your project's folder; from then on, use the project's copy (type `python3` where `python` is missing):

```bash
python path/to/kanvas.py init
python .agents/skills/kanvas/kanvas.py serve
```

To update, run this, then `kanvas init` again in each project to refresh the copy its agents use:

```bash
uv tool upgrade kanvas-board
```

`kanvas status` and `kanvas serve` remind you when a project's copy differs from the installed `kanvas`: they suggest `kanvas init` when the copy is older, and `uv tool upgrade kanvas-board` when it is newer.

## How it works

```mermaid
flowchart LR
    agent["AI agents"] -- "kanvas.py start / finish / propose / note" --> board[("kanvas.json")]
    you["You"] -- "browser" --> page["kanvas.html"]
    page <-- "kanvas serve" --> board
```

Every task moves through six states. A to-do task whose dependencies are unfinished shows as blocked, and becomes ready by itself once they are done.

| State | Color and mark | Who moves it there |
|-------|----------------|--------------------|
| Proposed | purple, dashed circle | An agent proposes it |
| To do | blue, empty circle (gray and struck through while blocked) | PO + TL complete the product Grill me |
| Doing | orange, half-filled circle | Architect + senior complete the technical Grill me and start a worktree |
| Review | pink, three-quarters-filled circle | The implementer finishes; an independent agent and a human review it |
| Done | green, filled circle with a check | The reviewed change is merged to `dev` |
| Production | teal, filled star | The change is merged to `main`/`master` and deployed |

The first Grill me records the outcome, needs and constraints, acceptance criteria and non-goals. The second records the architecture, implementation, tests and rollout plan. `start` also snapshots each supplied Git worktree—repository, path, branch, commit and uncommitted-change flag. Review then has two explicit, independent gates: an agent PR review and a manual review. A dev merge and a production promotion each require their own evidence.

## The board

![Reviewing a finished task](img/review.png)

- **Graph view.** One swimlane per group, laid out by dependency depth, flowing left to right or top to bottom (switch under Display, or press `L`). The automatic layout orders the cards for fewer crossings and straighter arrows, and arrows that skip levels run between cards. A swimlane with three or more finished tasks folds them into one stub, such as "8 done": click it to show them, and again to fold them. Arrows from folded tasks are left out, since a finished dependency holds nothing up, and a task you open from a link, the bell or search shows even when its swimlane is folded. Set the card order to Manual under Display (or press `M`) to order the cards yourself: drag a card up or down within its level, or into another swimlane to change its group, and drag a swimlane by its name to reorder the groups. Your order is saved in `kanvas.json`, and right-clicking empty space offers to start from the automatic order. The spacing slider under Display, or `[` and `]`, compresses or extends the space between dependency levels. Hover or select a task to highlight everything it depends on and everything waiting for it. A dependency that another dependency already implies (C needs A and B, and B needs A) stays in the file but is drawn only while A or C is selected, as a dashed arrow. Each card has a dot on its incoming and outgoing side: drag one onto another card to add a dependency in that direction (cycles are refused). Click an arrow to open or remove the dependency.
- **Board view.** Kanban columns by state. Forward transitions open the required Grill me, review or delivery-evidence dialog; dragging cannot bypass a gate. On a phone, one column fills the screen, and a row above the columns gives every state with its count; tap one to go to its column.
- **New tasks.** Choose what a new task depends on and what it blocks by searching, or with **Pick on graph**: click cards to add or remove them, then press Done (or Enter), or Cancel (or Escape) to keep the earlier choice. Choices that would create a cycle are dimmed, with the reason on hover.
- **Task panel.** Alongside the task and notes, it shows the approved product definition, construction plan and Git worktrees, both review gates, and dev/production delivery evidence.
- **Questions from agents.** When an agent needs your answer to go on, it runs `ask` and asks in its chat. Its card turns from Doing to **Waiting for you** and shows the question, a toast announces it, the bell lists it above the work to review, across every open board, and the browser tab's title counts the agents waiting, as in `(1) My project · Kanvas`. You reply in the agent's chat, and the card goes back to Doing with the agent's next note. If that chat is gone, Dismiss in the task panel clears the question.
- **Right-click menus.** On a card: state actions, a new task that depends on it or blocks it, adding and removing dependencies, copying the ID, deleting. On an arrow (left or right click): the two tasks it connects are highlighted and shown in the menu; click either one to open it, or remove the connection. On empty space: a new task in that group, a new group, or deleting that group (its tasks stay without a group unless you choose to delete them too).
- **Undo and redo.** Ctrl+Z undoes your last change in the browser, Ctrl+Shift+Z or Ctrl+Y redoes it, and the arrows next to New task do the same. On a Mac, these and the page's other Ctrl shortcuts use ⌘, as the page shows. The toast that confirms a change has an Undo button for that change. Changes agents made in the meantime are kept: an undo only touches what your change touched, and refuses if an agent has changed that since. Inside a text field, Ctrl+Z undoes typing as usual.
- **YOLO.** "YOLO up to here…" selects a dependency chain for autonomous attention. It never creates product definitions, approves reviews, merges to dev or promotes to production. The task carries a YOLO tag and covered work a lightning icon; the same menu turns it off.
- **Execution automation.** A board may opt into running tasks with Claude Code by declaring its repositories under `automation`. The product Grill me then selects Front, API or any other configured repository. Moving the task to Doing creates isolated branches and worktrees, launches Claude Code (`claude -p`) with the root documentation and repository instructions, and records its live status and log path. When Claude finishes, Kanvas pushes each branch that has commits, opens a GitHub pull request and moves the task to Review with the pull request links, ready for you to review and merge. Original checkouts are never used as workspaces. Kanvas never merges or deploys automatically.
- **Everything else.** Filter by state, search (press `/`), hide finished work, groups with their own ID prefixes (from the menu on the board name), light, dark or system theme, keyboard shortcuts (press `?`), arrow keys to move from card to card and Enter to move into its panel (Tab goes round the panel, Escape comes back out), and links: `#TR-02` in the URL opens that task.

![The board view in dark mode](img/board-dark.png)

There are three ways to open it:

| | |
|---|---|
| `kanvas serve` | Any browser, live two-way sync. Concurrent changes from agents and the browser merge automatically. |
| Open `kanvas.html` directly | The project has a copy in `.agents/skills/kanvas/`. In Chrome or Edge, choose your `kanvas.json` and the page reads and writes it in place, with live updates. Other browsers edit a copy in the tab; **Save** downloads it. |
| `kanvas export` | A single self-contained, read-only HTML snapshot to share or attach to a pull request. |

## On your phone

Kanvas can open your boards on your phone, or on another computer, through [Tailscale](https://tailscale.com), a free private network between your own devices.

1. Install Tailscale on the computer that runs Kanvas and on your phone ([downloads](https://tailscale.com/download)), and sign in to the same account on both.
2. In the board, choose **Open on your phone…** in the menu on the board's name, then **Open to my phone**.
3. Scan the QR code with the phone's camera.

`kanvas serve --tailscale` opens Kanvas to your phone from the start and prints the QR code in the terminal. Under the QR code are the board's address by the computer's Tailscale name, such as `http://my-computer.tail1234.ts.net:8765/my-project/`, and by its Tailscale IP, in case the name does not resolve. Your other open boards are there too.

Kanvas listens only on this computer and on its Tailscale address, so your home or café network cannot reach it, and it answers only to this computer's own names. It has no password: anyone on your Tailscale network can open and edit your boards. Opening new boards, and opening Kanvas to phones, stay with the computer.

### If the phone cannot connect

The computer's firewall is probably blocking Kanvas. Kanvas uses port 8765, or the next free one up to 8784; the dialog shows which. Allow those ports from Tailscale's addresses (`100.64.0.0/10`) only:

| System | What to do |
|--------|------------|
| Windows | The first time, Windows asks whether Python may communicate on networks: allow it. If you missed that, run in PowerShell as administrator: `New-NetFirewallRule -DisplayName "Kanvas over Tailscale" -Direction Inbound -Protocol TCP -LocalPort 8765-8784 -RemoteAddress 100.64.0.0/10 -Action Allow` |
| macOS | The firewall is off unless you turned it on. If it is on, macOS asks whether Python may accept incoming connections: choose Allow. If you missed that, add Python under System Settings > Network > Firewall > Options. |
| Ubuntu, Debian, Mint (ufw) | `sudo ufw allow in on tailscale0 to any port 8765:8784 proto tcp` |
| Fedora, RHEL (firewalld) | `sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="100.64.0.0/10" port port="8765-8784" protocol="tcp" accept'`, then `sudo firewall-cmd --reload` |
| NixOS | In your configuration: `networking.firewall.interfaces.tailscale0.allowedTCPPortRanges = [ { from = 8765; to = 8784; } ];`, then rebuild |

Then pick **Open on your phone…** again and scan.

### Keep Kanvas running

For the phone to reach your boards, Kanvas has to be running on the computer. Instead of leaving a terminal open, let it start when you log in:

```bash
kanvas service install --tailscale
```

It then runs in the background with the boards you opened before, and opens them to your phone as soon as Tailscale is connected. A board you open later with `kanvas serve` joins it and is remembered too. Leave out `--tailscale` to keep the boards on this computer only. `kanvas service status` says whether it is set up and running, and `kanvas service uninstall` removes it.

This needs Kanvas installed with `uv tool install kanvas-board`, as in [Quick start](#quick-start), so the service has a lasting command to run. On each system it uses the usual mechanism for your own programs, without administrator rights:

| System | What `service install` sets up |
|--------|------------|
| Linux | A systemd user service, `~/.config/systemd/user/kanvas.service`; `systemctl --user status kanvas` and `journalctl --user -u kanvas` show how it is doing. It starts when you log in; to start it at boot, before you log in, run `loginctl enable-linger $USER` once. On NixOS, the same command works. |
| macOS | A LaunchAgent, `~/Library/LaunchAgents/kanvas.plist`, logging to `~/Library/Logs/Kanvas.log` |
| Windows | A Task Scheduler task named `kanvas` that runs at logon without a window |

The browser's **Open to my phone** and **Stop opening to phones** are remembered the same way, so you can also change phone access from the board.

## CLI

You type `kanvas <command>`; `kanvas -h` lists the commands and `kanvas <command> -h` explains one. Agents run the same commands through the project's copy, `python .agents/skills/kanvas/kanvas.py <command>`, so cloud agents and teammates who never installed Kanvas can work on the board too. Messages name the command you typed: `kanvas ...` for the installed command, `python .agents/skills/kanvas/kanvas.py ...` for the project's copy.

| Command | Purpose |
|---------|---------|
| `status` | Overview: doing, waiting for review, ready, blocked, proposed, problems |
| `ready` / `blocked` | What can start now / what waits on what |
| `show ID` | Details, dependencies and notes |
| `list [STATE or GROUP]` | Tasks by group, optionally filtered |
| `start ID -m PLAN [--worktree PATH ...]` | To do to doing after the technical Grill me; records the plan and Git snapshots |
| `finish ID -m SUMMARY` | Doing to review; creates pending agent and manual review gates |
| `agent-review ID (--approve\|--changes) -m FINDINGS` | Independent implementation/PR review; changes return the task to Doing |
| `pause ID [-m NOTE]` | Doing back to to do |
| `note ID TEXT` | Append a note; descriptions stay as the human wrote them |
| `ask ID QUESTION` | Show that the agent waits for your reply in its conversation, until its next note |
| `propose GROUP TITLE [DESC] [--deps ID ...]` | Propose a task |
| `add-group NAME [--prefix XX]` | Create a group |
| `batch` | Propose many groups and tasks from JSON on stdin |
| `depend ID ON [ON ...]` | Add dependencies |
| `edit ID [--title T] [--group G] [--desc D]` | Revise a proposal that is not approved yet |
| `withdraw ID [ID ...]` | Delete proposals that are not approved yet |
| `undepend ID ON [ON ...]` | Remove dependencies from a proposal |
| `check` | Validate the board; exit code 1 on errors |
| `serve [--port N] [--host H] [--tailscale]` | Open the board in the browser; when Kanvas already runs for another project, open the board there and return. In a project without a board, offer to create one first. `--tailscale` also opens the boards to your phone over Tailscale |
| `serve --saved` | Serve only the boards open last time, without a project of its own; what `service` runs |
| `service install [--tailscale]` / `uninstall` / `status` | Start Kanvas when you log in, so your boards are always open; `--tailscale` also opens them to your phone |
| `init [DIR]` | Set up Kanvas in a project, or refresh the agents' copy after an update |
| `import FILE.canvas` | Migrate a Kanvas 0.x Obsidian board |
| `export [FILE.html]` | Write a read-only snapshot |

`init` creates `kanvas.json` and installs the agent skill in `.agents/skills/kanvas/` (a copy of `kanvas.py`, `kanvas.html` and `SKILL.md`), and a copy of `SKILL.md` in `.claude/skills/` for Claude Code, which reads skills only from there. Codex, Cursor, Gemini CLI, OpenCode and GitHub Copilot read `.agents/skills/`. When the project has no `AGENTS.md`, it creates one that points agents to the skill. Run without a folder, it picks the folder that already holds a board (the current folder or one above it), else the top folder of the git repository, else the current folder; `kanvas init .` keeps the current folder, for one board per subproject. Apart from the skill files it installs, it never modifies an existing file.

In a project without a board, `serve` asks whether to create one, then opens it. Without a person at the terminal, it fails with a message naming `kanvas init`.

One Kanvas serves the boards of all your projects. The first `serve` starts it and keeps running; a `serve` in another project finds it, opens its board there and returns. Each board's address is named after its project folder, and `http://127.0.0.1:8765/` lists the open boards. Kanvas remembers that list, and whether it was open to phones, in `boards.json` in your user folder (`~/.local/state/kanvas/` on Linux, `~/Library/Application Support/Kanvas/` on macOS, `%LOCALAPPDATA%\Kanvas\` on Windows), so the next start reopens them. A board whose file is gone is dropped from the list.

Text arguments also accept `--stdin` and `--file PATH`, read as UTF-8 on every platform, which is the reliable way to pass multi-line or non-ASCII text from any shell. Notes and proposals are signed with `--as NAME` or the `KANVAS_AGENT` environment variable. The CLI finds `kanvas.json` in the current directory or any parent; `--board PATH` or `KANVAS_BOARD` point elsewhere.

Writes take a short lock and replace the file atomically, so several agents and the browser can work at the same time without losing changes.

### Automated project execution

Automation is opt-in and runs only while `kanvas serve` (or the login service) is running. Add project-relative repositories to the board:

```json
"automation": {
  "enabled": true,
  "model": "sonnet",
  "project_root": ".",
  "worktrees_root": ".kanvas/worktrees",
  "runs_root": ".kanvas/runs",
  "branch_pattern": "feature/{id}",
  "repositories": [
    { "id": "front", "name": "Frontend", "path": "Frontend", "base": "origin/dev" },
    { "id": "api", "name": "API", "path": "Api", "base": "origin/dev" }
  ]
}
```

The PO/TL Grill me requires at least one repository. After the Architect/Senior plan starts the task, Kanvas creates a branch in each selected repository under `.kanvas/worktrees/<task-id>/`, launches Claude Code in headless mode (`claude -p`) and stores its JSONL log under `.kanvas/runs/`. Branches default to `kanvas/<task-id>-<title>`; set `branch_pattern` to a format such as `feature/{id}` when the project tracks work by task ID. Available fields are `{id}`, `{id_lower}`, `{id_slug}` and `{title_slug}`. Add `.kanvas/` to the project root's `.gitignore`. A successful run enters Review with a pull request per repository the agent changed, based on the repository's `base` (`origin/dev` opens it against `dev`). The agent only commits; Kanvas itself pushes the branch and opens the pull request with the GitHub CLI, so `gh` must be installed and signed in (`gh auth login`) in the environment `kanvas serve` runs in, and the agent never needs GitHub credentials. A run that made no commits, left tracked files uncommitted, or could not push or open the pull request stays in Doing with the exact reason; `kanvas retry ID` runs it again on the same branch and reuses a pull request that is already open.

The task panel also shows the evidence a task left: the screenshots and videos in `.kanvas/evidence/<task-id>/` (PNG, JPEG, GIF, WebP, WebM and MP4, up to three folders deep) appear under *Evidence*, with videos playing in the panel and screenshots opening full size. `kanvas serve` lists and serves them read-only and only from that folder; set `evidence_root` under `automation` to use another one. A task without evidence shows no section, and neither does a page opened from a file or an export.

Which GitHub credentials Kanvas uses is up to you. By default its `git push` and `gh` calls use the ones of the computer it runs on (your SSH key and `gh auth login`). To give Kanvas a token of its own, set `KANVAS_GITHUB_TOKEN` in the environment of `kanvas serve` (and restart it): then every push and pull request call uses only that token, over HTTPS through the GitHub CLI's credential helper, and nothing else. A fine-grained token limited to the repositories with *Contents* and *Pull requests* read and write is enough. The agent never inherits GitHub credentials: `KANVAS_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, their enterprise variants and `SSH_AUTH_SOCK` are removed from its environment, and `git push`, `git merge` and `gh` are refused to it, including with an option first such as `git -C . push`. Kanvas itself never pushes to `dev`, `main` or `master` or to the branch a pull request goes into, and refuses to start a task whose `branch_pattern` names one of them. It never merges: that stays with you.

Runs are built to spend few tokens. Every turn of a run re-reads what the agent started with, so a smaller start makes each turn cheaper. Unless `automation.lean` is `false`, the agent starts with no MCP servers, only the project's own settings and only the tools it is allowed (`--strict-mcp-config`, `--setting-sources project,local` and `--tools`), which cut the start of a typical run from about 41,000 to about 15,000 tokens. The model also matters, so each task runs as an agent role. Three exist without configuration: `implementer` (`sonnet`, effort `medium`) for ordinary work, `complex` (`opus`, effort `high`) and `reviewer` (`haiku`, effort `low`, read-only tools and at most 12 turns). The product Grill me has an *Agent* choice next to the repositories; a task that names no role runs as the implementer, so the strongest model is only used when someone asks for it. Change or add roles under `automation.agents`, each with any of `model`, `effort` (`low`, `medium`, `high`, `xhigh` or `max`), `max_turns`, `max_budget_usd`, `allowed_tools` and `disallowed_tools`, which win over the same keys of `automation`. `kanvas usage` lists the turns, cost and tokens of every run, per task, agent role and model, to check what the choices save.

The dialogs can ask the agents for help, from a button, never on their own. In *New task* and in *PO + TL Grill me*, **Ask the product owner** sharpens the title and description, picks the group, spots a task it duplicates and asks what is missing; for the product definition it also drafts the outcome, needs, acceptance criteria, non-goals, repositories and agent role. In *Architect + Senior Grill me*, **Ask the architect and the senior** starts a short conversation: the architect proposes a construction plan, the senior developer checks it against the code, and the architect settles it with a final plan and the questions only you can answer. The conversation shows as it goes, and nothing changes in the dialog until you press **Use the draft**; you still edit and approve. The agents are the Claude Code agents `product-owner`, `architect` and `senior-dev` of the project's `.claude/agents/`, run read-only (`Read`, `Glob` and `Grep`, no MCP servers) from the project folder, one step at a time, and only for requests that come from the computer running Kanvas; at most two conversations run at once. Set `automation.assist` with `max_turns` (default 10 per step) and `max_budget_usd` to limit them.

When a task reaches Review, Kanvas has an agent review it before you read it, and sends the work back by itself until the pull request is ready. The `reviewer` role (`haiku`, read-only, no shell) gets the task's acceptance criteria, what the implementer reported and the diff of every branch against its base, and answers `approve`, `changes` or `escalate` with at most 8 findings that name a file and a line. Approved: the agent gate of the task passes and the pull request gets a comment. Changes: the findings become a note, the task goes back to Doing and the implementer runs the next round, which is reviewed again. Escalate: the `reviewer-strong` role (`sonnet`) looks again and has to decide. Kanvas stops asking for changes by itself after `automation.review.rounds` in a row (default 2), or when the reviewers cannot decide, and then leaves the findings, as a note and a pull request comment, for you. Every review leaves a comment on the pull request, so what you read there is the summary, the rounds and the review. When you send the task back yourself, with a note or a comment on the pull request, the same loop starts again with a fresh count of rounds: `automation.max_rounds` only stops the loops that run without you. Turn it off with `"review": false` under `automation`, or review a task by hand with `kanvas review ID`. The reviews show in `kanvas usage` as `reviewer` and `reviewer-strong`.

**Design before building.** A task that changes a repository listed in `automation.design.repositories` (default `front`) does not go straight to the implementer. When it reaches Doing with its plan, the `designer` role (`sonnet`, read-only) reads the application's rules, its design decisions and its existing screens and proposes the UI/UX: goal per role, how it lays out on mobile, iPad (portrait and landscape) and the web, the components it reuses, the states, the texts, accessibility and open questions, and a **mockup**: a standalone HTML page with the application's own CSS and sample data. The task panel has a card that opens the **design studio**: the mockup, interactive, on a phone, an iPad and a desktop browser side by side (or one at a time), with the answer, *Approve design* or *Ask for changes*, in its bar. A **timeline** keeps the conversation: each proposal, each comment of yours and the version it answers, the version that answers it and the approval, and any earlier version can be shown again. Approving needs no comment. Kanvas serves it with a policy that gives it no origin and no network. The proposal appears in the task panel as *UI/UX design*, waiting for you: *Approve design* lets the implementer start, with the approved design and mockup in its prompt and the instruction to build exactly that, and *Ask for changes* makes the designer propose again with your feedback, as many times as you need. The same from the terminal with `kanvas design ID` (show), `--approve`, `--changes TEXT` or `--again` (after a failure). A task already sent back for another round keeps its approved design. Turn it off with `"design": false` under `automation`.

**Inbox: ideas and incidents from your phone.** Send a raw idea, or an incident someone reported, and the product owner turns it into a task, so that creating one is no longer a form to fill in. It reads the documentation and the open tasks, names the task, picks the group, drafts the product definition (outcome, needs, acceptance criteria, non-goals, repositories and agent role), spots duplicates, and for a bug adds the steps to reproduce, who is affected, severity, the module that most likely holds it and a workaround. It asks only what it cannot infer, at most three questions; you answer in the same thread, and when the draft is complete you approve the definition with one tap, which makes the task To do. Starting the work (plan, design, running agents) stays with the board, because it costs tokens. A turn takes about 15 seconds and a few cents (Sonnet, `po` role, capped at $0.5 and 10 minutes). Three ways in: `kanvas inbox "the idea"` (with `--task ID` to answer, `--approve ID`, `--drop ID`, and no text to list the inbox); the **inbox page**, `/<board>/inbox`, made for a phone; and a **private Telegram bot**, `kanvas telegram`. The page and its API answer this computer, and phones only with a token of their own: `kanvas inbox --pair` prints the address to open on the phone once (run it in your own terminal; only its hash is kept, and a new pairing replaces the old one). **Connecting the bot takes two steps and no terminal:** create it with @BotFather in Telegram (`/newbot`, which gives a token), then in the board's menu choose *Connect Telegram…* and paste the token. Kanvas shows a six-digit code; send it to your bot from your own Telegram account and that account is paired, with no user id to look up. The code works once and expires in ten minutes, and the bot hears only paired accounts, only in a private chat, and ignores everyone else without a word. The bot then runs inside `kanvas serve`. Kanvas keeps the token in its own folder (`telegram.json`, next to `boards.json`), and agents never inherit it; `KANVAS_TELEGRAM_TOKEN` and `KANVAS_TELEGRAM_USERS` in the environment still work for a bot that runs apart (`kanvas telegram`). In Telegram anything you write is an idea; a reply goes to the idea the product owner is still shaping (touched in the last 30 minutes, with questions open), `/new` forces a new one, photos are kept and given to the product owner, and the buttons *Aprobar* and *Descartar* answer. The board's *PO + TL Grill me* dialog starts from the draft of an idea that came through the inbox.

**MCP.** `kanvas mcp` serves the board and the documentation to Claude over MCP (stdio), for an agent or session that has to look things up: the commercial agent that prices a client's request, a bot, an interactive session. Add it to a project's `.mcp.json` as `{"mcpServers": {"kanvas": {"command": "kanvas", "args": ["mcp"]}}}`, or with `claude mcp add kanvas -- kanvas mcp`. It reads: `kanvas_status`, `kanvas_list`, `kanvas_show`, `kanvas_usage` (what runs cost) and, over the `brain` repository and the `adr` and `docs` folders of each repository, `brain_search`, `brain_read` and `brain_list`; it never reads anything else, whatever path is asked. It writes only what an agent may write in Kanvas anyway, `kanvas_propose` (a proposed task) and `kanvas_note`; approving, starting, reviewing, stopping and merging have no tool, so they stay with the human. `--read-only` leaves the two writers out. Agent runs of the board start without MCP servers (`automation.lean`), so this is for the agents and sessions you start yourself.

**Seeing what was built.** Once a design is approved, the implementer's prompt tells it to look at the real result: `kanvas shots <html file or app URL> --task ID` opens the page in a headless Chrome, Chromium or Edge (`KANVAS_BROWSER` names another) at 390x844 (phone), 820x1180 (iPad) and 1280x800 (desktop) and saves `design-mobile.png`, `design-ipad.png` and `design-web.png` in the task's evidence folder, under `final/`. The agent compares them with the mockup and fixes the differences. In the design studio a *Design / Built* switch puts the screenshots of what was built in the same device frames as the mockup, and the task's evidence shows them too. Like all evidence they are deleted when the task is accepted.

**Stopping an agent.** A stuck run must not keep spending tokens, so every run of Claude Code is watched. It ends by itself when it has run longer than its role allows (`timeout_minutes`: 40 for the implementer, 60 for `complex`, 10 to 15 for the others) or when its log has not grown for `idle_minutes` (10): a command that never returns, a question nobody can answer. Each role also caps what it spends with `max_budget_usd` ($2 for the implementer, $5 for `complex`, $1 for the designer, less for the reviewers and the curator). Set `timeout_minutes` and `idle_minutes` under `automation` for every role, or per role under `automation.agents`. You can stop a run yourself at any time: *Stop* in the notice of the task panel, or `kanvas stop ID`. The agent and everything it started end within seconds, the run is marked failed with the reason, and what it did stays in the worktree, so `retry ID` continues from there. A stop asked for a run that Kanvas no longer has (it restarted) is settled by the dispatcher.

**Look, don't touch.** While an agent works on a task (the designer proposing, the implementer running, the reviewer reviewing) the task panel says so and the board lets you look at it and nothing else: no notes, no moves, no approvals, until the agent finishes. Stop is the one thing you may do. A mark that a crash left behind is cleared when Kanvas starts, and the page ignores one older than the time a run can take.

**Documentation.** When you accept (Done) a task that an agent implemented, Kanvas creates a follow-up task, *Docs: <title>*, for a repository with the id `brain`, if the board has one. It runs as the `curator` role (`haiku`, the project's `brain-curator` agent, tools to read and edit files only) with the summary and the changes of the accepted task, which Kanvas saved beside the run log, and it goes through the same pull request and review as any task, so you read the documentation change before it merges. Turn it off with `"curator": false` under `automation`.

**Iterating.** Comment on the pull request in GitHub, or use Send back in the board (or let an agent reviewer request changes), and the task returns to Doing. Kanvas then runs Claude again as the next round, in the same worktree and branch: the prompt carries the notes written since the last round (the runner's own excluded) and the comments, reviews and inline comments left on the pull request since then. The new commits go to the same pull request, which gets a comment for the round, and the task goes back to Review. `max_rounds` (default 3) stops the loop: the task stays in Doing, marked failed, until you continue by hand or raise the limit and run `retry ID`. The task panel shows the round.

`model` takes an alias such as `haiku`, `sonnet` or `opus` (or a full model name), and `max_turns` and `max_budget_usd` cap a run so an unattended agent cannot spend without limit. The run edits files in the worktrees and may use `allowed_tools`, which defaults to `Bash`, `Edit`, `Write`, `Read`, `Glob` and `Grep`. `disallowed_tools` defaults to `git push`, `git merge` and every `gh` command, so the agent cannot publish or merge anything itself; replace either list in the board to change this. Authentication comes from the environment `kanvas serve` runs in, such as `ANTHROPIC_API_KEY` or a Claude Code login, and `claude` must be on its `PATH`. Kanvas shows Claude's closing summary, turns and cost on the task.

## File format

```json
{
  "kanvas": 1,
  "title": "Learning to fly",
  "groups": [
    {
      "id": "TR",
      "name": "Training"
    }
  ],
  "tasks": [
    {
      "id": "TR-02",
      "title": "Train the first policy",
      "state": "review",
      "group": "TR",
      "deps": ["TR-01", "SM-03"],
      "desc": "- [x] Passes every gate on most random courses\n- [x] Copes with wind up to 8 m/s",
      "notes": [
        {
          "at": "2026-10-25T08:20:00Z",
          "by": "claude",
          "text": "The policy flies the course on its own: 92 of 100 random courses through every gate."
        }
      ]
    }
  ]
}
```

States are `proposed`, `todo`, `doing`, `review`, `done` and `production`; blocked is computed. Optional `definition`, `work`, `review` and `delivery` objects contain the gate evidence. `"yolo": true` selects the task and its dependencies for autonomous attention but does not bypass gates. The CLI and browser preserve unknown fields and write the same canonical formatting, so diffs show only real changes. See [`examples/drone.json`](examples/drone.json) for a complete board.

## Migrating from Kanvas 0.x (Obsidian)

```bash
kanvas init
kanvas import Project.canvas
```

Card colors become states, groups come from the canvas groups, arrows become dependencies, and existing task IDs are kept. The `.canvas` file is left untouched. Once you are happy with the result, delete `Project.canvas`, `canvas-tool.py`, `RULES.md`, the Kanvas instructions in `CLAUDE.md` / `AGENTS.md`, and the `.obsidian/plugins/canvas-watcher` plugin.

## Development

```bash
python -m unittest discover -s tests -v
```

The suite is standard library only; one test also runs the browser's serializer under Node.js when it is installed, to check that both sides write identical files. Contributor notes are in [AGENTS.md](AGENTS.md).

## License

MIT
