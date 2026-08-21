---
name: coordinate-claude-work
description: Coordinate a goal-driven project in which Codex or ChatGPT owns the overall goal, assigns and reviews Claude Code subgoals, and Claude performs all product-code edits. Use when a repository contains `.coordination/`, when the user asks Codex and Claude to communicate through watched files, or when every Claude handoff must be reviewed before Codex assigns the next turn and signals done.
---

# Coordinate Claude Work

Keep one durable project mailbox in `.coordination/`. Act as the planner and
reviewer; make Claude Code the sole product-code writer.

## Initialize a project

If `.coordination/README.md` is absent, run:

```bash
python3.14 <skill-directory>/scripts/init_project.py . --project-name "<name>"
```

The initializer preserves existing `AGENTS.md` and `CLAUDE.md` content and adds
only a marked coordination block. Never reset existing coordination state unless
the user explicitly requests it.

After initialization, ask the user to fill gaps in `.coordination/PROJECT.md`
only when they materially affect the first task. Otherwise infer repository facts
by inspection and record them there.

## Respect ownership

- Codex owns `.coordination/planner/` and `.coordination/reviews/`.
- Claude owns product edits and `.coordination/coder/`.
- The user owns product policy, scope expansion, credentials, purchases,
  destructive external actions, and unresolved tradeoffs.
- Either agent may read every file and run non-mutating inspection or tests.
- Do not edit product files as Codex merely to rescue a failed Claude turn. Report
  the failure or issue a narrower correction turn unless the user changes roles.
- Do not let Claude commit, push, deploy, or mutate external systems unless the
  active assignment explicitly authorizes that action.

## Run one implementation turn

1. Read repository instructions, `.coordination/README.md`,
   `.coordination/PROJECT.md`, the overall goal, current assignment, coder
   status/report, and latest review. Inspect the relevant product state.
2. For new work, write the complete objective and verifiable completion criteria
   to `.coordination/planner/goal.md` with state `active`. Write one bounded
   subgoal to
   `.coordination/planner/current-task.md`. Include a stable task ID, state
   `ready`, objective, scope, exclusions, acceptance criteria, required evidence,
   and allowed external actions. Set the review round to `0` for a new task.
3. Run exactly one Claude handoff:

   ```bash
   python3.14 <skill-directory>/scripts/run_claude_turn.py --repo .
   ```

   The script is a thin adapter around Claude Code print mode and safe `auto`
   permissions. It embeds only `planner/current-task.md` as the authoritative
   coordination packet; Claude Code loads ordinary repository instructions and
   manages its own context, tools, tasks, and agents. The lead defaults to Opus,
   native workers default to Sonnet through `CLAUDE_CODE_SUBAGENT_MODEL`, and a
   generous 40-turn ceiling exists only as a runaway guard. Do not prescribe a
   file-reading sequence or manually mirror Claude's native task list.

   A once-per-second dashboard observes the handoff signal, native Agent events,
   actual CLI token usage, and timers. Generated/output tokens are the headline
   count; new input, cache reads, and cache writes are separate. Repeated streamed
   message IDs are counted once. The installed `claude` CLI uses its own
   configured authentication.
   Do not use a permission-bypass flag. Use `--permission-mode default` when the
   environment or user policy requires explicit project permission rules.
4. Review before issuing any new task. Read Claude's report, inspect the complete
   diff and repository state, and run or independently inspect evidence that can
   falsify the acceptance claims. Treat unrun checks as unrun.
5. Replace `.coordination/reviews/latest.md` with verdict `accepted`,
   `changes_requested`, or `blocked`. Name the task ID, review round, examined
   ref/worktree, findings ordered by severity, commands run, and next action.
6. For `changes_requested`, update the same assignment with the review findings,
   increment the review round, set state `changes_requested`, and run one more
   Claude turn. Keep the same task ID while the objective is unchanged.
7. For an accepted subgoal, either assign the next bounded subgoal with a new task
   ID and review round `0`, or—only when the overall completion criteria are
   satisfied—set the current task to `accepted`, set the overall goal to `done`,
   and write `.coordination/reviews/completion.md`. Notify the user from that
   completion artifact.

Every Claude invocation is one implementation turn and must receive a Codex
review. A turn may contain many internal tool calls and code edits; do not confuse
Claude's internal model turns with coordination handoffs.

## Run watched goal mode

After writing an active overall goal and the first subgoal, run both relays in the
current Codex turn:

```bash
python3.14 <skill-directory>/scripts/watch_coordination.py --repo . --role both
```

Keep the process attached. It launches Claude for a ready subgoal, launches a
fresh non-interactive Codex review when Claude signals `review`, and repeats until
Codex writes `State: done` to `planner/goal.md` or records a blocker. When it exits
done, read `reviews/completion.md` and notify the user.

