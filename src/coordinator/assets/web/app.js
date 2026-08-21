/*
 * Render the read-only coordination state API as a live dashboard.
 *
 * Every value from the API reaches the page through textContent or a fixed
 * attribute value, so no API string is ever parsed as HTML.
 */
"use strict";

var STATE_URL = "/api/state";
var STATE_EVENTS_URL = "/api/events";
var PROVIDER_USAGE_URL = "/api/provider-usage";
var PROVIDER_USAGE_REFRESH_URL = "/api/provider-usage/refresh";
var TERMINAL_SOCKET_URL = "/ws/terminal";
var REPOSITORY_SELECT_URL = "/api/repository/select";
var REPOSITORY_SELECT_TIMEOUT_MS = 30000;
var CONTROL_URLS = { start: "/api/watcher/start", stop: "/api/watcher/stop" };
var CODEX_CONTROL_URLS = { start: "/api/codex/start", stop: "/api/codex/stop" };
var CODEX_CONTROL_TIMEOUT_MS = 30000;
var CODEX_RESIZE_DEBOUNCE_MS = 150;
var CODEX_INPUT_CHUNK_CHARS = 16 * 1024;
var POLL_INTERVAL_MS = 1000;
var REQUEST_TIMEOUT_MS = 4000;
var CONTROL_TIMEOUT_MS = 30000;
var STALE_AFTER_MS = 3000;
var LOG_VIEW_LINES = 200;
var NOT_RECORDED = "not recorded";

var nodes = Object.create(null);
var counts = new Intl.NumberFormat();
var tickTimer = null;
var usageTimer = null;
var stateSource = null;
var lastSuccessAt = null;
var failures = 0;
var lastFailure = "";
var renderFailure = "";
var managed = Object.create(null);
var pendingControl = "";
var renderedLog = null;
var csrfToken = "";
var securityMode = "local";
var terminalEnabled = true;
var latestState = null;
var activeRunId = "";
var guardrailFormRunId = "";
var runHistory = [];
var runsLoaded = false;
var runsLoading = false;
var selectedRun = null;
var selectedRunEvents = [];
var preferences = { browser_notifications: false, theme: "system", log_lines: 200 };
var preferencesLoaded = false;
var lastNotificationKey = "";
var shortcutPrefix = false;

var repositoryCatalog = { root: "", active: "", entries: [] };
var repositorySwitching = false;
var stateEpoch = 0;

var codexTerminal = null;
var codexTerminalReady = false;
var codexFitAddon = null;
var codexSession = Object.create(null);
var codexPendingControl = "";

var codexSocket = null;
var codexSocketTimer = null;
var codexOnDataDisposable = null;
var codexTerminalWritable = false;
var codexTerminalCursor = null;

var codexResizeTimer = null;
var codexLastSentRows = null;
var codexLastSentCols = null;
var codexResizeObserver = null;
var codexResizeFallbackWired = false;

var ROUTES = [
  "monitor",
  "terminal",
  "work",
  "agents",
  "logs",
  "activity",
  "runs",
  "settings",
  "setup",
  "sessions",
  "diagnostics",
];
var DEFAULT_ROUTE = "monitor";
var currentRoute = "";
var terminalEverVisible = false;

function el(id) {
  if (!(id in nodes)) {
    nodes[id] = document.getElementById(id);
  }
  return nodes[id];
}

function setText(id, value) {
  var node = el(id);
  if (node && node.textContent !== value) {
    node.textContent = value;
  }
}

function setTone(id, tone) {
  var node = el(id);
  if (!node) {
    return;
  }
  if (tone) {
    node.setAttribute("data-tone", tone);
  } else {
    node.removeAttribute("data-tone");
  }
}

function text(value, fallback) {
  var missing = typeof fallback === "string" ? fallback : NOT_RECORDED;
  if (typeof value === "number" && isFinite(value)) {
    return String(value);
  }
  if (typeof value !== "string") {
    return missing;
  }
  var trimmed = value.trim();
  return trimmed === "" ? missing : trimmed;
}

function count(value) {
  return typeof value === "number" && isFinite(value) ? counts.format(value) : "0";
}

function clock(timing) {
  if (!timing || typeof timing !== "object") {
    return "00:00:00";
  }
  return text(timing.display, "00:00:00");
}

function list(value) {
  return Array.isArray(value) ? value : [];
}

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function tone(state) {
  var value = String(state || "").toLowerCase();
  if (
    value === "accepted" ||
    value === "completed" ||
    value === "created" ||
    value === "done" ||
    value === "initialized" ||
    value === "ok" ||
    value === "revoked" ||
    value === "running" ||
    value === "success"
  ) {
    return "ok";
  }
  if (
    value === "active" ||
    value === "implementing" ||
    value === "ready" ||
    value === "review" ||
    value === "starting"
  ) {
    return "active";
  }
  if (
    value === "blocked" ||
    value === "denied" ||
    value === "failed" ||
    value === "error" ||
    value === "invalid" ||
    value === "changes_requested"
  ) {
    return "bad";
  }
  if (
    value === "idle" ||
    value === "unknown" ||
    value === "stopped" ||
    value === "stopping" ||
    value === "exited" ||
    value === "not_reviewed" ||
    value === "waiting_for_claude" ||
    value === "waiting_for_codex" ||
    value === "inactive"
  ) {
    return "warn";
  }
  return "";
}

function span(className, value) {
  var node = document.createElement("span");
  node.className = className;
  node.textContent = value;
  return node;
}

function item(className, value) {
  var node = document.createElement("li");
  node.className = className;
  node.textContent = value;
  return node;
}

function fillList(id, values, empty) {
  var node = el(id);
  if (!node) {
    return;
  }
  var entries = list(values).filter(function (value) {
    return typeof value === "string" && value.trim() !== "";
  });
  if (entries.length === 0) {
    node.replaceChildren(item("is-empty", empty));
    return;
  }
  node.replaceChildren.apply(
    node,
    entries.map(function (value) {
      return item("", value.trim());
    })
  );
}

/* Panels ------------------------------------------------------------------ */

function renderWorkflow(state) {
  var workflow = record(state.workflow);
  var completion = record(state.completion);

  if (state.coordination_present !== true) {
    setText("workflow-phase", "setup needed");
    setTone("workflow-phase", "warn");
    setText("workflow-label", "Coordination not yet initialized");
    var onboardingNode = el("workflow-completion");
    if (onboardingNode) {
      onboardingNode.hidden = true;
    }
    setText(
      "workflow-detail",
      "This repository has no .coordination/ yet. Open Terminal below, start Codex, " +
        "describe the project and its overall goal, and ask Codex to begin coordinated " +
        "work. Codex creates the coordination files from that discussion; normal " +
        "workflow status resumes automatically once the marker appears."
    );
    return;
  }

  setText("workflow-phase", text(workflow.phase, "unknown"));
  setTone("workflow-phase", tone(workflow.phase));
  setText("workflow-label", text(workflow.label));

  var node = el("workflow-completion");
  var current = workflow.completion_current === true;
  if (node) {
    node.hidden = !current;
  }
  if (current) {
    setText(
      "workflow-detail",
      "A completion report matches the current goal. See the conclusion below and expand for full detail."
    );
    var results = list(completion.result).filter(function (value) {
      return typeof value === "string" && value.trim() !== "";
    });
    setText(
      "workflow-conclusion",
      results.length > 0 ? results[0].trim() : "No result recorded."
    );
    fillList("workflow-result", completion.result, "No result recorded.");
    fillList("workflow-evidence", completion.evidence, "No evidence recorded.");
    fillList("workflow-limitations", completion.limitations, "No limitations recorded.");
  } else {
    setText("workflow-detail", text(workflow.detail));
  }
}

function renderGoal(state) {
  var goal = record(state.goal);
  var progress = record(goal.progress);
  var accepted = typeof progress.accepted === "number" ? progress.accepted : 0;
  var planned = typeof progress.planned === "number" ? progress.planned : 0;
  var label = text(progress.label, NOT_RECORDED);

  setText("goal-id", text(goal.id, "none"));
  setText("goal-state", text(goal.state, "unknown"));
  setTone("goal-state", tone(goal.state));
  setText("goal-objective", text(goal.objective));
  setText("goal-starting-ref", text(goal.starting_ref));
  setText("goal-target-branch", text(goal.target_branch));
  setText("goal-progress", label);
  setText(
    "goal-coordination",
    state.coordination_present === true ? "present" : "missing"
  );

  var meter = el("goal-meter");
  var fill = el("goal-meter-fill");
  var ratio = planned > 0 ? Math.max(0, Math.min(1, accepted / planned)) : 0;
  if (fill) {
    fill.style.width = (ratio * 100).toFixed(1) + "%";
  }
  if (meter) {
    meter.setAttribute("aria-valuemin", "0");
    meter.setAttribute("aria-valuemax", String(planned > 0 ? planned : 1));
    meter.setAttribute("aria-valuenow", String(planned > 0 ? accepted : 0));
    meter.setAttribute(
      "aria-valuetext",
      planned > 0 ? accepted + " of " + planned + " subgoals accepted" : NOT_RECORDED
    );
  }

  fillList("goal-completion", goal.completion_criteria, "No completion criteria recorded.");
  fillList("goal-constraints", goal.durable_constraints, "No durable constraints recorded.");
  fillList("goal-decisions", goal.owner_decisions, "No owner decisions recorded.");
}

