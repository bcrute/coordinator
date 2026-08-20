# Codex-plans, Claude-codes coordination workflow

A goal-driven two-agent development loop, plus a small local dashboard for
watching and controlling it. Clone this repository once, then point it at one
or more separate local project repositories to start coordinating
Codex/ChatGPT and Claude Code turns on those projects.

**Status: experimental personal / resume project.** It is not an official
product of, and has no affiliation with, OpenAI or Anthropic. It wraps their
separately installed and separately authenticated CLIs; you are responsible
for your own Codex and Claude Code accounts, credentials, and usage.

Tested on Linux/Unix-like systems with Python 3.11+. It has not been tested
on Windows.

## Prerequisites

Install and authenticate each of these yourself, independently of this
repository:

- **Git**
- **Python 3.11 or newer** (standard library only — no extra packages to
  install for the workflow itself)
- **Codex CLI**, logged in (`codex login` or an API key in the environment)
- **Claude Code CLI**, logged in

## Architecture

1. Codex/ChatGPT owns the overall goal and writes one bounded subgoal at a
   time.
2. Claude Code implements that subgoal and writes a handoff report.
3. Codex reviews the complete diff and evidence, then either requests a
   correction or hands Claude the next subgoal.
4. When every completion criterion is met, Codex writes the repository's
   `done` signal and notifies you.

The durable state lives in the target project's `.coordination/` directory,
so a fresh chat can resume without you restating the project or the last
handoff. A small standard-library-only web app (`web_app.py`) lets you watch
and control that loop from a browser instead of a terminal.

## Quickstart (from a fresh clone)

Run these from the directory where you cloned this repository:

1. **Install the skill and global defaults** (once per machine):

   ```bash
   python3 skills/coordinate-claude-work/scripts/install_user.py
   ```

   This symlinks the versioned skill into `~/.codex/skills` and adds small
   marked blocks to `~/.codex/AGENTS.md` and `~/.claude/CLAUDE.md`, preserving
   any existing content. Restart Codex and Claude Code afterward.

2. **Create your local settings file.** Copy the example, which stays
   untracked and machine-specific:

   ```bash
   cp workflow.example.toml workflow.toml
   ```

   Edit `workflow.toml` and set `repo` to the project you want to coordinate
   (or `.` to serve this clone itself) and `repositories_root` to the
   directory whose sibling Git repositories you want selectable from the
   browser's repository picker.

3. **Run the web app** against your settings file:

   ```bash
   python3 skills/coordinate-claude-work/scripts/web_app.py --config workflow.toml
   ```

4. **Open the dashboard** at the loopback URL printed on startup (default
   `http://127.0.0.1:8765`).

## Onboarding your first repository

If the repository you selected has no `.coordination/` yet, the Monitor view
shows onboarding guidance instead of a workflow summary:

1. Open the **Terminal** view and click **Start** to launch a real,
   interactive `codex -C <repo>` session in the browser.
2. Discuss the project and its overall goal with Codex in that terminal.
3. Ask Codex to begin coordinated work. Codex creates the `.coordination/`
   files from that discussion (it runs the same initializer described in
   `skills/coordinate-claude-work/SKILL.md`).
4. Once `.coordination/` exists, the **Agents** view's watcher **Start**
   control becomes available (a later poll notices the new coordination
   marker automatically), and you can start the automatic watcher that
   relays Claude and Codex turns for you.

See `skills/coordinate-claude-work/SKILL.md` for the full procedure Codex
follows, including watched goal mode, native Claude teams, and the review
standard.

## Settings

`web_app.py --config <file>` reads a TOML file with these optional keys.
Anything you omit uses the program's built-in default, and an explicit
command-line flag always overrides the matching config value.