When attached to a terminal, the watcher uses a persistent btop-style dashboard
showing the overall goal and roadmap, current acceptance contract and activity,
timers, Claude token usage, and observed native Agent workers. It refreshes once
per second. Claude decides whether genuinely independent work warrants up to two
Sonnet subagents; the Opus lead owns integration. The watcher writes agent
subprocess output to `runtime/relay.log`. Use
`--no-dashboard` when line-oriented output is required. A failed or incomplete
agent handoff stops the watcher immediately; inspect and review the coordination
state before any restart.

The review relay also launches the installed `codex` CLI. For a deliberately
unversioned test directory it enables Codex's explicit non-repository mode; real
projects should still be Git worktrees so reviews can inspect reliable diffs.

## Run a native Claude team handoff

Agent teams require an interactive Claude session and do not run under the
automatic watcher's `claude -p` mode. Do not emulate them in the watcher. For a
task where teammates need to communicate or share Claude's native task list, run
the Codex review watcher in one terminal and the native team launcher in another:

```bash
python3.14 <skill-directory>/scripts/watch_coordination.py --repo . --role codex
python3.14 <skill-directory>/scripts/start_claude_team.py --repo .
```

The launcher enables `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, starts an Opus
lead with Sonnet workers in Claude's native interactive interface, and supplies
only the active task packet. Claude owns teammate creation, messages, tasks, and
shutdown. After Claude writes `review` or `blocked`, exit that Claude session;
the Codex watcher performs the review. Start a fresh team session for a later
task. Use teams only when workers need to collaborate; ordinary bounded work
should use the automatic native-subagent path.

To run the two sides independently in separate terminals, use:

```bash
python3.14 <skill-directory>/scripts/watch_coordination.py --repo . --role claude
python3.14 <skill-directory>/scripts/watch_coordination.py --repo . --role codex
```

Do not mix a `both` watcher with side-specific watchers. Watchers only relay file
signals. They do not choose subgoals, judge work, or expand permissions. Runtime
status and locks live in `.coordination/runtime/` and remain untracked.

## Serve the local web app

The owner can watch and drive the automatic relay from a browser. Run the
installed Coordinator application; never copy the web assets into the target project. The
initial `--repo` may be an uninitialized Git repository or a compatible
already-initialized non-Git repository. Either pass flags directly:

```bash
coordinator serve \
  --repo <path-to-project> --repositories-root <path-to-projects-parent-dir>
```

or point at a portable TOML settings file (recommended for repeated use — see
the repository root's `workflow.example.toml` and `README.md` for the full
key reference and precedence rules). The default `auth_mode = "local"` remains
loopback-only; `auth_mode = "oidc"` selects the authenticated ASGI runtime:

```bash
coordinator serve --config workflow.toml
```

`--repo` defaults to the current directory and accepts either an absolute path or
a path relative to the current working directory. `--repositories-root` names
the directory whose direct-child Git repositories (recognized by a `.git` file
or directory, scanned non-recursively) populate the browser's repository
catalog; it defaults to the parent directory of the initial `--repo`.
`--config` supplies the base server fields plus the documented OIDC,
allow-policy, session, state-directory, trusted-host, and trusted-proxy fields;
equivalent command-line flags always override the matching config value, which
in turn overrides the built-in default. Relative `repo`, `repositories_root`,
and `state_dir` paths in a config file resolve against that file's own directory,
not the launch working directory.
The Setup view can create and select a new Git repository as a validated direct
child of `repositories_root`, or install the bundled coordination template into
the active repository. Selecting a catalog entry never resets its existing
`.coordination/` state. For a discussion-led setup, select the repository, open
Terminal, start Codex, describe the project and overall goal, and ask it to
begin coordinated work. Watcher Start remains disabled until setup completes;
the live state stream notices the coordination marker. The app binds
`127.0.0.1:8765` by default; `--port 0` picks a free port and `--relay-log-lines`
sets the `/api/state` log tail. Open `http://127.0.0.1:8765`.

For running this as a persistent local service (foreground operation, updating,
troubleshooting, and an optional systemd user-service example), see the
repository root's `docs/SELF_HOSTING.md`; this skill file stays focused on the
operational procedure, not hosting mechanics.

The page's state feed uses `/api/events` Server-Sent Events. The server emits
changed snapshots and periodic heartbeats, independently of whether Claude or
Codex is doing anything and independently of the visible view.

Instead of one long dashboard, the page is split into focused views,
reachable by nav links or directly by URL hash:

- **Monitor** (`#monitor`, default) — current workflow summary/conclusion and
  live metrics (lead/subagent models, token counts).
- **Terminal** (`#terminal`) — the "Codex session" browser terminal described
  below.
- **Work** (`#work`) — overall goal/progress, roadmap, current task contract,
  and latest review.
- **Agents** (`#agents`) — coder status, observed native Claude subagents, and
  watcher status/controls.
- **Logs** (`#logs`) — the tail of `.coordination/runtime/relay.log`.
- **Activity** (`#activity`) — redacted authentication and control events.
- **Setup** (`#setup`) — bounded repository creation and coordination setup.
- **Sessions** (`#sessions`) — opaque session handles and revocation controls.
- **Diagnostics** (`#diagnostics`) — repository, CLI, state-directory, and
  runtime checks.