function renderRoadmap(state) {
  var node = el("roadmap");
  var entries = list(state.roadmap).filter(function (entry) {
    return entry && typeof entry === "object";
  });
  var accepted = entries.filter(function (entry) {
    return entry.status === "accepted";
  }).length;

  setText(
    "roadmap-summary",
    entries.length === 0
      ? "no turns"
      : accepted + "/" + entries.length + " turns accepted"
  );
  if (!node) {
    return;
  }
  if (entries.length === 0) {
    node.replaceChildren(item("is-empty", "No roadmap recorded."));
    return;
  }
  node.replaceChildren.apply(
    node,
    entries.map(function (entry) {
      var status = text(entry.status, "planned");
      var marker = status === "accepted" ? "[x]" : status === "current" ? "[>]" : "[ ]";
      var row = document.createElement("li");
      row.setAttribute("data-status", status);
      var mark = span("marker", marker);
      mark.setAttribute("aria-hidden", "true");
      row.appendChild(mark);
      row.appendChild(
        span(
          "title",
          "Turn " + text(entry.turn, "?") + ": " + text(entry.title, "untitled")
        )
      );
      row.appendChild(span("status", status));
      return row;
    })
  );
}

function renderTask(state) {
  var task = record(state.task);
  setText("task-id", text(task.id, "none"));
  setText("task-state", text(task.state, "unknown"));
  setTone("task-state", tone(task.state));
  setText("task-review-round", text(task.review_round, "0"));
  setText("task-starting-ref", text(task.starting_ref));
  setText("task-objective", text(task.objective));
  fillList("task-acceptance", task.acceptance_criteria, "No acceptance criteria recorded.");
  fillList("task-in-scope", task.in_scope, "No scope recorded.");
  fillList("task-out-of-scope", task.out_of_scope, "Nothing excluded.");
  fillList("task-evidence", task.required_evidence, "No evidence requested.");
  fillList("task-external", task.allowed_external_actions, "No external actions allowed.");
  fillList("task-corrections", task.review_corrections, "No review corrections.");
}

function renderCoder(state) {
  var coder = record(state.coder);
  var workflow = record(state.workflow);
  var current = workflow.coder_current === true;
  var blocker = text(coder.blocker, "none");
  setText("coder-state", current ? text(coder.state, "unknown") : "inactive");
  setTone("coder-state", current ? tone(coder.state) : tone("inactive"));
  setText(
    "coder-activity",
    current
      ? text(coder.current_activity)
      : "No current Claude handoff. The raw coder record below is historical."
  );
  setText("coder-task", text(coder.task_id, "none"));
  setText("coder-review-round", text(coder.review_round, "0"));
  setText("coder-starting-ref", text(coder.starting_ref));
  setText("coder-current-ref", text(coder.current_ref));
  setText("coder-blocker", blocker);
  setTone("coder-blocker", blocker === "none" ? "" : "bad");

  var synced = coder.matches_current_task === true;
  setText(
    "coder-sync",
    synced
      ? "Coder status matches the current assignment."
      : "Coder status is from a different task or review round."
  );
  setTone("coder-sync", synced ? "" : "warn");
}

function renderReview(state) {
  var review = record(state.review);
  setText("review-verdict", text(review.verdict, "not_reviewed"));
  setTone("review-verdict", tone(review.verdict));
  setText("review-task", text(review.task_id, "none"));
  setText("review-round", text(review.review_round, "0"));
  setText("review-examined-ref", text(review.examined_ref));
  fillList("review-findings", review.findings, "No findings recorded.");
  fillList("review-next", review.next_action, "No next action recorded.");
}

function renderRuntime(state) {
  var runtime = record(state.runtime);
  var timing = record(runtime.timing);
  var tokens = record(runtime.tokens);

  setText("runtime-state", text(runtime.state, "unknown"));
  setTone("runtime-state", tone(runtime.state));
  setText("metric-activity", clock(timing.activity));
  setText("metric-turn", clock(timing.turn));
  setText("metric-overall", clock(timing.overall));
  setText("token-output", count(tokens.output_tokens));
  setText("token-input", count(tokens.input_tokens));
  setText("token-cache-read", count(tokens.cache_read_input_tokens));
  setText("token-cache-write", count(tokens.cache_creation_input_tokens));
  setText("runtime-primary-model", text(runtime.primary_model, "Claude"));
  setText("runtime-subagent-model", text(runtime.subagent_model, "provider-selected"));
  setText("runtime-orchestration", text(runtime.orchestration_mode));
  setText("runtime-task", text(runtime.task_id, "none"));

  var synced = runtime.matches_current_task === true;
  setText(
    "runtime-sync",
    synced
      ? "Metrics belong to the current task."
      : "No metrics recorded for the current task yet; counters read zero."
  );
  setTone("runtime-sync", synced ? "" : "warn");

  renderSubagents(list(runtime.subagents));
}

function renderSubagents(agents) {
  var node = el("subagents");
  var entries = agents.filter(function (entry) {
    return entry && typeof entry === "object";
  });
  var running = entries.filter(function (entry) {
    return entry.state === "running";
  }).length;

  setText(
    "subagents-summary",
    entries.length === 0 ? "none recorded" : running + " running / " + entries.length + " total"
  );
  if (!node) {
    return;
  }
  if (entries.length === 0) {
    node.replaceChildren(item("is-empty", "No subagents recorded for this task."));
    return;
  }
  node.replaceChildren.apply(
    node,
    entries.map(function (entry) {
      var usage = record(entry.usage);
      var row = document.createElement("li");
      var head = document.createElement("p");
      head.className = "record-head";
      var badge = span("badge", text(entry.state, "unknown"));
      var mood = tone(entry.state);
      if (mood) {
        badge.setAttribute("data-tone", mood);
      }
      head.appendChild(badge);
      head.appendChild(span("record-title", text(entry.description, "Claude subagent")));
      row.appendChild(head);
      var meta = document.createElement("p");
      meta.className = "record-meta";
      meta.textContent =
        text(entry.model, "inherited") +
        " · " +
        clock(entry.elapsed) +
        " · generated " +
        count(usage.output_tokens) +
        " · cache read " +
        count(usage.cache_read_input_tokens);
      row.appendChild(meta);
      return row;
    })
  );
}

function renderWatchers(state) {
  var node = el("watchers");
  var entries = list(state.watchers).filter(function (entry) {
    return entry && typeof entry === "object";
  });

  setText("watchers-summary", entries.length === 0 ? "none recorded" : entries.length + " recorded");
  if (!node) {
    return;
  }
  if (entries.length === 0) {
    node.replaceChildren(item("is-empty", "No watcher is reporting status."));
    return;
  }
  node.replaceChildren.apply(
    node,
    entries.map(function (entry) {
      var row = document.createElement("li");
      var head = document.createElement("p");
      head.className = "record-head";
      var badge = span("badge", text(entry.watcher_state, "unknown"));
      var mood = tone(entry.watcher_state);
      if (mood) {
        badge.setAttribute("data-tone", mood);
      }
      head.appendChild(badge);
      head.appendChild(span("record-title", text(entry.role, "watcher")));
      row.appendChild(head);
      var detail = document.createElement("p");
      detail.className = "record-meta";
      detail.textContent = text(entry.detail, "No detail reported.");
      row.appendChild(detail);
      var meta = document.createElement("p");
      meta.className = "record-meta";
      meta.textContent =
        "updated " +
        text(entry.updated_at) +
        " · lock " +
        (entry.lock_present === true ? "held" : "free") +
        summarise(record(entry.coordination));
      row.appendChild(meta);
      return row;
    })
  );
}

function summarise(coordination) {
  var parts = Object.keys(coordination)
    .filter(function (key) {
      var value = coordination[key];
      return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
    })
    .map(function (key) {
      return key.replace(/_/g, " ") + " " + String(coordination[key]);
    });
  return parts.length === 0 ? "" : " · " + parts.join(" · ");
}

/* Repository picker --------------------------------------------------------- */

function repositoryReport(message, mood) {
  setText("repository-select-feedback", message);
  setTone("repository-select-feedback", mood || "");
}

function paintRepositorySelector() {
  var select = el("repository-select");
  if (select) {
    select.disabled =
      repositorySwitching ||
      pendingControl !== "" ||
      codexPendingControl !== "" ||
      repositoryCatalog.entries.length === 0;
  }
}

function paintRepositoryControls() {
  paintRepositorySelector();
  paintControls();
  paintCodexControls();
}