| Key                 | Default                              | Meaning |
| -------------------- | ------------------------------------ | ------- |
| `repo`               | current working directory            | Project root to serve; must already be a Git repository or already coordination-initialized. |
| `repositories_root`  | the resolved `repo`'s parent directory | Directory whose direct-child Git repositories populate the browser's repository picker (non-recursive scan). |
| `host`               | `127.0.0.1`                          | Bind address. Keep this at loopback unless you provide an authenticated boundary yourself (see warning below). |
| `port`               | `8765`                               | TCP port; `0` picks a free port. |
| `relay_log_lines`    | `200`                                | Number of `runtime/relay.log` lines returned by `/api/state`. |
| `quiet`              | `false`                              | Suppress per-request HTTP logging. |

**Path semantics:** relative `repo` and `repositories_root` values in the
config file resolve against the directory *containing that config file*, not
the directory you launch the command from. `workflow.example.toml` uses `.`
and `..` for exactly this reason.

**Precedence:** command-line flags (`--repo`, `--repositories-root`, `--host`,
`--port`, `--relay-log-lines`, `--quiet`/`--no-quiet`) always win over a value
from `--config`, which in turn wins over the built-in default. See
`workflow.example.toml` for a fully commented starting point.

## Testing

Run the full local suite:

```bash
python3 -m unittest discover -s tests -v
```

Or run a focused subset relevant to the web app and settings:

```bash
python3 -m unittest tests.test_web_settings tests.test_web_workflow_state \
  tests.test_web_terminal_contract tests.test_web_views \
  tests.test_web_repository_picker tests.test_web_repository_switching -v
```

## Continuous integration and publishing

`.github/workflows/ci.yml` runs the full test suite and a compile check on
every push and pull request, on `ubuntu-latest` with read-only permissions,
across a Python 3.11 / 3.12 / 3.13 matrix, plus a pinned Node 24 setup for a
deterministic syntax check of the vendored dashboard app. It installs no
packages and launches no external services or CLIs.

Before pushing this repository's intended public file set to a new public
remote for the first time, work through
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md).

## Limitations, honestly

- This is a **dynamic Python/PTY service, not a static file bundle**. It has
  to keep running (in a terminal or as the optional systemd user service
  described in `docs/SELF_HOSTING.md`) for the dashboard and Codex terminal
  to work.
- The server has **no authentication, TLS, or per-user access control**. It
  binds `127.0.0.1` by default and is intended strictly for the owner's own
  machine. Do not bind a routable/LAN address unless you put an
  authenticated boundary (for example an authenticating reverse proxy) in
  front of it yourself — this repository does not provide one.
- The browser's "Codex session" terminal gives anyone who can load the page
  a full interactive shell into `codex -C <repo>` using your inherited Codex
  login. Treat the loopback port accordingly.
- The automatic watcher's Claude turns use Claude Code's non-interactive
  print mode and support native subagents, not native agent teams; use
  `start_claude_team.py` directly for collaborative team work (see
  `skills/coordinate-claude-work/SKILL.md`).
- This development worktree also contains proprietary and unrelated local
  fixtures — `citadel-main.zip` (proprietary, excluded) and
  `readiness_demo/`/`examples/` plus their tests (an unrelated local demo,
  excluded) — that are intentionally kept out of the public file set by
  `.gitignore` rather than deleted from disk. They are not part of this
  workflow's product surface.
- Windows is not supported.

## Self-hosting

For running this as a persistent local service (foreground operation, logs,
updating, troubleshooting, and an optional systemd user-service setup), see
[`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md).

## License

Released under the MIT License, Copyright (c) 2026 Benjamin Crute. See
[`LICENSE`](LICENSE) for the full text. This remains an experimental personal
project; the license covers the code in this repository and grants no rights
in the third-party material described below.

## Third-party code

The browser terminal vendors [xterm.js and addon-fit](https://github.com/xtermjs/xterm.js)
under `skills/coordinate-claude-work/assets/web/vendor/`. See that project for
its own license and attribution.

## Why both a skill and repository files?

The skill contains the repeatable procedure. `AGENTS.md` gives Codex
persistent project instructions, while `CLAUDE.md` gives Claude persistent
project instructions. `.coordination/` contains changing task state. This
separates stable personal behavior, stable project rules, and per-turn state
instead of forcing one large prompt to do all three jobs.