Navigation between views is client-side only: it toggles which section is
visible in the browser and never interrupts the live state feed or a
running Codex terminal session — both keep running in the background regardless
of the active view. Loading the page with an empty or unrecognized hash falls
back to Monitor. Monitor's full completion evidence and limitations are
collapsed by default behind an expandable summary, showing only a one-line
conclusion until expanded.

Coder and runtime records from an earlier task or review round stay on disk and
stay visible, but the page marks them out of sync and suppresses them from
"current activity" rather than presenting stale audit trail as live status. The
dashboard only reports the goal as done once a Codex-authored `completion.md`
names the current goal, is itself `done`, and the goal record is also `done`;
that matching Codex completion record — not a coder's own claim — is what makes
"done" authoritative.

The watcher controls are deliberately narrow: start and stop for one fixed
command, the automatic `both` watcher for that repository with `--no-dashboard`.
Outside the explicit Setup action, the web app never selects a subgoal, writes
a verdict, or edits coordination files. A watcher start request is refused
without creating a process while
`runtime/watcher-both.lock` is held, so a watcher already running in a terminal is
untouched. Ctrl-C stops the server and closing it stops only a watcher the app
itself started.

The persistent topbar/header, visible across all views, shows a repository picker
that lists the current catalog entries under `--repositories-root` (every
direct-child Git repository, whether or not it is coordination-initialized
yet, plus the active repository). It
only ever offers a choice among those entries — no free-form path, command,
executable, argument, or shell value is accepted from the browser. Selecting a
different entry stops only this app's own managed Codex terminal session and
watcher for the previously active repository (leaving an externally started
watcher's lock untouched), binds new managers to the selected repository,
resets the browser's terminal attachment, and refreshes every view to the new
repository's state, all without restarting the server.

The "Codex session" panel is a real, interactive terminal (xterm.js, vendored
by the application under `src/coordinator/assets/web/vendor/`) — an actual
PTY-backed frontend, not a log viewer —
attached to one fixed, non-configurable command, `codex -C <repo>`, where
`<repo>` is whichever repository is currently active. There is no request
surface for choosing a different program, argument, or working directory; only
the catalog-based repository switch above can change which repository `<repo>`
is. Coordination work and task prompts still come from the repository's own
`.coordination/` files, not from anything typed in this terminal. It inherits whatever local Codex login/configuration already exists
for the account running `web_app.py`; the web app itself never prompts for or
stores credentials. Start launches that command if it is not already running and
reattaches the browser terminal to its output; Stop ends the running process;
Clear only redraws the browser terminal and never touches the process or its
input. Typed input, output, and debounced resize messages share one bounded,
origin- and CSRF-validated WebSocket; leaving the page (`pagehide`) closes the
socket, resize watcher, and terminal input listener.

Both modes use the same Starlette/Uvicorn application. The default local mode
is unauthenticated and enforced as loopback-only, and
the Codex terminal means anyone who can load it gets a full interactive shell
using your inherited Codex login. The optional OIDC runtime adds native
authentication, default-deny authorization, opaque SQLite sessions, CSRF
enforcement, and audit events, but it still requires the TLS/proxy,
service-account, secret-management, and operational gates in
`docs/SECURITY_ROADMAP.md` before network exposure. Native interactive Claude
teams still require Claude's terminal UI through `start_claude_team.py` and are not launchable or controllable
from the browser.

Run the browser terminal's focused contract tests with:

```bash
python3.14 -m unittest tests.test_web_terminal_contract tests.test_web_views \
  tests.test_web_repository_picker tests.test_web_repository_switching -v
```

## Review standard

Review the implementation, not the report prose. Check:

- the diff stays inside scope and preserves repository instructions;
- each acceptance criterion has direct evidence;
- tests cover the changed behavior and important failure paths;
- security, data migration, compatibility, and deployment claims are supported;
- unrelated user changes remain untouched;
- the coder report accurately names unrun checks and remaining risks.

Request focused corrections with file or behavior references. Do not rewrite the
implementation yourself. If the task is genuinely blocked, record the exact
missing decision or capability in both the review and user handoff.

## Recover across sessions

Treat `.coordination/` as authoritative over chat history. On a new Codex session:

- overall goal `done`: read the completion report and notify the user;
- overall goal `blocked`: surface the blocker;
- `ready` or `changes_requested`: resume with one Claude turn;
- `implementing`: inspect the actual working tree and coder report before deciding
  whether the prior process ended or is still live;
- `review`: perform the pending Codex review;
- subgoal `accepted` with goal still active: assign the next bounded subgoal;
- `blocked`: surface the recorded blocker without inventing authority.

Use the templates as a schema, not a transcript. Archive only concise task and
review artifacts that will help a later session understand accepted work.
