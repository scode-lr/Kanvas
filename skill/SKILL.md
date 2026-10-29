---
name: kanvas
description: Plan and track work on the project's Kanvas board (kanvas.json), which you share with a human who approves and reviews tasks. Use when the user mentions the board, Kanvas, tasks, planning, "what's next", or a task ID such as DV-03.
license: MIT
compatibility: Requires Python 3.8 or later.
---

# Kanvas

The board is `kanvas.json` at the project root. Change it only through the CLI in `.agents/skills/kanvas/`; the CLI enforces the workflow and keeps the file valid:

```bash
python .agents/skills/kanvas/kanvas.py <command>
```

Use `python3` where `python` is missing. Never edit `kanvas.json` by hand. The human watches the board in a browser, where your changes appear within a second. It opens with `kanvas serve` when Kanvas is installed, or `python .agents/skills/kanvas/kanvas.py serve`. When Kanvas already runs for another project, this opens the board there and returns at once; otherwise it keeps running until stopped.

## Workflow

```
proposed -> todo -> doing -> review -> done -> production
```

| State | Moved there by | Meaning |
|-------|----------------|---------|
| proposed | you (`propose`, `batch`) | An idea awaiting a PO + TL product Grill me |
| todo | the human | Product definition approved; **blocked** while a dependency is unfinished |
| doing | you (`start`) | Technical Grill me complete; implementation active in recorded worktrees |
| review | you (`finish`) | Awaiting independent agent review and manual review |
| done | the human | Both reviews passed and the change is merged to `dev` |
| production | the human | Merged to `main`/`master`, deployed and evidenced |

The product Grill me records outcome, needs/constraints, acceptance criteria and non-goals. Before Doing, `start` requires an architecture/senior construction plan and accepts repeatable `--worktree PATH`; it snapshots worktree, branch, commit and dirty state. `finish` always opens both review gates. A different agent identity must run `agent-review --approve`; the human completes manual review and records dev/production evidence in the board.

The human can also ask for changes to a proposal in the board. `status` then marks it `(changes requested: ...)`, and `show ID` has the request in its notes. Revise the proposal with `edit ID`, passing `-m` with what you changed; that note answers the request, and the mark goes.

### YOLO

YOLO selects a task and its dependencies for autonomous attention. It never approves a proposal, invents either Grill me, skips either review, records a merge, or promotes to production. It ends for a task at Review, Done or Production. Only the human can turn it on or off.

## Session protocol

1. **Read the board:** `status`. Tell the user what waits for review, what is ready, what is blocked and what is proposed, and repeat any question still marked `(waiting for the human: ...)`. Revise proposals marked `(changes requested: ...)` as the human asked.
2. **Pick a task:** if the user named one, `show ID`. Otherwise `ready`; when several tasks are ready, ask which one to take.
3. **Start it:** after the technical Grill me, run `start ID -m "Plan: ..." --worktree PATH` (repeat `--worktree` for multiple repositories). Read the definition, description and every note.
4. **Work.** Record findings with `note ID "..."`. Propose any extra work you discover with `propose`, linking it with `--deps`.
5. **Finish:** `finish ID -m "..."` with what changed, how you verified it and what reviewers should check. It enters Review regardless of YOLO.
6. **Independent review:** an agent other than the implementer runs `agent-review ID --approve -m "findings"` or `--changes`. Approval still leaves the manual gate pending. Do not claim Done until dev merge evidence exists, or Production until main/deployment evidence exists.

To stop halfway, run `pause ID -m "Where I left off and why"`.

When you need the human's answer before going on, run `ask ID "the question"`, ask the same question in the conversation, and stop. The board then shows the task waiting for them, with a notification. When they reply, record the answer with `note ID "..."`; the next note ends the wait.

## Planning

When asked to plan, get to know the work first: what is already there (code, documents, the board itself) and what the user wants from the board. Tasks can be large milestones the user checks now and then, or small steps they follow closely; size them to how the user wants to work, and ask when it is unclear. Propose the plan in one batch:

```bash
python .agents/skills/kanvas/kanvas.py batch <<'EOF'
{
  "groups": ["Training", "Flight tests"],
  "tasks": [
    {"title": "Train the first policy", "group": "Training", "desc": "Flies random obstacle courses in the simulator through every gate, in wind up to 8 m/s"},
    {"title": "First autonomous flight", "group": "Flight tests", "deps": ["Train the first policy"]}
  ]
}
EOF
```