function applyRepositoryCatalog(catalog) {
  var root = text(catalog.root, "");
  var active = text(catalog.active, "");
  var entries = list(catalog.entries).filter(function (entry) {
    return entry && typeof entry === "object";
  });
  repositoryCatalog = { root: root, active: active, entries: entries };
  setText(
    "repository-catalog-root",
    "Catalog root: " + (root === "" ? "not recorded" : root)
  );

  var select = el("repository-select");
  if (!select) {
    return;
  }
  var signature = entries
    .map(function (entry) {
      return (
        text(entry.path, "") +
        "\u0000" +
        text(entry.name, text(entry.path, "")) +
        "\u0000" +
        (entry.initialized === true ? "1" : "0")
      );
    })
    .join("\u0001");
  if (select.getAttribute("data-signature") !== signature) {
    select.replaceChildren.apply(
      select,
      entries.map(function (entry) {
        var path = text(entry.path, "");
        var opt = document.createElement("option");
        opt.value = path;
        opt.textContent = text(entry.name, path);
        if (entry.initialized !== true) {
          opt.textContent += " \u2014 setup needed";
        }
        opt.title = path;
        return opt;
      })
    );
    select.setAttribute("data-signature", signature);
  }
  select.value = active;
  paintRepositoryControls();
}

function renderRepositoryCatalog(state) {
  if (repositorySwitching) {
    return;
  }
  applyRepositoryCatalog(record(state.repository_catalog));
}

function describeRepositorySelect(error) {
  if (error && error.name === "AbortError") {
    return "the request timed out after " + REPOSITORY_SELECT_TIMEOUT_MS + " ms";
  }
  return describe(error);
}

function resetTerminalClientStateForSwitch() {
  closeCodexSocket();
  if (codexTerminalReady && codexTerminal) {
    codexTerminal.reset();
  }
  codexLastSentRows = null;
  codexLastSentCols = null;
  codexTerminalCursor = null;
  renderedLog = null;
}

/*
 * Select a repository. The URL is the fixed REPOSITORY_SELECT_URL, and the
 * body is exactly {path: selectedValue} for a value that came only from a
 * server-provided catalog option, so no free-form path can be sent.
 */
function selectRepository(path) {
  if (repositorySwitching || pendingControl !== "" || codexPendingControl !== "") {
    return;
  }
  var select = el("repository-select");
  var previousActive = repositoryCatalog.active;
  repositorySwitching = true;
  stopStateFeed();
  stateEpoch += 1;
  var myEpoch = stateEpoch;
  paintRepositoryControls();
  repositoryReport("Switching repository…", "active");

  var controller = typeof AbortController === "function" ? new AbortController() : null;
  var timeout = window.setTimeout(function () {
    if (controller) {
      controller.abort();
    }
  }, REPOSITORY_SELECT_TIMEOUT_MS);
  var options = {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify({ path: path }),
  };
  if (controller) {
    options.signal = controller.signal;
  }

  fetch(REPOSITORY_SELECT_URL, options)
    .then(answer)
    .then(function (result) {
      var payload = result.payload;
      if (result.status !== 200) {
        throw new Error(text(payload.message, "the server answered " + result.status));
      }
      var outcome = text(payload.outcome, "error");
      var catalog = record(payload.repository_catalog);
      if (outcome === "selected") {
        resetTerminalClientStateForSwitch();
        applyRepositoryCatalog(catalog);
        repositoryReport(text(payload.message, "Repository switched."), "ok");
      } else if (outcome === "unchanged") {
        applyRepositoryCatalog(catalog);
        repositoryReport(text(payload.message, "Repository unchanged."), "ok");
      } else {
        throw new Error(text(payload.message, "the selection was rejected"));
      }
    })
    .catch(function (error) {
      if (select) {
        select.value = previousActive;
      }
      repositoryReport("Repository switch failed: " + describeRepositorySelect(error), "bad");
    })
    .then(function () {
      window.clearTimeout(timeout);
      repositorySwitching = false;
      paintRepositoryControls();
      if (myEpoch === stateEpoch) {
        restartStateFeed();
      }
    });
}

function wireRepositoryPicker() {
  var select = el("repository-select");
  if (!select) {
    return;
  }
  select.addEventListener("change", function () {
    var value = select.value;
    if (value === "" || value === repositoryCatalog.active) {
      return;
    }
    selectRepository(value);
  });
}

/* Watcher controls -------------------------------------------------------- */

function commandText(command) {
  var parts = list(command).filter(function (value) {
    return typeof value === "string" && value.trim() !== "";
  });
  return parts.length === 0 ? NOT_RECORDED : parts.join(" ");
}

function processText(watcher) {
  var pid =
    typeof watcher.pid === "number" && isFinite(watcher.pid)
      ? "pid " + watcher.pid
      : "no pid";
  return (
    text(watcher.role, "both") +
    " watcher · " +
    pid +
    " · " +
    (watcher.running === true ? "process running" : "no process running")
  );
}

function startedText(watcher) {
  var stamp = text(watcher.started_at, "");
  var epoch =
    typeof watcher.started_at_epoch === "number" && isFinite(watcher.started_at_epoch)
      ? watcher.started_at_epoch
      : null;
  if (stamp === "" && epoch === null) {
    return "not started";
  }
  if (epoch === null) {
    return stamp;
  }
  var age = ago(Math.max(0, Date.now() - epoch * 1000));
  return stamp === "" ? "started " + age : stamp + " · started " + age;
}

function exitText(watcher) {
  var code =
    typeof watcher.exit_code === "number" && isFinite(watcher.exit_code)
      ? watcher.exit_code
      : null;
  var stamp = text(watcher.exited_at, "");
  if (code === null && stamp === "") {
    return NOT_RECORDED;
  }
  var label = code === null ? "exited" : "exit status " + code;
  return stamp === "" ? label : label + " at " + stamp;
}

function paintControls() {
  var busy = pendingControl !== "" || repositorySwitching;
  var startNode = el("watcher-start");
  var stopNode = el("watcher-stop");
  if (startNode) {
    startNode.disabled = busy || managed.can_start !== true;
  }
  if (stopNode) {
    stopNode.disabled = busy || managed.can_stop !== true;
  }
  paintRepositorySelector();
}

function report(message, mood) {
  setText("watcher-feedback", message);
  setTone("watcher-feedback", mood || "");
}

function applyManaged(watcher) {
  managed = watcher;
  setText("managed-watcher-state", text(watcher.state, "unknown"));
  setTone("managed-watcher-state", tone(watcher.state));
  setText("managed-watcher-detail", text(watcher.detail, "No managed watcher detail reported."));
  setText("managed-watcher-pid", processText(watcher));
  setText("managed-watcher-started", startedText(watcher));
  setText("managed-watcher-exit", exitText(watcher));
  setText("managed-watcher-lock", watcher.lock_present === true ? "held" : "free");
  setText(
    "managed-watcher-command",
    commandText(watcher.command) + " · logs to " + text(watcher.log_path)
  );
  paintControls();
}

function renderManaged(state) {
  applyManaged(record(state.managed_watcher));
}

function describeControl(error) {
  if (error && error.name === "AbortError") {
    return "the request timed out after " + CONTROL_TIMEOUT_MS + " ms";
  }
  return describe(error);
}

function answer(response) {
  return response.text().then(function (body) {
    var payload = null;
    try {
      payload = JSON.parse(body);
    } catch (error) {
      payload = null;
    }
    return { status: response.status, payload: record(payload) };
  });
}

/*
 * Send one fixed, body-less control request. The URL comes from CONTROL_URLS,
 * never from the page or the API, so no request can name a command or a path.
 */
function control(kind) {
  if (
    pendingControl !== "" ||
    repositorySwitching ||
    !Object.prototype.hasOwnProperty.call(CONTROL_URLS, kind)
  ) {
    return;
  }
  pendingControl = kind;
  paintControls();
  report(kind === "start" ? "Starting the watcher…" : "Stopping the watcher…", "active");

  var controller = typeof AbortController === "function" ? new AbortController() : null;
  var timeout = window.setTimeout(function () {
    if (controller) {
      controller.abort();
    }
  }, CONTROL_TIMEOUT_MS);
  var options = {
    method: "POST",
    cache: "no-store",
    headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
  };
  if (controller) {
    options.signal = controller.signal;
  }

  fetch(CONTROL_URLS[kind], options)
    .then(answer)
    .then(function (result) {
      var payload = result.payload;
      var outcome = text(payload.outcome, result.status === 200 ? "done" : "error");
      var message = text(payload.message, "the server answered " + result.status);
      report(
        outcome + ": " + message,
        result.status === 200 ? "ok" : result.status === 409 ? "warn" : "bad"
      );
      var next = record(payload.managed_watcher);
      if (Object.keys(next).length > 0) {
        applyManaged(next);
      }
    })
    .catch(function (error) {
      report("the " + kind + " request failed: " + describeControl(error), "bad");
    })
    .then(function () {
      window.clearTimeout(timeout);
      pendingControl = "";
      paintControls();
      restartStateFeed();
    });
}

function wireControls() {
  var buttons = { start: el("watcher-start"), stop: el("watcher-stop") };
  Object.keys(buttons).forEach(function (kind) {
    var node = buttons[kind];
    if (node) {
      node.addEventListener("click", function () {
        control(kind);
      });
    }
  });
  paintControls();
}

/* Relay log --------------------------------------------------------------- */

