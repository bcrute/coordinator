# Self-hosting the coordination dashboard

`web_app.py` is a **dynamic service**, not a static site: it has to keep
running for the dashboard event stream and browser Codex WebSocket terminal to
work. There is no front-end build step and nothing to deploy to a CDN or static
host. Both runtime modes use Starlette/Uvicorn:

- `local` (the default): enforced as loopback-only and unauthenticated, with an
  opaque browser session for CSRF and audit state;
- `oidc`: generic OpenID Connect through Authlib, with opaque
  SQLite sessions, CSRF enforcement, and default-deny authorization.

This guide assumes you have already followed the root `README.md` quickstart
(installed the skill, copied `workflow.example.toml` to `workflow.toml`, and
confirmed the app runs in the foreground).

## Running in the foreground

```bash
.venv/bin/python skills/coordinate-claude-work/scripts/web_app.py --config workflow.toml
```

- The process logs to standard output/error and serves until you stop it.
- `Ctrl-C` stops the server. If the app itself started an automatic watcher
  (via the Agents view's Start control), stopping the server also stops that
  watcher; a watcher you started yourself in a separate terminal is left
  running.
- Uvicorn access logs are disabled in both modes. In OIDC mode this also keeps
  callback query strings containing short-lived authorization codes and state
  out of generic access logs. The redacted SQLite activity trail is the
  security and control record.
- There is no separate application log file by default; redirect stdout/
  stderr yourself if you want one, for example:

  ```bash
  .venv/bin/python skills/coordinate-claude-work/scripts/web_app.py --config workflow.toml \
    >> ~/workflow-web.log 2>&1
  ```

  The coordination watcher's own agent output is written separately, inside
  the coordinated project, to `.coordination/runtime/relay.log`; that file is
  already excluded from Git by the project's own coordination `.gitignore`
  and is unrelated to the web server's own stdout/stderr.

## Stopping it

- Foreground: `Ctrl-C`.
- As a systemd user service (below): `systemctl --user stop workflow-web`.

Stopping the server does not touch the coordinated project's `.coordination/`
state; task and review files persist on disk exactly as they were.

## Updating the clone

```bash
git pull
```

Refresh pinned Python dependencies after a pull, then restart the service:

```bash
uv pip install --python .venv/bin/python --requirement requirements.txt
```

If you are also updating the machine-level skill symlink and
`AGENTS.md`/`CLAUDE.md` blocks, re-run the installer — it only touches its own
marked blocks and does not reset your project's live coordination state:

```bash
python3 skills/coordinate-claude-work/scripts/install_user.py
```

If you are also managing a systemd user service, restart it after pulling:

```bash
systemctl --user restart workflow-web
```

## Troubleshooting

**Port already in use.** Pick a free port explicitly, or let the OS choose
one:

```bash
.venv/bin/python skills/coordinate-claude-work/scripts/web_app.py --config workflow.toml --port 0
```

The chosen port is printed on startup. You can also set `port = 0` (or any
specific free port) in `workflow.toml`; a command-line `--port` always wins
over the config file's value.

**"must be a Git repository or already coordination-initialized."** The initial
`repo` path (from `--repo` or your config file's `repo` key) needs to either
be a Git repository or already contain `.coordination/README.md`. Once the app
is running, the Setup view can create additional direct-child Git repositories
and initialize coordination for the active repository.

**Codex or Claude CLI authentication errors in the browser terminal.** The
browser's "Codex session" terminal runs `codex -C <repo>` using whatever
Codex login already exists for the account running `web_app.py`; it does not
prompt for or store credentials itself. Run `codex login` (or set the
appropriate API key in your shell environment) for that same account before
starting the server, then restart the server so the new process inherits the
updated environment. The same applies to Claude Code's own authentication for
automatic-watcher and native-team turns.

**A sibling repository does not show up in the repository picker.** The
picker only lists direct children of `repositories_root` that have a `.git`
file or directory directly inside them (a non-recursive scan). Confirm the
path in your config file's `repositories_root` key and that the target
directory is a real Git checkout, not a nested subdirectory of one.

**The terminal says its live socket is disconnected.** Confirm dependencies
were installed from `requirements.txt`; Uvicorn needs the pinned `websockets`
package to accept upgrades. Also confirm a reverse proxy passes WebSocket
upgrades and does not buffer `/api/events` when using OIDC mode.

**Changes to `workflow.toml` do not seem to take effect.** The server reads
`--config` once at startup; restart it after editing the file. Also confirm
you are editing the file the running process was actually pointed at — `cp
workflow.example.toml workflow.toml` creates a new, separate file that stays
untracked by Git.

## Optional: run as a systemd user service

This keeps the dashboard running across logins/reboots on a Linux machine
with systemd, without granting any additional network exposure beyond what
running it manually would give you (still loopback-only by default).

1. Copy the example unit and edit its placeholders:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp deploy/workflow-web.service.example ~/.config/systemd/user/workflow-web.service
   ```

   Edit `~/.config/systemd/user/workflow-web.service` and replace the three
   placeholder paths (`ExecStart`'s script path and `--config` path,
   `WorkingDirectory`, and the CLI directory in `Environment=PATH=...`) with
   the absolute paths to your actual clone, `workflow.toml`, and the
   directory containing your Codex/Claude executables. Do not use a `--host`
   other than the loopback address your config file already sets, unless
   you have independently put an authenticated boundary in front of the
   service.

   systemd user services do not necessarily inherit an interactive shell's
   `PATH`, so `codex`/`claude` may be missing even though they work in your
   terminal. Before enabling the unit, run `command -v codex` and
   `command -v claude` in the shell you normally use them from, and add
   the directory/directories they report to the unit's `Environment=PATH=...`
   value alongside standard system locations. Never put API keys or tokens
   in the tracked unit file or in `workflow.toml`; authenticate the CLIs
   through their own normal login mechanisms instead.

2. Reload systemd's user manager and enable the service:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now workflow-web
   ```

3. Check status and logs:

   ```bash
   systemctl --user status workflow-web
   journalctl --user -u workflow-web -f
   ```

4. To let the service survive logout (not just active sessions), enable
   lingering for your user once:

   ```bash
   loginctl enable-linger "$USER"
   ```

The example user unit remains a loopback/local starting point. For OIDC mode,
the service additionally needs the client-secret environment variable, an
owner-only writable `state_dir`, and a TLS reverse proxy whose address matches
`forwarded_allow_ips`. Do not put the client secret in the tracked unit or TOML
file; load it from a separate mode-0600 environment/credential file. OIDC mode
refuses wildcard trusted hosts and `forwarded_allow_ips = "*"`.

The authenticated application does not remove the need for service-account,
filesystem, proxy, backup, and recovery hardening. Treat the network-ready
checklist in `docs/SECURITY_ROADMAP.md` as the deployment gate.

For a future dedicated service account, use
`deploy/workflow-web-oidc.service.example` as a reviewed starting point. Its
paths are placeholders for a `/opt` clone, `/etc` configuration and secret
environment file, `/var/lib` security state, and `/srv` repositories; adapt
them to the host rather than copying the unit unchanged.