`deps` takes task IDs or the titles of earlier tasks in the same batch. A batch is all or nothing. Everything arrives as proposals; ask the human to approve them in the board.

Give each task a `desc` that says what it achieves and what finished means, since the human approves the proposal from it and later reviews the work against it. Groups are areas of work; a group's ID prefix starts every task ID in it (`RS-01`). A new task takes the highest number in its group plus one, so the ID of a rejected or deleted task can come back on the next one: check the title before acting on an old ID.

## Writing for the human

The human decides from what you write on the board: whether to approve a proposal, accept finished work or send it back, and how to answer your question. They read it between other work, without your context. Before you write a description, summary or note, put yourself in their place: what do they need to understand the task and decide, and how fast can they get it?

- Open with what matters for the decision. A summary starts with what now works or changed for them; a proposal, with what it would achieve.
- Make what they should check or try, and anything they must decide, easy to find.
- Leave out what does not help them decide: how you got there, dead ends, every file you touched, numbers that only show diligence. The code and its history keep those.
- Name things as they see them in the product before you name the code behind them, and explain terms they may not know.
- Fit the length and shape to the task. A small fix takes a line or two; a change with risks, trade-offs or choices for them takes the room to set those out clearly. Use lists for items they will go through one by one.
- When unsure, write less: they can ask, and a later `note` can add detail.

## Commands

| Command | Purpose |
|---------|---------|
| `status` | Overview: doing, review, ready, blocked, proposed, problems |
| `ready` | Tasks you can start now |
| `show ID` | Details, dependencies, notes |
| `list [STATE or GROUP]` | Tasks by group; filter by state (`ready`, `blocked`, ...) or group |
| `blocked` | Blocked tasks and what they wait on |
| `start ID -m PLAN [--worktree PATH ...]` | todo -> doing; product definition and plan are required |
| `finish ID -m SUMMARY` | doing -> review; opens both review gates |
| `agent-review ID (--approve\|--changes) -m FINDINGS` | Independent review; changes return it to doing |
| `pause ID [-m NOTE]` | doing -> todo |
| `note ID TEXT` | Append a note |
| `ask ID QUESTION` | Show the human that you wait for their reply in the conversation; the next note ends the wait |
| `propose GROUP TITLE [DESC] [--deps ID ...]` | New proposal; use `-` as GROUP for none |
| `add-group NAME [--prefix XX]` | New group |
| `batch` | Many groups and proposals from JSON on stdin |
| `depend ID ON [ON ...]` | ID waits for the others (cycles are rejected) |
| `edit ID [--title T] [--group G] [--desc D] [-m NOTE]` | Revise a proposal; `-m` answers a request for changes; `--stdin` or `--file` for a long description |
| `withdraw ID [ID ...]` | Delete proposals |
| `undepend ID ON [ON ...]` | Remove dependencies from a proposal |
| `check` | Validate the board |

Notes and proposals record your name, so the human can tell who did what. Sign with the name of your tool: `claude-code`, `cursor`, `codex`, `gemini`, `copilot`, `opencode`, or another short name for it. Pass `--as cursor` to each command, or set `KANVAS_AGENT=cursor` once if your shell keeps environment variables between commands. When several agents of one tool share the board, add what each one does, as in `cursor-docs`. Unsigned notes show as `agent`.

## Long or non-ASCII text

Commands that take text also accept `--stdin` and `--file PATH`, which read UTF-8 on every platform. They give the plan for `start`, the summary for `finish`, the note for `pause` and `note`, the question for `ask`, and the description for `propose` and `edit`; the `-m` note of `edit` is an argument only. Use a heredoc for multi-line summaries:

```bash
python .agents/skills/kanvas/kanvas.py finish TR-02 --stdin <<'EOF'
The policy flies the course on its own: 92 of 100 random courses through every gate, 3 crashes, 5 too slow.
- To see it: `make replay` plays ten flights, the three crashes among them
- All three crashes happen in gusts above 7 m/s: try it on the real drone in calm weather, or train on stronger wind first?
EOF
```

In PowerShell, write the text to a file and pass `--file summary.md`.

## When a command fails

The CLI exits with a non-zero code and an `Error:` line explaining the rule, for example "DV-04 is blocked by unfinished DV-02". Follow the message, or ask the human. Editing the file to get around a rule breaks the human's trust in the board.