function renderLog(state) {
  var log = record(state.relay_log);
  var available = log.available === true;
  var path = text(log.path);
  var shown = list(log.lines)
    .filter(function (line) {
      return typeof line === "string";
    })
    .slice(-LOG_VIEW_LINES);

  setText(
    "relay-log-summary",
    shown.length === 0
      ? available
        ? "empty"
        : "no log file"
      : shown.length + (shown.length === 1 ? " line" : " lines")
  );
  setText("relay-log-path", path);
  setText(
    "relay-log-note",
    !available
      ? "No relay log file yet at " + path + "."
      : shown.length === 0
      ? "The relay log is empty."
      : "Showing the last " +
        shown.length +
        (shown.length === 1 ? " line" : " lines") +
        (log.truncated === true ? "; earlier lines are trimmed." : "; this is the whole log.")
  );

  var node = el("relay-log");
  if (!node) {
    return;
  }
  var signature = shown.length + "\u0000" + shown.join("\u0000");
  if (renderedLog === signature) {
    return;
  }
  renderedLog = signature;

  var scroll = el("relay-log-scroll");
  var pinned =
    !scroll || scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight <= 24;
  if (shown.length === 0) {
    node.replaceChildren(
      item("is-empty", available ? "The relay log is empty." : "No relay log file yet.")
    );
  } else {
    node.replaceChildren.apply(
      node,
      shown.map(function (line) {
        return item("", line === "" ? " " : line);
      })
    );
  }
  if (scroll && pinned) {
    scroll.scrollTop = scroll.scrollHeight;
  }
}

/* Codex terminal session --------------------------------------------------- */

function codexReport(message, mood) {
  setText("codex-session-feedback", message);
  setTone("codex-session-feedback", mood || "");
}

function initCodexTerminal() {
  var viewport = el("codex-terminal");
  if (!viewport) {
    return;
  }
  if (
    typeof globalThis.Terminal !== "function" ||
    !globalThis.FitAddon ||
    typeof globalThis.FitAddon.FitAddon !== "function"
  ) {
    codexReport("The terminal library did not load; session controls are unavailable.", "bad");
    return;
  }
  try {
    codexTerminal = new globalThis.Terminal({
      convertEol: true,
      cursorBlink: true,
      scrollback: 5000,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      theme: {
        background: "#0b0f14",
        foreground: "#d5dbe3",
        cursor: "#d5dbe3",
      },
    });
    var fitAddon = new globalThis.FitAddon.FitAddon();
    codexFitAddon = fitAddon;
    codexTerminal.loadAddon(fitAddon);
    codexTerminal.open(viewport);
    /*
     * Terminal view is hidden by default (display: none), so its viewport
     * has zero size at open() time. Fitting a zero-size viewport would
     * collapse the terminal to 0 rows/cols. Skip the initial fit here and
     * fit/resize only once the Terminal route becomes visible.
     */
    codexTerminalReady = true;
    codexOnDataDisposable = codexTerminal.onData(function (data) {
      if (codexTerminalWritable && codexSocket && codexSocket.readyState === WebSocket.OPEN) {
        for (var index = 0; index < data.length; index += CODEX_INPUT_CHUNK_CHARS) {
          codexSocket.send(
            JSON.stringify({
              type: "input",
              protocol: "terminal.v1",
              data: data.slice(index, index + CODEX_INPUT_CHUNK_CHARS),
            })
          );
        }
      }
    });
    wireCodexResizeWatcher(viewport);
    if (currentRoute === "terminal") {
      scheduleCodexFitAndResize();
    }
    paintCodexControls();
  } catch (error) {
    codexTerminal = null;
    codexTerminalReady = false;
    codexReport("The terminal failed to initialize: " + describe(error), "bad");
  }
}

/* Codex terminal socket ---------------------------------------------------- */

function applyCodexOutput(output) {
  if (!codexTerminalReady || !codexTerminal) {
    return;
  }
  var payload = output && typeof output === "object" ? output : {};
  var chunk = typeof payload.text === "string" ? payload.text : "";
  var reset = payload.reset === true;
  if (Number.isInteger(payload.next_cursor)) codexTerminalCursor = payload.next_cursor;
  if (reset) {
    codexTerminal.reset();
    if (chunk !== "") {
      codexTerminal.write(chunk);
    }
  } else if (chunk !== "") {
    codexTerminal.write(chunk);
  }
}

function closeCodexSocket() {
  window.clearTimeout(codexSocketTimer);
  if (codexSocket) {
    var socket = codexSocket;
    codexSocket = null;
    socket.close();
  }
  codexTerminalWritable = false;
}

function connectCodexSocket() {
  if (
    !terminalEnabled ||
    currentRoute !== "terminal" ||
    csrfToken === "" ||
    !codexTerminalReady ||
    (codexSocket && codexSocket.readyState < 2)
  ) {
    return;
  }
  var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  var socket = new WebSocket(protocol + "//" + window.location.host + TERMINAL_SOCKET_URL);
  codexSocket = socket;
  socket.addEventListener("open", function () {
    if (socket !== codexSocket) {
      return;
    }
    socket.send(JSON.stringify({
      type: "hello",
      protocol: "terminal.v1",
      csrf_token: csrfToken,
      cursor: codexTerminalCursor,
    }));
    codexTerminalWritable = false;
    codexReport("Terminal connected through the live socket.", "ok");
    scheduleCodexFitAndResize();
  });
  socket.addEventListener("message", function (event) {
    if (socket !== codexSocket) {
      return;
    }
    try {
      var message = JSON.parse(event.data);
      if (message.type === "output") {
        applyCodexOutput(record(message.output));
      } else if (message.type === "session") {
        applyCodexSession(record(message.session));
      } else if (message.type === "repository_changed") {
        closeCodexSocket();
        connectCodexSocket();
      } else if (message.type === "error") {
        codexReport(text(message.message, "Terminal socket error."), "bad");
      }
    } catch (error) {
      codexReport("Invalid terminal socket response.", "bad");
    }
  });
  socket.addEventListener("close", function () {
    if (socket !== codexSocket) {
      return;
    }
    codexSocket = null;
    codexReport("Terminal socket disconnected; reconnecting.", "warn");
    codexSocketTimer = window.setTimeout(connectCodexSocket, 1000);
  });
  socket.addEventListener("error", function () {
    codexReport("Terminal socket error; reconnecting.", "bad");
  });
}

/* Codex resize --------------------------------------------------------------- */

function sendCodexResize(rows, cols) {
  if (
    !Number.isInteger(rows) ||
    !Number.isInteger(cols) ||
    rows <= 0 ||
    cols <= 0 ||
    (rows === codexLastSentRows && cols === codexLastSentCols)
  ) {
    return;
  }
  codexLastSentRows = rows;
  codexLastSentCols = cols;
  if (codexTerminalWritable && codexSocket && codexSocket.readyState === WebSocket.OPEN) {
    codexSocket.send(JSON.stringify({
      type: "resize",
      protocol: "terminal.v1",
      rows: rows,
      cols: cols,
    }));
  }
}

function scheduleCodexFitAndResize() {
  window.clearTimeout(codexResizeTimer);
  codexResizeTimer = window.setTimeout(function () {
    if (!codexTerminalReady || !codexFitAddon || !codexTerminal) {
      return;
    }
    try {
      codexFitAddon.fit();
    } catch (error) {
      return;
    }
    sendCodexResize(codexTerminal.rows, codexTerminal.cols);
  }, CODEX_RESIZE_DEBOUNCE_MS);
}

function wireCodexResizeWatcher(viewport) {
  if (typeof globalThis.ResizeObserver === "function") {
    codexResizeObserver = new globalThis.ResizeObserver(function () {
      scheduleCodexFitAndResize();
    });
    codexResizeObserver.observe(viewport);
  } else {
    codexResizeFallbackWired = true;
    window.addEventListener("resize", scheduleCodexFitAndResize);
  }
}

function teardownCodexTerminal() {
  closeCodexSocket();
  window.clearTimeout(codexResizeTimer);
  if (codexResizeObserver) {
    codexResizeObserver.disconnect();
    codexResizeObserver = null;
  }
  if (codexResizeFallbackWired) {
    window.removeEventListener("resize", scheduleCodexFitAndResize);
    codexResizeFallbackWired = false;
  }
  if (codexOnDataDisposable && typeof codexOnDataDisposable.dispose === "function") {
    codexOnDataDisposable.dispose();
    codexOnDataDisposable = null;
  }
}

function codexProcessText(session) {
  var pid =
    typeof session.pid === "number" && isFinite(session.pid) ? "pid " + session.pid : "no pid";
  return pid + " · " + (session.running === true ? "process running" : "no process running");
}

function codexSizeText(session) {
  var rows = typeof session.rows === "number" && isFinite(session.rows) ? session.rows : null;
  var cols = typeof session.cols === "number" && isFinite(session.cols) ? session.cols : null;
  return rows === null || cols === null ? NOT_RECORDED : rows + " x " + cols;
}

function paintCodexControls() {
  var busy = codexPendingControl !== "" || repositorySwitching;
  var startNode = el("codex-session-start");
  var stopNode = el("codex-session-stop");
  var clearNode = el("codex-terminal-clear");
  if (startNode) {
    startNode.disabled = !terminalEnabled || busy || codexSession.can_start !== true;
  }
  if (stopNode) {
    stopNode.disabled = !terminalEnabled || busy || codexSession.can_stop !== true;
  }
  if (clearNode) {
    clearNode.disabled = !terminalEnabled || !codexTerminalReady;
  }
  paintRepositorySelector();
}

