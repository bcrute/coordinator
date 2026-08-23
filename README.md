# Coordinator: frontier-led coding workflows

A goal-driven development loop, plus a local dashboard for
watching and controlling it. Clone this repository once, then point it at one
or more separate local project repositories to start coordinating
Codex planning/review with Claude Code or mini-swe-agent implementation turns.

**Status: self-hosted beta.** It is not an official product of, and has no
affiliation with OpenAI, Anthropic, or the mini-swe-agent project. Provider
runtimes are installed and configured separately; you are responsible for
their accounts, credentials, endpoints, models, and usage.

Tested on Linux/Unix-like systems with Python 3.14. It has not been tested
on Windows.

## Prerequisites

Install and authenticate each of these yourself, independently of this
repository:

- **Git**
- **Python 3.14**
- **Codex CLI**, logged in (`codex login` or an API key in the environment)
- At least one implementation executor: **Claude Code CLI**, logged in, or the
  optional **mini-swe-agent** `mini` command with a configured model

## Architecture

1. Codex/ChatGPT owns the overall goal and writes one bounded subgoal at a
   time.
2. The configured executor implements that subgoal. Its adapter records a handoff.
3. Codex reviews the complete diff and evidence, then either requests a
   correction or assigns the executor the next subgoal.
4. When every completion criterion is met, Codex writes the repository's
   `done` signal and notifies you.

The durable state lives in the target project's `.coordination/` directory,
so a fresh chat can resume without you restating the project or the last
handoff. The `web_app.py` dashboard uses the same Starlette/Uvicorn runtime in
local and authenticated modes, with live state events and a WebSocket-backed
Codex terminal.

## Quickstart (from a fresh clone)

Run these from the directory where you cloned this repository:

1. **Create an environment and install the application dependencies:**

   ```bash
   uv venv --python 3.14
   uv pip install --python .venv/bin/python --editable .
   ```

   If you do not use `uv`, the equivalent is `python3.14 -m venv .venv` followed
   by `.venv/bin/python -m pip install --editable .` (your OS
   may package Python's `venv` support separately).

2. **Install the skill and global defaults** (once per machine):

   ```bash
   .venv/bin/python skills/coordinate-claude-work/scripts/install_user.py
   ```

   This symlinks the versioned skill into `~/.codex/skills` and adds small
   marked blocks to `~/.codex/AGENTS.md` and `~/.claude/CLAUDE.md`, preserving
   any existing content. Restart Codex and Claude Code afterward.

3. **Create your local settings file.** Copy the example, which stays
   untracked and machine-specific:

   ```bash
   cp workflow.example.toml workflow.toml
   ```

   Edit `workflow.toml` and set `repo` to the project you want to coordinate
   (or `.` to serve this clone itself) and `repositories_root` to the
   directory whose sibling Git repositories you want selectable from the
   browser's repository picker.

4. **Run the web app** against your settings file:

   ```bash
   .venv/bin/coordinator serve --config workflow.toml
   ```

   Run `.venv/bin/coordinator doctor --config workflow.toml` for a read-only
   installation, CLI, repository, and coordination preflight.

5. **Open the dashboard** at the loopback URL printed on startup (default
   `http://127.0.0.1:8765`).

## Onboarding your first repository

Use the **Setup** view to create a direct-child Git repository or initialize
the selected repository with the coordination template. Initialization also
adds a narrowly scoped GitHub Actions workflow that validates the coordination
files. If the repository already has Actions workflows, Coordinator lists them
and waits for you to add its workflow alongside them or keep the existing CI
unchanged. It never replaces an existing workflow. The workflow becomes active
when the repository is pushed to GitHub; no GitHub API credential is required.

The equivalent command is `coordinator init <repo> --project-name <name>`.
Use `--github-ci add` or `--github-ci skip` for non-interactive setup when the
repository already has CI.

If you want Codex to shape the files from a project discussion first:

1. Open the **Terminal** view and click **Start** to launch a real,
   interactive `codex -C <repo>` session in the browser.
   Select output and press **Ctrl+C** or **Ctrl+Shift+C** to copy it; with no
   selection, **Ctrl+C** remains the terminal interrupt. **Copy selection** is
   available as an explicit control as well.
2. Discuss the project and its overall goal with Codex in that terminal.
3. Ask Codex to begin coordinated work. Codex creates the `.coordination/`
   files from that discussion (it runs the same initializer described in
   `skills/coordinate-claude-work/SKILL.md`).
4. Once `.coordination/` exists, the live state feed updates the dashboard and
   the **Agents** view's watcher **Start** control becomes available. You can
   then start the automatic watcher that
   relays executor and Codex turns for you.

See `skills/coordinate-claude-work/SKILL.md` for the full procedure Codex
follows, including watched goal mode, native Claude teams, and the review
standard.

The staged path from the current realtime dashboard to the installable,
recoverable, network-ready application is tracked in
[`docs/PRODUCT_ROADMAP.md`](docs/PRODUCT_ROADMAP.md). Architectural decisions are
recorded in the ADR series, including
[`ADR 0003`](docs/adr/0003-professional-application-core.md) and the
[`executor-adapter decision`](docs/adr/0004-executor-adapters-and-mini-swe-agent.md).

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
| `usage_refresh_seconds` | `3600`                            | Server-side refresh interval for cached Codex and Claude rolling-limit usage. Collection does not create model turns. |
| `executor_adapter`   | `claude`                              | Implementation runtime: `claude` or `mini-swe-agent`. |
| `mini_swe_command` / `mini_swe_model` | `mini` / provider configuration | mini-swe-agent executable and optional LiteLLM model name. |
| `mini_swe_config`    | none                                  | Optional base mini-swe-agent YAML. Relative paths resolve beside the Coordinator TOML file. |
| `mini_swe_api_base` / `mini_swe_provider` | none / `openai`      | Optional OpenAI-compatible endpoint and LiteLLM custom-provider name. |
| `mini_swe_api_key_env` | `OPENAI_API_KEY`                   | Name of the inherited environment variable containing an endpoint key; its value is never written to TOML or arguments. |
| `mini_swe_step_limit` / `mini_swe_timeout_seconds` | `12` / `900` | Per-turn model-call and wall-time bounds. |
| `mini_swe_cost_limit` | `0.0`                                | mini-swe-agent cost cap; zero disables cost limiting for a local model with no registered price. |
| `quiet`              | `false`                              | Suppress Uvicorn lifecycle messages; request access logging is disabled in both modes. |
| `auth_mode`          | `local`                              | Both modes use the ASGI runtime; `local` enforces loopback, while `oidc` enables authentication. |
| `oidc_issuer`        | none                                 | Exact Authentik issuer URL. Required in OIDC mode. |
| `oidc_client_id`     | none                                 | Authentik confidential-client identifier. Required in OIDC mode. |
| `oidc_client_secret_env` | `COORDINATOR_OIDC_CLIENT_SECRET` | Environment-variable name holding the client secret; the secret itself never goes in TOML. |
| `external_url`       | none                                 | Canonical external HTTPS origin, without a path. Required in OIDC mode. |
| `allowed_subjects` / `allowed_groups` | empty             | Default-deny identity policy; at least one exact subject or group is required. |
| `groups_claim`       | `groups`                             | ID-token claim containing Authentik groups. |
| `state_dir`          | `$XDG_STATE_HOME/coordinator`, or `~/.local/state/coordinator` | Owner-only SQLite session and audit directory. |
| `session_idle_seconds` / `session_absolute_seconds` | `3600` / `43200` | Server-enforced session lifetimes. |
| `rate_limit_window_seconds` | `60` | Sliding-window duration for in-process abuse controls. |
| `rate_limit_auth_attempts` / `rate_limit_control_attempts` / `rate_limit_terminal_connections` | `30` / `120` / `30` | Per-source/session limits for sign-in, state-changing controls, and terminal attachments. |
| `terminal_enabled`   | `true` in local mode; `false` in OIDC mode | Explicitly enables the interactive command-execution surface. Set it deliberately for a network deployment. |
| `trusted_hosts`      | external URL hostname               | Exact accepted HTTP hostnames; wildcards are refused. |
| `forwarded_allow_ips` | `127.0.0.1`                         | Proxy IP/CIDR values Uvicorn may trust for forwarded scheme/client data; `*` is refused. |

**Path semantics:** relative `repo`, `repositories_root`, `state_dir`, and
`mini_swe_config` values
in the config file resolve against the directory *containing that config file*,
not the directory you launch the command from. `workflow.example.toml` uses `.`
and `..` for the repository paths for exactly this reason.

**Precedence:** every explicit command-line flag wins over its corresponding value
from `--config`, which in turn wins over the built-in default. See
`workflow.example.toml` for a fully commented starting point.

### Managed terminal activity

The Terminal page shows live agents, safely reported models, and background terminals
inside the exact process tree launched by that managed browser-terminal session. Each
start receives a distinct session identifier, so unrelated Codex and Claude instances
running elsewhere on the workstation are never merged into the display. Process names,
PIDs, elapsed time, and OS state come from bounded Linux procfs reads. Models come only
from known model flags, allowlisted model environment values, or the exact Codex thread
record associated with an open rollout file; prompts, arbitrary arguments, and general
environment values are not returned to the browser.

This is observation, not agent configuration. Reusable agent profiles do not yet have a
dashboard editor: provider executables, API endpoints, executor adapters, and model
defaults remain TOML/CLI settings. Process presence also does not claim to know an
agent's internal wait reason; a visible background terminal confirms that the managed
session owns the work, while semantic messages such as “awaiting background terminal”
remain provider-owned until exposed as structured events.

### Provider usage indicators

The topbar shows every percentage-based allowance reported by **Codex** and
**Claude**, along with the account plan. This includes session and weekly windows
plus named model-specific limits such as Fable when the provider returns them.
Spark's separate research-preview allowance is intentionally omitted. Each
provider has columns for the limit and reset time, percentage left,
and projected percentage remaining at reset. The projection linearly extrapolates
the average pace in the current window: red at 0% or below, yellow at 1–20%, and
green above 20%. A negative value shows how far the current pace would exceed the
allowance. A missing reset or window duration displays `—`. Use the
adjacent refresh button for an immediate update. The server performs one shared
refresh per `usage_refresh_seconds` (one hour by default), regardless of the
number of open browser tabs.

If a provider refresh fails after a successful reading, the header keeps that
provider's last known windows and marks them **stale** beside the plan. The tooltip
shows the refresh failure and last-success time. A provider is shown as unavailable
only when Coordinator has no successful value to retain.

These checks do not create model turns: Codex is queried through its local app
server's account-rate-limit method; Claude Code's non-interactive authentication
status is checked before its authenticated subscription-usage endpoint is read.
Claude subscription limits are unavailable for API-key-only logins, and either
provider displays an unavailable state instead of estimating a quota from local
token history. Credentials and access tokens are never returned to the browser
or persisted by Coordinator.

### Historical usage value

The dedicated **Usage** page imports token metadata from native Codex and Claude
session records across the service account, including sessions started outside
Coordinator. Provider tabs show cumulative API-equivalent value over 24 hours,
7 days, 30 days, or all retained history, plus raw token totals, pricing coverage,
and a per-model breakdown. These figures estimate what the same token mix would
cost at published API rates; they are not charges against the subscription.
GPT-5.6 Codex records use OpenAI's Standard short- or long-context rate for each
turn, with the long-context rate applied when the native prompt counter exceeds
272,000 input tokens. Claude records retain the provider's distinct five-minute
and one-hour cache-write rates.

The importer reads only timestamps, model identifiers, message/session identifiers,
and token counters. Prompts, responses, tool arguments, credentials, and repository
contents are neither copied into the owner-only usage database nor returned by the
HTTP API. The first import scans existing native history and may take several
seconds; later imports fingerprint files and revisit only changed logs. Cache writes
retain their native five-minute or one-hour duration when the provider reports it;
the combined cache-write total remains available for adapter compatibility.

Historical usage is provider-neutral. A `UsageHistoryAdapter` supplies a stable ID,
display name, and normalized records. The page builds its tabs from the API response,
so a local or custom adapter appears without frontend changes. Adapters may supply a
native or estimated USD value; records without a valid price remain useful and are
shown as straight token counts rather than assigned an invented value.

### Local/API-backed implementation with mini-swe-agent

Install mini-swe-agent as a separate tool following its
[official CLI documentation](https://mini-swe-agent.com/latest/usage/mini/), then
select it in your ignored `workflow.toml`:

```toml
executor_adapter = "mini-swe-agent"
mini_swe_model = "openai/your-local-model"
mini_swe_api_base = "http://127.0.0.1:8000/v1"
mini_swe_provider = "openai"
mini_swe_api_key_env = "OPENAI_API_KEY"
mini_swe_step_limit = 12
mini_swe_timeout_seconds = 900
```

Export any required endpoint key before starting Coordinator; put only its environment
variable name in TOML. Start the app normally and check `coordinator doctor --config
workflow.toml`. The watcher launches one noninteractive mini-swe-agent turn for each
ready assignment, stores trajectories below `.coordination/runtime/trajectories/`, and
hands the resulting diff back to Codex for review. This first integration is
deliberately single-agent: mini-swe-agent does not create or report nested workers here.

## Authenticated OIDC mode

The authenticated runtime is generic OIDC with Authentik as the intended OpenID
Provider. It uses Authorization Code flow with PKCE `S256`, Authlib discovery and
ID-token validation, an exact callback URL, default-deny subject/group authorization,
opaque SQLite-backed sessions, per-session CSRF tokens, and security headers. OAuth
tokens and the client secret are not returned to the browser or stored in the session
database. Sign out clears the local session first and then uses the provider's
discovered end-session endpoint when one is advertised. Signed OIDC back-channel
logout tokens can revoke matching sessions at `/auth/backchannel-logout`; issuer,
audience, signature, age, event, subject/session, and replay checks are enforced.

To configure it:

1. In Authentik, create an OAuth2/OIDC provider with its recommended per-provider
   issuer mode, a confidential client, an
   asymmetric signing key, the default `profile` scope mapping (for the `groups`
   claim), and the strict redirect URI
   `https://<your-host>/auth/callback`. Assign the application only to the intended
   user or group.
2. Copy `workflow.example.toml` to the ignored `workflow.toml`, set
   `auth_mode = "oidc"`, and fill the issuer, client id, external URL, allow policy,
   state directory, trusted host, and trusted proxy address. Prefer a dedicated
   Authentik group in `allowed_groups`.
3. Put the client secret in the named environment variable, not the TOML file:

   ```bash
   export COORDINATOR_OIDC_CLIENT_SECRET='value-from-authentik'
   ```

4. Keep the application bound to loopback or another proxy-only transport. Terminate
   TLS at the reverse proxy and make the upstream unreachable from other machines.
5. Start the same entrypoint. Both modes use Starlette/Uvicorn, so HTTP,
   event-stream, and WebSocket behavior stays consistent between development
   and deployment.

The security SQLite database holds sessions, consumed logout-token identifiers, and
redacted audit events. Coordination goals, tasks, reviews, relay logs, and repositories
remain file-backed. See
[`docs/adr/0001-authenticated-runtime.md`](docs/adr/0001-authenticated-runtime.md)
for the decision and [`docs/SECURITY_ROADMAP.md`](docs/SECURITY_ROADMAP.md) for the
remaining network-readiness gates.

Version history is in [`CHANGELOG.md`](CHANGELOG.md). Release upgrades, checksum and
attestation verification, database migration, and rollback are documented in
[`docs/UPGRADING.md`](docs/UPGRADING.md).

## Testing

Run the full local suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The branch-coverage workflow, subprocess instrumentation boundary, regression floor,
and risk-based behavior matrix are documented in
[`docs/TESTING.md`](docs/TESTING.md). Coverage is a review aid here: the project does
not require raw 100% line coverage or add implementation-coupled tests merely to
increase a score.

The browser workflow is opt-in locally because it requires Chromium and Firefox
downloads. Chromium covers the complete journey; Firefox independently covers the
terminal disconnect/reconnect contract:

```bash
.venv/bin/python -m playwright install chromium firefox
COORDINATOR_E2E=1 .venv/bin/python -m unittest -v tests.test_web_e2e
```

Or run a focused subset relevant to the web app and settings:

```bash
.venv/bin/python -m unittest tests.test_authenticated_web_app \
  tests.test_web_settings tests.test_web_workflow_state \
  tests.test_web_terminal_contract tests.test_web_views \
  tests.test_web_repository_picker tests.test_web_repository_switching -v
```

## Continuous integration and publishing

`.github/workflows/ci.yml` runs the full test suite and a compile check on
every push and pull request, on `ubuntu-latest` with read-only permissions,
on Python 3.14. It installs the pinned application
and test dependencies, audits the application dependency set for known
vulnerabilities, uses Node 24 for a syntax check, and runs a separate Playwright
browser workflow through the local server: a complete Chromium journey plus a focused
Firefox terminal-reconnect contract. The tests launch no external identity provider
or agent CLI.

Before publishing a release or substantial security-sensitive update, work through
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md). Dated verification from the
current `0.3.0` feature checkpoint is recorded in
[`docs/RELEASE_EVIDENCE.md`](docs/RELEASE_EVIDENCE.md).

## Limitations, honestly

- This is a **dynamic Python/PTY service, not a static file bundle**. It has
  to keep running (in a terminal or as the optional systemd user service
  described in `docs/SELF_HOSTING.md`) for the dashboard and Codex terminal
  to work.
- The default `local` mode has **no authentication, TLS, or per-user access
  control** and refuses non-loopback bind addresses. Its opaque local browser
  session exists for CSRF protection and auditing, not user authentication. OIDC provides the
  application authentication boundary, but TLS termination, proxy isolation,
  Authentik policy, service-account isolation, backups, and operational review are
  still deployment responsibilities.
- The browser's "Codex session" terminal gives anyone who can load the page
  a full interactive shell into `codex -C <repo>` using your inherited Codex
  login. Treat the loopback port accordingly.
- The automatic watcher's Claude turns use Claude Code's non-interactive
  print mode and support native subagents, not native agent teams; use
  `start_claude_team.py` directly for collaborative team work (see
  `skills/coordinate-claude-work/SKILL.md`).
- mini-swe-agent runs in unattended `yolo` mode and can execute shell commands as the
  Coordinator service identity. Keep its repository, model, endpoint, limits, and host
  isolation within the same trust boundary as the browser terminal.
- Windows is not supported.

## Security posture

The default local dashboard has **no user authentication, no TLS, and no
authorization**. Its opaque local browser session supports CSRF protection and
the activity trail; it does not identify a user. Local mode is restricted to
loopback because anyone who can reach it gets the browser terminal's full
interactive `codex -C <repo>` session as your OS user.

An authenticated OIDC/ASGI mode now supplies default-deny route enforcement,
server-side sessions, CSRF checks, and audit events. **That does not by itself make a
deployment network-ready.** Network exposure remains gated by the plan in
[`docs/SECURITY_ROADMAP.md`](docs/SECURITY_ROADMAP.md), which records the
reverse-proxy, TLS, service-account, secret-management, backup, and adversarial-review
requirements that must also be met.

## Self-hosting

For running this as a persistent local service (foreground operation, logs,
updating, troubleshooting, and an optional systemd user-service setup), see
[`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md).

## License

Released under the MIT License, Copyright (c) 2026 Benjamin Crute. See
[`LICENSE`](LICENSE) for the full text. The license covers the first-party code
in this repository and grants no rights in the third-party material described
below.

## Third-party code

The browser terminal vendors [xterm.js and addon-fit](https://github.com/xtermjs/xterm.js)
under `src/coordinator/assets/web/vendor/`. See that project for
its own license and attribution.

## Why both a skill and repository files?

The skill contains the repeatable procedure. `AGENTS.md` gives Codex
persistent project instructions, while `CLAUDE.md` gives Claude persistent
project instructions. `.coordination/` contains changing task state. This
separates stable personal behavior, stable project rules, and per-turn state
instead of forcing one large prompt to do all three jobs.