function applyCodexSession(session) {
  codexSession = session;
  setText("codex-session-state", text(session.state, "unknown"));
  setTone("codex-session-state", tone(session.state));
  setText("codex-session-detail", text(session.detail, "No Codex session detail reported."));
  setText("codex-session-pid", codexProcessText(session));
  setText("codex-session-size", codexSizeText(session));
  setText("codex-session-command", commandText(session.command));
  var attachment = record(session.attachment);
  if (attachment.mode) {
    codexTerminalWritable = attachment.owned_by_this_connection === true;
    codexReport(
      codexTerminalWritable
        ? "Terminal attached with input ownership."
        : "Terminal attached read-only; another browser owns input.",
      codexTerminalWritable ? "ok" : "warn"
    );
  }
  paintCodexControls();
  connectCodexSocket();
}

function renderCodexSession(state) {
  if (!terminalEnabled) {
    closeCodexSocket();
    setText("codex-session-state", "disabled");
    setTone("codex-session-state", "muted");
    setText("codex-session-detail", "The browser terminal is disabled by server configuration.");
    paintCodexControls();
    return;
  }
  applyCodexSession(record(state.codex_session));
}

function codexControl(kind) {
  if (
    codexPendingControl !== "" ||
    repositorySwitching ||
    !Object.prototype.hasOwnProperty.call(CODEX_CONTROL_URLS, kind)
  ) {
    return;
  }
  codexPendingControl = kind;
  paintCodexControls();
  codexReport(kind === "start" ? "Starting the Codex session…" : "Stopping the Codex session…", "active");

  var controller = typeof AbortController === "function" ? new AbortController() : null;
  var timeout = window.setTimeout(function () {
    if (controller) {
      controller.abort();
    }
  }, CODEX_CONTROL_TIMEOUT_MS);
  var options = {
    method: "POST",
    cache: "no-store",
    headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
  };
  if (controller) {
    options.signal = controller.signal;
  }

  fetch(CODEX_CONTROL_URLS[kind], options)
    .then(answer)
    .then(function (result) {
      var payload = result.payload;
      var outcome = text(payload.outcome, result.status === 200 ? "done" : "error");
      var message = text(payload.message, "the server answered " + result.status);
      codexReport(
        outcome + ": " + message,
        result.status === 200 ? "ok" : result.status === 409 ? "warn" : "bad"
      );
      var next = record(payload.codex_session);
      if (Object.keys(next).length > 0) {
        applyCodexSession(next);
      }
      if (kind === "start" && result.status === 200) {
        if (codexTerminalReady && codexTerminal) {
          codexTerminal.reset();
        }
        connectCodexSocket();
      }
    })
    .catch(function (error) {
      codexReport("the " + kind + " request failed: " + describeControl(error), "bad");
    })
    .then(function () {
      window.clearTimeout(timeout);
      codexPendingControl = "";
      paintCodexControls();
      restartStateFeed();
    });
}

function codexClear() {
  if (!codexTerminalReady || !codexTerminal) {
    return;
  }
  if (typeof codexTerminal.reset === "function") {
    codexTerminal.reset();
  } else if (typeof codexTerminal.clear === "function") {
    codexTerminal.clear();
  }
}

function wireCodexControls() {
  var startNode = el("codex-session-start");
  var stopNode = el("codex-session-stop");
  var clearNode = el("codex-terminal-clear");
  if (startNode) {
    startNode.addEventListener("click", function () {
      codexControl("start");
    });
  }
  if (stopNode) {
    stopNode.addEventListener("click", function () {
      codexControl("stop");
    });
  }
  if (clearNode) {
    clearNode.addEventListener("click", codexClear);
  }
  paintCodexControls();
}

function render(state) {
  latestState = state;
  var security = record(state.security);
  csrfToken = text(security.csrf_token, "");
  securityMode = text(security.mode, "local");
  var user = record(security.user);
  var userNode = el("authenticated-user");
  var logoutNode = el("logout");
  var authenticated = security.authenticated === true;
  terminalEnabled = record(state.capabilities).terminal === true;
  var terminalNav = el("nav-terminal");
  if (terminalNav) {
    terminalNav.parentElement.hidden = !terminalEnabled;
  }
  if (terminalEnabled && !codexTerminalReady) {
    initCodexTerminal();
  } else if (!terminalEnabled) {
    closeCodexSocket();
    if (routeFromHash() === "terminal") {
      window.location.hash = "#monitor";
    }
  }
  if (userNode) {
    userNode.hidden = !authenticated;
    userNode.textContent = authenticated ? text(user.display, text(user.sub, "signed in")) : "";
  }
  if (logoutNode) {
    logoutNode.hidden = !authenticated;
    logoutNode.disabled = !authenticated || csrfToken === "";
  }
  setText("repo-path", text(state.repo, "unknown repository"));
  setText("generated-at", text(state.generated_at));
  renderRepositoryCatalog(state);
  renderWorkflow(state);
  renderCodexSession(state);
  renderGoal(state);
  renderRoadmap(state);
  renderTask(state);
  renderCoder(state);
  renderReview(state);
  renderRuntime(state);
  renderWatchers(state);
  renderManaged(state);
  renderLog(state);
  renderOwnerAction(state);
  renderWorkspace(state);
  renderGuardrails(state);
  reportActiveRepositoryReadiness(state);
  if (!runsLoaded && !runsLoading) {
    loadRuns();
  }
}

/* Provider limits -------------------------------------------------------- */

function usageTone(remaining) {
  if (typeof remaining !== "number") return "neutral";
  if (remaining <= 5) return "bad";
  if (remaining <= 20) return "warn";
  return "ok";
}

function usagePercent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return Math.round(value) + "%";
}

function usageReset(value) {
  if (typeof value !== "string" || value === "") return "reset unknown";
  var parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "reset unknown";
  return "resets " + parsed.toLocaleString();
}

function renderProviderUsage(payload) {
  var providers = list(record(payload).providers);
  [
    { id: "codex", chip: el("usage-codex"), value: el("usage-codex-value") },
    { id: "claude", chip: el("usage-claude"), value: el("usage-claude-value") },
  ].forEach(function (target) {
    var provider = providers.find(function (candidate) {
      return text(record(candidate).id) === target.id;
    });
    var details = record(provider);
    var remaining = details.remaining_percent;
    var chip = target.chip;
    var value = target.value;
    if (!chip || !value) return;
    value.textContent = usagePercent(remaining);
    chip.dataset.tone = usageTone(remaining);
    var title = text(details.name, target.id === "codex" ? "Codex" : "Claude");
    var plan = text(details.plan);
    if (plan) title += " " + plan;
    var windows = list(details.windows);
    if (windows.length) {
      title += " — " + windows.map(function (windowValue) {
        var windowDetails = record(windowValue);
        return text(windowDetails.label, "rolling") + ": " +
          usagePercent(windowDetails.remaining_percent) + " remaining, " +
          usageReset(windowDetails.resets_at);
      }).join("; ");
    } else {
      title += " — " + text(details.message, "Usage unavailable");
    }
    chip.title = title;
  });
  var refresh = el("usage-refresh");
  if (refresh) refresh.disabled = record(payload).refreshing === true;
}

function loadProviderUsage() {
  return fetch(PROVIDER_USAGE_URL, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  }).then(answer).then(function (result) {
    if (!result.response.ok) {
      throw new Error(text(result.payload.message, "usage request failed"));
    }
    renderProviderUsage(result.payload);
  }).catch(function () {
    renderProviderUsage({
      providers: [
        { id: "codex", message: "Usage service unavailable" },
        { id: "claude", message: "Usage service unavailable" },
      ],
    });
  });
}

function refreshProviderUsage() {
  var refresh = el("usage-refresh");
  if (!refresh || refresh.disabled || csrfToken === "") return;
  refresh.disabled = true;
  fetch(PROVIDER_USAGE_REFRESH_URL, {
    method: "POST",
    cache: "no-store",
    headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
  }).then(answer).then(function (result) {
    if (!result.response.ok) {
      throw new Error(text(result.payload.message, "usage refresh failed"));
    }
    renderProviderUsage(result.payload);
  }).catch(function () {
    refresh.disabled = false;
  });
}

function wireProviderUsage() {
  var refresh = el("usage-refresh");
  if (refresh) refresh.addEventListener("click", refreshProviderUsage);
  loadProviderUsage();
  window.setTimeout(loadProviderUsage, 1500);
  usageTimer = window.setInterval(loadProviderUsage, 60 * 1000);
}

/* Workspace and durable runs --------------------------------------------- */

function renderOwnerAction(state) {
  var workflow = record(state.workflow);
  var run = record(state.run);
  var phase = text(workflow.phase, "inactive");
  var title = "No action required";
  var detail = text(workflow.detail, "The workflow is being observed.");
  var mood = "ok";
  var destination = "#work";
  if (run.resume_required === true || phase === "blocked") {
    title = run.resume_required === true ? "Run paused — review and resume" : "Resolve the current blocker";
    detail = text(run.pause_reason, detail);
    mood = "bad";
    destination = run.resume_required === true ? "#settings" : "#work";
  } else if (phase === "waiting_for_codex") {
    title = "Review is ready";
    mood = "warn";
  } else if (phase === "done") {
    title = "Run complete — inspect the evidence";
    destination = "#runs";
  }
  setText("owner-action-title", title);
  setText("owner-action-detail", detail);
  var owner = el("owner-action");
  if (owner) owner.setAttribute("data-tone", mood);
  var link = el("owner-action-link");
  if (link) link.setAttribute("href", destination);
  maybeNotifyOwner(run, phase, title, detail);
}

function maybeNotifyOwner(run, phase, title, detail) {
  if (preferences.browser_notifications !== true || typeof Notification !== "function") return;
  if (Notification.permission !== "granted") return;
  if (!(run.resume_required === true || phase === "blocked" || phase === "waiting_for_codex" || phase === "done")) return;
  var key = text(run.run_id, "") + ":" + phase + ":" + String(run.resume_required === true);
  if (key === lastNotificationKey) return;
  lastNotificationKey = key;
  new Notification(title, { body: detail, tag: key });
}

function renderWorkspace(state) {
  var catalog = record(state.repository_catalog);
  var entries = list(catalog.entries);
  setText(
    "workspace-repositories-summary",
    entries.length + " repositor" + (entries.length === 1 ? "y" : "ies")
  );
  var node = el("workspace-repositories");
  if (!node) return;
  var cards = entries.map(function (entry) {
    var path = text(entry.path, "");
    var run = runHistory.find(function (candidate) { return text(candidate.path, "") === path; });
    var summary = record(run && run.summary);
    var workflow = record(summary.workflow);
    var timing = record(record(summary.timing).overall);
    var tokens = record(summary.tokens);
    var card = document.createElement("li");
    card.className = "workspace-card";
    card.setAttribute("data-active", entry.active === true ? "true" : "false");
    card.appendChild(recordBlock(
      text(entry.name, "Repository"),
      run ? text(run.status, "unknown") : (entry.initialized === true ? "ready" : "setup"),
      run
        ? "Goal " + text(run.goal_id, "none") + " · " + text(workflow.label, text(run.status, "unknown")) +
          " · " + text(timing.display, "00:00:00") + " · " + count(tokens.output_tokens) + " generated"
        : path
    ));
    return card;
  });
  if (cards.length === 0) cards.push(item("is-empty", "No repositories are available."));
  node.replaceChildren.apply(node, cards);
}

function renderGuardrails(state) {
  var run = record(state.run);
  var guardrails = record(state.guardrails);
  activeRunId = text(run.run_id, "");
  setText("guardrails-state", text(guardrails.status, "not configured"));
  setTone("guardrails-state", tone(text(guardrails.status, "")));
  var form = el("guardrails-form");
  if (!form) return;
  Array.prototype.forEach.call(form.elements, function (control) {
    if (control && control.name) control.disabled = activeRunId === "";
  });
  if (activeRunId === "" || guardrailFormRunId === activeRunId) return;
  guardrailFormRunId = activeRunId;
  var policy = record(run.policy);
  Object.keys(GuardrailDefaults).forEach(function (name) {
    var control = form.elements.namedItem(name);
    if (!control) return;
    var value = policy[name];
    control.value = value === null || value === undefined ? GuardrailDefaults[name] : String(value);
  });
  setText("guardrails-feedback", run.resume_required === true
    ? "This run is paused. Adjust limits if needed, then resume it from Runs."
    : "Limits are evaluated only from provider-reported values.");
}

var GuardrailDefaults = {
  turn_seconds: "",
  overall_seconds: "",
  generated_tokens: "",
  input_tokens: "",
  cache_read_tokens: "",
  cache_write_tokens: "",
  correction_rounds: "",
  concurrent_workers: "",
  no_progress_seconds: "",
  warning_ratio: "0.8",
};

function loadRuns() {
  if (runsLoading) return;
  runsLoading = true;
  setText("runs-feedback", "Loading durable run history…");
  apiGet("/api/runs?limit=200").then(function (payload) {
    runHistory = list(payload.runs);
    runsLoaded = true;
    renderRunRecords();
    if (latestState) renderWorkspace(latestState);
    setText("runs-feedback", runHistory.length + " durable run" + (runHistory.length === 1 ? "" : "s") + ".");
  }).catch(function (error) {
    setText("runs-feedback", "Could not load runs: " + describe(error));
  }).then(function () { runsLoading = false; });
}

function renderRunRecords() {
  var queryNode = el("runs-filter");
  var query = queryNode ? queryNode.value.trim().toLowerCase() : "";
  var values = runHistory.filter(function (run) {
    return query === "" || [run.repository, run.goal_id, run.status, run.path].join(" ").toLowerCase().indexOf(query) !== -1;
  });
  var node = el("run-records");
  if (!node) return;
  var rows = values.map(function (run) {
    var row = document.createElement("li");
    var button = document.createElement("button");
    button.type = "button";
    button.className = "record-select";
    button.appendChild(recordBlock(
      text(run.repository, "Repository") + " · " + text(run.goal_id, "Goal"),
      text(run.status, "unknown"),
      new Date(Number(run.last_seen_at) * 1000).toLocaleString() +
        (run.resume_required === true ? " · owner action required" : "")
    ));
    button.addEventListener("click", function () { loadRunDetail(text(run.run_id, "")); });
    row.appendChild(button);
    return row;
  });
  if (rows.length === 0) rows.push(item("is-empty", "No runs match this filter."));
  node.replaceChildren.apply(node, rows);
}

function loadRunDetail(runId) {
  if (runId === "") return;
  setText("run-detail-title", "Loading run…");
  Promise.all([apiGet("/api/runs/" + encodeURIComponent(runId)), apiGet("/api/runs/" + encodeURIComponent(runId) + "/events")])
    .then(function (values) {
      selectedRun = record(values[0].run);
      selectedRunEvents = list(values[1].events);
      renderRunDetail();
    }).catch(function (error) {
      setText("run-detail-title", "Could not load run");
      setText("run-detail-meta", describe(error));
    });
}

function renderRunDetail() {
  var run = record(selectedRun);
  setText("run-detail-title", text(run.repository, "Repository") + " · " + text(run.goal_id, "Goal"));
  setText("run-detail-meta", text(run.status, "unknown") + " · " + text(run.run_id, "") +
    (run.pause_reason ? " · " + text(run.pause_reason, "") : ""));
  var timeline = el("run-timeline");
  if (timeline) {
    var entries = selectedRunEvents.map(function (event) {
      var row = document.createElement("li");
      row.appendChild(span("record-title", text(event.type, "event")));
      var payload = record(event.payload);
      var workflow = record(payload.workflow);
      var task = record(payload.task);
      var detail = new Date(Number(event.created_at) * 1000).toLocaleString();
      if (workflow.label) detail += " · " + text(workflow.label, "");
      if (task.id) detail += " · " + text(task.id, "");
      row.appendChild(span("record-meta", detail));
      return row;
    });
    if (entries.length === 0) entries.push(item("is-empty", "No transitions recorded."));
    timeline.replaceChildren.apply(timeline, entries);
  }
  var exportButton = el("run-export");
  var resumeButton = el("run-resume");
  var archiveButton = el("run-archive");
  if (exportButton) exportButton.disabled = false;
  if (resumeButton) resumeButton.disabled = run.resume_required !== true;
  if (archiveButton) {
    archiveButton.disabled = false;
    archiveButton.textContent = run.archived_at ? "Reopen" : "Archive";
  }
  var snapshot = record(run.snapshot);
  fillList("run-evidence", record(snapshot.completion).evidence, "No completion evidence recorded.");
  fillList("run-findings", record(snapshot.review).findings, "No review findings recorded.");
}

function applyTheme(theme) {
  if (theme === "dark" || theme === "light") document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
}

function loadPreferences() {
  apiGet("/api/preferences").then(function (payload) {
    preferences = Object.assign(preferences, record(payload.preferences));
    preferencesLoaded = true;
    LOG_VIEW_LINES = Number(preferences.log_lines) || 200;
    applyTheme(text(preferences.theme, "system"));
    var form = el("preferences-form");
    if (form) {
      form.elements.namedItem("theme").value = text(preferences.theme, "system");
      form.elements.namedItem("log_lines").value = String(LOG_VIEW_LINES);
      form.elements.namedItem("browser_notifications").checked = preferences.browser_notifications === true;
    }
  }).catch(function (error) {
    setText("preferences-feedback", "Could not load preferences: " + describe(error));
  });
}

function wireLogout() {
  var node = el("logout");
  if (!node) {
    return;
  }
  node.addEventListener("click", function () {
    if (csrfToken === "") {
      return;
    }
    node.disabled = true;
    fetch("/auth/logout", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
    })
      .then(answer)
      .then(function (result) {
        if (result.status !== 200) {
          throw new Error(text(result.payload.message, "logout failed"));
        }
        window.location.assign(text(result.payload.redirect, "/auth/login"));
      })
      .catch(function (error) {
        node.disabled = false;
        lastFailure = "logout failed: " + describe(error);
        paintConnection();
      });
  });
}

/*
 * After a normal state render, tell the owner whether the active repository
 * is coordination-ready or still needs setup, using only server-reported
 * readiness. Never overwrite in-flight switch feedback.
 */
function reportActiveRepositoryReadiness(state) {
  if (repositorySwitching) {
    return;
  }
  var active = text(record(state.repository_catalog).active, "");
  if (active === "") {
    return;
  }
  if (state.coordination_present === true) {
    repositoryReport("Active repository is coordination-ready.", "ok");
  } else {
    repositoryReport(
      "Active repository needs setup: start Codex in Terminal to initialize coordination.",
      "warn"
    );
  }
}

/* Live state feed ---------------------------------------------------------- */

function now() {
  return typeof performance === "object" && performance && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

function describe(error) {
  if (error && error.name === "AbortError") {
    return "the request timed out after " + REQUEST_TIMEOUT_MS + " ms";
  }
  if (error && typeof error.message === "string" && error.message !== "") {
    return error.message;
  }
  return "the state server did not respond";
}

function ago(milliseconds) {
  var seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) {
    return seconds + "s ago";
  }
  var minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return minutes + "m " + (seconds % 60) + "s ago";
  }
  return Math.floor(minutes / 60) + "h " + (minutes % 60) + "m ago";
}

function paintConnection() {
  var connection = el("connection");
  var age = lastSuccessAt === null ? null : now() - lastSuccessAt;
  var stale = age === null || age >= STALE_AFTER_MS;
  var status;
  var detail;

  if (lastSuccessAt === null && failures === 0) {
    status = "connecting";
    detail = "Requesting the first snapshot.";
  } else if (failures > 0 && stale) {
    status = "stale";
    detail = "No fresh snapshot: " + lastFailure + ".";
  } else if (failures > 0) {
    status = "reconnecting";
    detail = "The live feed is reconnecting: " + lastFailure + ".";
  } else if (stale) {
    status = "stale";
    detail = "The last snapshot is older than " + STALE_AFTER_MS / 1000 + " seconds.";
  } else {
    status = "connected";
    detail = "Receiving coordination changes from the live event stream.";
  }

  if (connection) {
    connection.setAttribute("data-connection", status);
  }
  setText("connection-label", status === "connected" ? "state feed" : status);
  setText("connection-detail", detail);
  var trouble =
    failures === 0
      ? ""
      : " " + failures + " failed attempt" + (failures === 1 ? "" : "s") + " since.";
  if (renderFailure !== "") {
    trouble += " The last snapshot could not be rendered: " + renderFailure + ".";
  }
  setText(
    "refresh-age",
    (lastSuccessAt === null
      ? "No snapshot received yet."
      : "Last successful refresh " + ago(age) + ".") + trouble
  );
}

function acceptState(state) {
  if (!state || typeof state !== "object" || Array.isArray(state)) {
    throw new Error("the state server returned an unexpected payload");
  }
  lastSuccessAt = now();
  failures = 0;
  lastFailure = "";
  renderFailure = "";
  render(state);
  paintConnection();
}

function probeAuthentication() {
  fetch(STATE_URL, { cache: "no-store", headers: { Accept: "application/json" } }).then(
    function (response) {
      if (response.status === 401) {
        var destination = window.location.pathname + window.location.search + window.location.hash;
        window.location.assign("/auth/login?next=" + encodeURIComponent(destination));
      }
    }
  );
}

function startStateFeed() {
  if (stateSource) {
    return;
  }
  var source = new EventSource(STATE_EVENTS_URL);
  stateSource = source;
  source.addEventListener("state", function (event) {
    if (source !== stateSource) {
      return;
    }
    try {
      acceptState(JSON.parse(event.data));
    } catch (error) {
      renderFailure = describe(error);
      paintConnection();
    }
  });
  source.addEventListener("open", function () {
    failures = 0;
    lastFailure = "";
    paintConnection();
  });
  source.addEventListener("error", function () {
    failures += 1;
    lastFailure = "live state feed disconnected";
    paintConnection();
    probeAuthentication();
  });
}

function stopStateFeed() {
  if (stateSource) {
    stateSource.close();
    stateSource = null;
  }
}

function restartStateFeed() {
  stopStateFeed();
  startStateFeed();
}

/* Administration pages ---------------------------------------------------- */

function apiGet(url) {
  return fetch(url, {
    cache: "no-store",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  }).then(answer).then(function (result) {
    if (result.status !== 200) {
      throw new Error(text(result.payload.message, "the server answered " + result.status));
    }
    return result.payload;
  });
}

function apiPost(url, payload) {
  var options = {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
  };
  if (payload !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }
  return fetch(url, options).then(answer);
}

function recordRow(title, badgeText, detail) {
  var row = document.createElement("li");
  var head = document.createElement("p");
  head.className = "record-head";
  var badge = span("badge", badgeText);
  setToneOnNode(badge, tone(badgeText));
  head.appendChild(badge);
  head.appendChild(span("record-title", title));
  row.appendChild(head);
  var meta = document.createElement("p");
  meta.className = "record-meta";
  meta.textContent = detail;
  row.appendChild(meta);
  return row;
}

function recordBlock(title, badgeText, detail) {
  var row = recordRow(title, badgeText, detail);
  var block = document.createElement("div");
  while (row.firstChild) block.appendChild(row.firstChild);
  return block;
}

function setToneOnNode(node, mood) {
  if (mood) {
    node.setAttribute("data-tone", mood);
  }
}

function loadActivity() {
  setText("activity-feedback", "Loading recent events…");
  apiGet("/api/activity?limit=200").then(function (payload) {
    var events = list(payload.events);
    var node = el("activity-events");
    setText("activity-feedback", events.length + " recent event" + (events.length === 1 ? "" : "s") + ".");
    node.replaceChildren.apply(node, events.slice().reverse().map(function (event) {
      return recordRow(
        text(event.event, "event"),
        text(event.outcome, "unknown"),
        new Date(Number(event.created_at) * 1000).toLocaleString() +
          " · " + text(event.subject, "local user") +
          (text(event.detail, "") ? " · " + text(event.detail, "") : "")
      );
    }));
  }).catch(function (error) {
    setText("activity-feedback", "Could not load activity: " + describe(error));
  });
}

function loadSessions() {
  setText("sessions-feedback", "Loading sessions…");
  apiGet("/api/sessions").then(function (payload) {
    var values = list(payload.sessions);
    var node = el("session-records");
    setText("sessions-feedback", values.length + " active session" + (values.length === 1 ? "" : "s") + ".");
    node.replaceChildren.apply(node, values.map(function (session) {
      var row = recordRow(
        text(session.display, text(session.subject, "local browser")),
        session.current === true ? "current" : "active",
        "Last seen " + new Date(Number(session.last_seen_at) * 1000).toLocaleString() +
          " · handle " + text(session.handle, "").slice(0, 12)
      );
      var button = document.createElement("button");
      button.type = "button";
      button.className = "control-button record-action";
      button.textContent = "Revoke";
      button.addEventListener("click", function () {
        button.disabled = true;
        apiPost("/api/sessions/revoke", { handle: session.handle }).then(function (result) {
          if (result.status !== 200) {
            throw new Error(text(result.payload.message, "revoke failed"));
          }
          if (session.current === true) {
            if (securityMode === "oidc") {
              window.location.assign("/auth/login");
            } else {
              window.location.reload();
            }
          } else {
            loadSessions();
          }
        }).catch(function (error) {
          button.disabled = false;
          setText("sessions-feedback", "Could not revoke session: " + describe(error));
        });
      });
      row.appendChild(button);
      return row;
    }));
  }).catch(function (error) {
    setText("sessions-feedback", "Could not load sessions: " + describe(error));
  });
}

function loadDiagnostics() {
  setText("diagnostics-feedback", "Running checks…");
  apiGet("/api/diagnostics").then(function (payload) {
    var checks = list(payload.checks);
    setText(
      "diagnostics-feedback",
      "Mode " + text(payload.mode, "unknown") + " · " +
        (payload.ok === true ? "all checks passed" : "one or more checks need attention")
    );
    var node = el("diagnostic-checks");
    node.replaceChildren.apply(node, checks.map(function (check) {
      return recordRow(
        text(check.name, "check"),
        check.ok === true ? "ok" : check.required === false ? "optional" : "attention",
        text(check.category, "system") + " · " + text(check.detail)
      );
    }));
  }).catch(function (error) {
    setText("diagnostics-feedback", "Could not run diagnostics: " + describe(error));
  });
}

function wireAdministration() {
  var activityRefresh = el("activity-refresh");
  var sessionsRefresh = el("sessions-refresh");
  var diagnosticsRefresh = el("diagnostics-refresh");
  if (activityRefresh) activityRefresh.addEventListener("click", loadActivity);
  if (sessionsRefresh) sessionsRefresh.addEventListener("click", loadSessions);
  if (diagnosticsRefresh) diagnosticsRefresh.addEventListener("click", loadDiagnostics);
  var revokeOthers = el("sessions-revoke-others");
  if (revokeOthers) revokeOthers.addEventListener("click", function () {
    revokeOthers.disabled = true;
    apiPost("/api/sessions/revoke-others").then(function (result) {
      if (result.status !== 200) throw new Error("revoke failed");
      loadSessions();
    }).catch(function (error) {
      setText("sessions-feedback", "Could not revoke sessions: " + describe(error));
    }).then(function () { revokeOthers.disabled = false; });
  });
  var createForm = el("repository-create-form");
  if (createForm) createForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var name = el("repository-create-name").value;
    setText("repository-create-feedback", "Creating repository…");
    apiPost("/api/repository/create", { name: name }).then(function (result) {
      if (result.status !== 201) throw new Error(text(result.payload.message, "create failed"));
      applyRepositoryCatalog(record(result.payload.repository_catalog));
      resetTerminalClientStateForSwitch();
      restartStateFeed();
      setText("repository-create-feedback", text(result.payload.message, "Repository created."));
    }).catch(function (error) {
      setText("repository-create-feedback", "Could not create repository: " + describe(error));
    });
  });
  var initializeForm = el("repository-initialize-form");
  if (initializeForm) initializeForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var projectName = el("repository-project-name").value;
    setText("repository-initialize-feedback", "Initializing coordination…");
    apiPost("/api/repository/initialize", { project_name: projectName }).then(function (result) {
      if (result.status !== 200) throw new Error(text(result.payload.message, "initialization failed"));
      setText("repository-initialize-feedback", text(result.payload.message, "Coordination initialized."));
      restartStateFeed();
    }).catch(function (error) {
      setText("repository-initialize-feedback", "Could not initialize: " + describe(error));
    });
  });
}

function wireDailyDriver() {
  var refresh = el("runs-refresh");
  var filter = el("runs-filter");
  var exportButton = el("run-export");
  var resumeButton = el("run-resume");
  var archiveButton = el("run-archive");
  var diffButton = el("run-diff-load");
  if (refresh) refresh.addEventListener("click", loadRuns);
  if (filter) filter.addEventListener("input", renderRunRecords);
  if (exportButton) exportButton.addEventListener("click", function () {
    if (!selectedRun) return;
    var contents = JSON.stringify({ run: selectedRun, events: selectedRunEvents }, null, 2);
    var url = URL.createObjectURL(new Blob([contents], { type: "application/json" }));
    var link = document.createElement("a");
    link.href = url;
    link.download = text(selectedRun.run_id, "coordinator-run") + ".json";
    link.click();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 0);
  });
  if (resumeButton) resumeButton.addEventListener("click", function () {
    var runId = text(record(selectedRun).run_id, "");
    if (runId === "") return;
    resumeButton.disabled = true;
    apiPost("/api/runs/" + encodeURIComponent(runId) + "/resume").then(function (result) {
      if (result.status !== 200) throw new Error("resume failed");
      runsLoaded = false;
      loadRuns();
      loadRunDetail(runId);
      restartStateFeed();
    }).catch(function (error) {
      setText("run-detail-meta", "Could not resume: " + describe(error));
      resumeButton.disabled = false;
    });
  });
  if (archiveButton) archiveButton.addEventListener("click", function () {
    var run = record(selectedRun);
    var runId = text(run.run_id, "");
    if (runId === "") return;
    var action = run.archived_at ? "reopen" : "archive";
    archiveButton.disabled = true;
    apiPost("/api/runs/" + encodeURIComponent(runId) + "/" + action).then(function (result) {
      if (result.status !== 200) throw new Error(action + " failed");
      runsLoaded = false;
      loadRuns();
      loadRunDetail(runId);
    }).catch(function (error) {
      setText("run-detail-meta", "Could not " + action + ": " + describe(error));
      archiveButton.disabled = false;
    });
  });
  if (diffButton) diffButton.addEventListener("click", function () {
    diffButton.disabled = true;
    setText("run-diff", "Loading bounded repository diff…");
    apiGet("/api/repository/diff").then(function (payload) {
      setText("run-diff", text(payload.diff, "Working tree is clean.") +
        (payload.truncated === true ? "\n\n[diff truncated at 512 KiB]" : ""));
    }).catch(function (error) {
      setText("run-diff", "Could not load diff: " + describe(error));
    }).then(function () { diffButton.disabled = false; });
  });

  var guardrailForm = el("guardrails-form");
  if (guardrailForm) guardrailForm.addEventListener("submit", function (event) {
    event.preventDefault();
    if (activeRunId === "") return;
    var policy = {};
    Object.keys(GuardrailDefaults).forEach(function (name) {
      var raw = guardrailForm.elements.namedItem(name).value.trim();
      policy[name] = raw === "" ? null : Number(raw);
    });
    setText("guardrails-feedback", "Saving guardrails…");
    apiPost("/api/runs/" + encodeURIComponent(activeRunId) + "/policy", policy)
      .then(function (result) {
        if (result.status !== 200) throw new Error(text(result.payload.message, "save failed"));
        setText("guardrails-feedback", "Guardrails saved. New snapshots use these limits immediately.");
        restartStateFeed();
      }).catch(function (error) {
        setText("guardrails-feedback", "Could not save guardrails: " + describe(error));
      });
  });

  var preferencesForm = el("preferences-form");
  if (preferencesForm) preferencesForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var wantsNotifications = preferencesForm.elements.namedItem("browser_notifications").checked;
    var permission = Promise.resolve();
    if (wantsNotifications && typeof Notification === "function" && Notification.permission === "default") {
      permission = Notification.requestPermission();
    }
    permission.then(function () {
      var payload = {
        theme: preferencesForm.elements.namedItem("theme").value,
        log_lines: Number(preferencesForm.elements.namedItem("log_lines").value),
        browser_notifications: wantsNotifications &&
          (typeof Notification !== "function" || Notification.permission === "granted"),
      };
      return apiPost("/api/preferences", payload);
    }).then(function (result) {
      if (result.status !== 200) throw new Error(text(result.payload.message, "save failed"));
      preferences = Object.assign(preferences, record(result.payload.preferences));
      LOG_VIEW_LINES = Number(preferences.log_lines) || 200;
      applyTheme(text(preferences.theme, "system"));
      setText("preferences-feedback", "Preferences saved.");
      if (latestState) renderLog(latestState);
    }).catch(function (error) {
      setText("preferences-feedback", "Could not save preferences: " + describe(error));
    });
  });
  loadPreferences();
}

function wireShortcuts() {
  document.addEventListener("keydown", function (event) {
    var target = event.target;
    var tag = target && target.tagName ? target.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "select" || tag === "textarea" || target === el("codex-terminal")) return;
    var key = String(event.key || "").toLowerCase();
    if (key === "?") {
      window.location.hash = "#settings";
      return;
    }
    if (!shortcutPrefix) {
      if (key === "g") {
        shortcutPrefix = true;
        window.setTimeout(function () { shortcutPrefix = false; }, 1200);
      }
      return;
    }
    shortcutPrefix = false;
    var destinations = { w: "monitor", r: "runs", l: "logs", s: "settings" };
    if (terminalEnabled) destinations.t = "terminal";
    if (destinations[key]) window.location.hash = "#" + destinations[key];
  });
}

/* Hash-based view navigation ------------------------------------------------ */

function routeFromHash() {
  var raw = String(window.location.hash || "").replace(/^#/, "");
  return ROUTES.indexOf(raw) === -1 ? DEFAULT_ROUTE : raw;
}

function applyRoute() {
  var route = routeFromHash();
  if (route === currentRoute) {
    return;
  }
  currentRoute = route;

  ROUTES.forEach(function (name) {
    var view = el("view-" + name);
    if (view) {
      view.hidden = name !== route;
    }
    var link = el("nav-" + name);
    if (link) {
      if (name === route) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    }
  });

  if (route !== "terminal") {
    closeCodexSocket();
  }
  if (route === "terminal") {
    if (!terminalEnabled) {
      window.location.hash = "#monitor";
      return;
    }
    terminalEverVisible = true;
    connectCodexSocket();
    if (codexTerminalReady) {
      scheduleCodexFitAndResize();
    }
  } else if (route === "activity") {
    loadActivity();
  } else if (route === "runs") {
    loadRuns();
  } else if (route === "settings" && !preferencesLoaded) {
    loadPreferences();
  } else if (route === "sessions") {
    loadSessions();
  } else if (route === "diagnostics") {
    loadDiagnostics();
  }
}

function wireNavigation() {
  window.addEventListener("hashchange", applyRoute);
  applyRoute();
}

function start() {
  wireControls();
  wireNavigation();
  wireCodexControls();
  wireRepositoryPicker();
  wireLogout();
  wireAdministration();
  wireDailyDriver();
  wireShortcuts();
  wireProviderUsage();
  paintConnection();
  startStateFeed();
  tickTimer = window.setInterval(paintConnection, POLL_INTERVAL_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      restartStateFeed();
    }
  });
  window.addEventListener("online", function () {
    restartStateFeed();
  });
  window.addEventListener("pagehide", function () {
    stopStateFeed();
    window.clearInterval(tickTimer);
    window.clearInterval(usageTimer);
    teardownCodexTerminal();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
