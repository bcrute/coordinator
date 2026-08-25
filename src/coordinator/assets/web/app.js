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
var USAGE_HISTORY_URL = "/api/usage-history";
var USAGE_HISTORY_REFRESH_URL = "/api/usage-history/refresh";
var TERMINAL_SOCKET_URL = "/ws/terminal";
var REPOSITORY_SELECT_URL = "/api/repository/select";
var REPOSITORY_SELECT_TIMEOUT_MS = 30000;
var CONTROL_URLS = { start: "/api/watcher/start", stop: "/api/watcher/stop" };
var CODEX_CONTROL_URLS = {
  start: "/api/codex/start",
  resume: "/api/codex/resume",
  stop: "/api/codex/stop",
  clear: "/api/codex/clear",
};
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
var usageHistoryPayload = null;
var usageHistoryProvider = "";
var usageHistoryLoaded = false;
var usageHistoryLoading = false;
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
var executorSettingsSaveTimer = null;
var executorSettingsSaving = false;
var executorSettingsSaveQueued = false;
var executorSettingsLoaded = false;
var roleModelCatalogs = { codex: [], claude: [] };
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
var codexOnSelectionDisposable = null;
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
  "usage",
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
    value === "ready_for_review" ||
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
    value === "stale" ||
    value === "stopped" ||
    value === "stopping" ||
    value === "exited" ||
    value === "not_reviewed" ||
    value === "waiting_for_claude" ||
    value === "waiting_for_codex" ||
    value === "inactive" ||
    value === "needs_review"
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
    var detail = text(workflow.detail);
    if (workflow.phase === "blocked") {
      detail = "No executor is active — this goal is blocked. " + detail;
    }
    setText("workflow-detail", detail);
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
  setText("task-executor", text(task.executor, "configured"));
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
      : "No current executor handoff. The raw coder record below is historical."
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
  setText("review-next-executor", text(review.next_executor, "none"));
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
  setText("runtime-primary-model", text(runtime.primary_model, "Executor"));
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
      head.appendChild(span("record-title", text(entry.description, "Executor worker")));
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

function renderDelegations(values) {
  var node = el("delegations");
  var entries = values.filter(function (entry) {
    return entry && typeof entry === "object";
  });
  var running = entries.filter(function (entry) {
    return entry.state === "starting" || entry.state === "running";
  }).length;
  setText(
    "delegations-summary",
    entries.length === 0 ? "none recorded" : running + " running / " + entries.length + " recent"
  );
  if (!node) return;
  if (entries.length === 0) {
    node.replaceChildren(item("is-empty", "No local MCP delegations recorded."));
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
      if (mood) badge.setAttribute("data-tone", mood);
      head.appendChild(badge);
      head.appendChild(span("record-title", text(entry.objective, "Bounded implementation")));
      row.appendChild(head);
      var meta = document.createElement("p");
      meta.className = "record-meta";
      meta.textContent =
        text(entry.model, "local worker") +
        " · route " + text(entry.routing_score, "—") + "/10" +
        " · step " + count(entry.steps) +
        " · " + clock(entry.elapsed) +
        " · generated " + count(usage.output_tokens) +
        " · " + list(entry.changed_files).length + " files";
      row.appendChild(meta);
      var rationale = document.createElement("p");
      rationale.className = "record-meta";
      rationale.textContent = text(entry.routing_rationale, "Routing rationale not recorded.");
      row.appendChild(rationale);
      if (entry.error) {
        var error = document.createElement("p");
        error.className = "record-meta";
        error.textContent = "Error: " + text(entry.error);
        row.appendChild(error);
      }
      return row;
    })
  );
}

function renderWatchers(state) {
  var node = el("watchers");
  var entries = list(state.watchers).filter(function (entry) {
    return entry && typeof entry === "object" && entry.watcher_state !== "stale";
  });

  var activeCount = entries.filter(function (entry) {
    return entry.lock_present === true &&
      (entry.watcher_state === "running" || entry.watcher_state === "waiting");
  }).length;
  setText(
    "watchers-summary",
    entries.length === 0 ? "none recorded" : activeCount + " active · " + entries.length + " recorded"
  );
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

function fallbackCopyText(value) {
  var input = document.createElement("textarea");
  input.value = value;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.left = "-9999px";
  document.body.appendChild(input);
  input.select();
  var copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (error) {
    copied = false;
  }
  input.remove();
  if (codexTerminalReady && codexTerminal) codexTerminal.focus();
  return copied;
}

function copyCodexSelection() {
  var selection = codexTerminalReady && codexTerminal ? codexTerminal.getSelection() : "";
  if (!selection) {
    codexReport("Select terminal text before copying.", "warn");
    return;
  }
  function reportCopy(copied) {
    codexReport(
      copied
        ? "Copied " + selection.length + " characters from the terminal."
        : "Clipboard access was blocked by the browser.",
      copied ? "ok" : "bad"
    );
  }
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    navigator.clipboard.writeText(selection).then(
      function () { reportCopy(true); },
      function () { reportCopy(fallbackCopyText(selection)); }
    );
    return;
  }
  reportCopy(fallbackCopyText(selection));
}

function handleCodexCopyShortcut(event) {
  if (event.type !== "keydown" || !codexTerminalReady || !codexTerminal) return true;
  var key = String(event.key || "").toLowerCase();
  var hasSelection = codexTerminal.hasSelection();
  var explicitCopy = event.ctrlKey && event.shiftKey && key === "c";
  var platformCopy = event.metaKey && !event.altKey && key === "c";
  var insertCopy = event.ctrlKey && !event.altKey && key === "insert";
  var selectedControlC = event.ctrlKey && !event.altKey && key === "c" && hasSelection;
  if (!explicitCopy && !platformCopy && !insertCopy && !selectedControlC) return true;
  event.preventDefault();
  event.stopPropagation();
  copyCodexSelection();
  return false;
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
    codexTerminal.attachCustomKeyEventHandler(handleCodexCopyShortcut);
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
    codexOnSelectionDisposable = codexTerminal.onSelectionChange(paintCodexControls);
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
  if (
    codexOnSelectionDisposable &&
    typeof codexOnSelectionDisposable.dispose === "function"
  ) {
    codexOnSelectionDisposable.dispose();
    codexOnSelectionDisposable = null;
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

function renderTerminalProcessActivity(session) {
  var activity = record(session.process_activity);
  var agents = list(activity.agents).filter(function (entry) {
    return entry && typeof entry === "object";
  });
  var terminals = list(activity.background_terminals).filter(function (entry) {
    return entry && typeof entry === "object";
  });
  var agentNode = el("terminal-agents");
  var terminalNode = el("background-terminals");

  setText(
    "terminal-activity-summary",
    agents.length + " agent" + (agents.length === 1 ? "" : "s") +
      " · " + terminals.length + " background"
  );
  setTone(
    "terminal-activity-summary",
    activity.supported === false ? "warn" : terminals.length > 0 ? "active" : agents.length > 0 ? "ok" : ""
  );
  setText(
    "terminal-activity-detail",
    session.running !== true && agents.length === 0 && terminals.length === 0
      ? "No managed Codex session, executor, subagent, or background terminal is active."
      : text(activity.detail, "No managed terminal process activity is available.")
  );

  if (agentNode) {
    if (agents.length === 0) {
      agentNode.replaceChildren(item("is-empty", "No agents observed in this session."));
    } else {
      agentNode.replaceChildren.apply(
        agentNode,
        agents.map(function (entry) {
          var row = document.createElement("li");
          var head = document.createElement("p");
          head.className = "record-head";
          var badge = span("badge", text(entry.state, "unknown"));
          var mood = tone(entry.state);
          if (mood) badge.setAttribute("data-tone", mood);
          head.appendChild(badge);
          head.appendChild(
            span(
              "record-title",
              text(entry.label, "Agent") + " · " + text(entry.role, "nested")
            )
          );
          row.appendChild(head);

          var meta = document.createElement("p");
          meta.className = "record-meta";
          meta.textContent =
            text(entry.provider, "provider unknown") +
            " · model " + text(entry.model, "not reported") +
            (entry.subagent_model ? " · subagents " + text(entry.subagent_model) : "") +
            " · pid " + text(entry.pid, "unknown") +
            " · " + text(entry.os_state, "state unknown") +
            " · " + clock(entry.elapsed);
          row.appendChild(meta);
          return row;
        })
      );
    }
  }

  if (terminalNode) {
    if (terminals.length === 0) {
      terminalNode.replaceChildren(item("is-empty", "No background terminals observed."));
    } else {
      terminalNode.replaceChildren.apply(
        terminalNode,
        terminals.map(function (entry) {
          var row = document.createElement("li");
          var head = document.createElement("p");
          head.className = "record-head";
          var badge = span("badge", text(entry.state, "unknown"));
          var mood = tone(entry.state);
          if (mood) badge.setAttribute("data-tone", mood);
          head.appendChild(badge);
          head.appendChild(span("record-title", text(entry.title, "Background terminal")));
          row.appendChild(head);

          var meta = document.createElement("p");
          meta.className = "record-meta";
          meta.textContent =
            "pid " + text(entry.pid, "unknown") +
            " · " + text(entry.os_state, "state unknown") +
            " · " + clock(entry.elapsed) +
            " · " + count(entry.agent_count) + " agent" +
            (entry.agent_count === 1 ? "" : "s") +
            " · " + count(entry.process_count) + " process" +
            (entry.process_count === 1 ? "" : "es");
          row.appendChild(meta);
          return row;
        })
      );
    }
  }
}

function paintCodexControls() {
  var busy = codexPendingControl !== "" || repositorySwitching;
  var startNode = el("codex-session-start");
  var resumeNode = el("codex-session-resume");
  var stopNode = el("codex-session-stop");
  var clearNode = el("codex-terminal-clear");
  var copyNode = el("codex-terminal-copy");
  if (startNode) {
    startNode.disabled = !terminalEnabled || busy || codexSession.can_start !== true;
  }
  if (resumeNode) {
    resumeNode.disabled = !terminalEnabled || busy || codexSession.can_resume !== true;
  }
  if (stopNode) {
    stopNode.disabled = !terminalEnabled || busy || codexSession.can_stop !== true;
  }
  if (clearNode) {
    clearNode.disabled = !terminalEnabled || busy || !codexTerminalReady;
  }
  if (copyNode) {
    copyNode.disabled =
      !terminalEnabled || !codexTerminalReady || !codexTerminal.hasSelection();
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
  renderTerminalProcessActivity(session);
  var attachment = record(session.attachment);
  if (session.state === "exited") {
    codexTerminalWritable = false;
    var exitStatus = Number.isInteger(session.exit_code)
      ? " with status " + session.exit_code
      : "";
    codexReport(
      "Codex exited" + exitStatus + ". Resume previous to continue this thread, or start a new session.",
      session.exit_code === 0 ? "warn" : "bad"
    );
  } else if (attachment.mode) {
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
  codexReport(
    kind === "start"
      ? "Starting the Codex session…"
      : kind === "resume"
        ? "Resuming the previous Codex session…"
      : kind === "stop"
        ? "Stopping the Codex session…"
        : "Clearing retained terminal output…",
    "active"
  );

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
      if ((kind === "start" || kind === "resume") && result.status === 200) {
        if (codexTerminalReady && codexTerminal) {
          codexTerminal.reset();
        }
        connectCodexSocket();
      } else if (
        kind === "clear" &&
        result.status === 200 &&
        Number.isInteger(payload.cleared_through_cursor)
      ) {
        closeCodexSocket();
        codexTerminalCursor = payload.cleared_through_cursor;
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
  codexControl("clear");
}

function wireCodexControls() {
  var startNode = el("codex-session-start");
  var resumeNode = el("codex-session-resume");
  var stopNode = el("codex-session-stop");
  var clearNode = el("codex-terminal-clear");
  var copyNode = el("codex-terminal-copy");
  if (startNode) {
    startNode.addEventListener("click", function () {
      codexControl("start");
    });
  }
  if (resumeNode) {
    resumeNode.addEventListener("click", function () {
      codexControl("resume");
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
  if (copyNode) {
    copyNode.addEventListener("click", copyCodexSelection);
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
  renderDelegations(list(state.delegations));
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

function usageResetShort(value) {
  if (typeof value !== "string" || value === "") return "reset unknown";
  var parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "reset unknown";
  return parsed.toLocaleString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function usagePaceForecast(windowValue, nowMilliseconds) {
  var details = record(windowValue);
  var reported = record(details.forecast);
  if (reported.method === "unavailable") {
    return { tone: "neutral", remaining: null, method: "unavailable" };
  }
  if (typeof reported.projected_remaining === "number" &&
      Number.isFinite(reported.projected_remaining)) {
    var reportedRemaining = reported.projected_remaining;
    return {
      tone: reportedRemaining <= 0 ? "bad" : reportedRemaining <= 20 ? "warn" : "ok",
      remaining: reportedRemaining,
      method: text(reported.method, "rolling_velocity"),
      burnRate: reported.burn_rate_percent_per_hour,
      sustainableRate: reported.sustainable_rate_percent_per_hour,
      velocityRatio: reported.velocity_ratio,
      sampleCount: reported.sample_count,
      basisHours: reported.basis_hours,
      confidence: text(reported.confidence, "unknown"),
    };
  }
  var reset = new Date(details.resets_at);
  var duration = details.duration_minutes;
  var used = details.used_percent;
  if (typeof details.resets_at !== "string" ||
      !Number.isFinite(reset.getTime()) || typeof duration !== "number" ||
      duration <= 0 || typeof used !== "number") {
    return { tone: "neutral", remaining: null, method: "unavailable" };
  }
  var durationMilliseconds = duration * 60 * 1000;
  var elapsed = durationMilliseconds - (reset.getTime() - nowMilliseconds);
  var projectedUsed = elapsed <= 0 ? used : used * durationMilliseconds / elapsed;
  var projectedRemaining = 100 - projectedUsed;
  if (projectedRemaining <= 0) {
    return { tone: "bad", remaining: projectedRemaining, method: "reset_average",
      burnRate: elapsed <= 0 ? 0 : used / (elapsed / 3600000), confidence: "fallback" };
  }
  if (projectedRemaining <= 20) {
    return { tone: "warn", remaining: projectedRemaining, method: "reset_average",
      burnRate: elapsed <= 0 ? 0 : used / (elapsed / 3600000), confidence: "fallback" };
  }
  return { tone: "ok", remaining: projectedRemaining, method: "reset_average",
    burnRate: elapsed <= 0 ? 0 : used / (elapsed / 3600000), confidence: "fallback" };
}

function usageVelocityLabel(forecast) {
  if (typeof forecast.burnRate !== "number" || !Number.isFinite(forecast.burnRate)) {
    return "collecting pace";
  }
  var prefix = forecast.method === "rolling_velocity" ? "" : "avg ";
  return prefix + forecast.burnRate.toFixed(forecast.burnRate < 0.1 ? 2 : 1) + "%/h";
}

function usageForecastDetail(forecast) {
  if (forecast.method !== "rolling_velocity") {
    return "reset-average fallback while collecting rolling observations";
  }
  var detail = "rolling velocity over " + Number(forecast.basisHours || 0).toFixed(1) +
    "h from " + Number(forecast.sampleCount || 0) + " observations";
  if (typeof forecast.velocityRatio === "number" && Number.isFinite(forecast.velocityRatio)) {
    detail += "; " + forecast.velocityRatio.toFixed(2) + "× sustainable pace";
  }
  return detail + "; " + forecast.confidence + " confidence";
}

function usageWindowChip(windowValue) {
  var details = record(windowValue);
  var remaining = details.remaining_percent;
  var chip = document.createElement("span");
  var name = document.createElement("span");
  var label = document.createElement("span");
  var reset = document.createElement("small");
  var value = document.createElement("strong");
  var forecastCell = document.createElement("span");
  var projection = document.createElement("strong");
  var velocity = document.createElement("small");
  var forecast = usagePaceForecast(details, Date.now());
  chip.className = "usage-chip";
  name.className = "usage-window-name";
  label.textContent = text(details.label, "Usage");
  reset.className = "usage-window-reset";
  reset.textContent = "· " + usageResetShort(details.resets_at);
  value.dataset.tone = usageTone(remaining);
  value.textContent = usagePercent(remaining);
  projection.className = "usage-window-projection";
  projection.dataset.tone = forecast.tone;
  projection.textContent = usagePercent(forecast.remaining);
  forecastCell.className = "usage-window-forecast";
  velocity.className = "usage-window-velocity";
  velocity.textContent = usageVelocityLabel(forecast);
  forecastCell.append(projection, velocity);
  name.append(label, reset);
  chip.append(name, value, forecastCell);
  chip.title = label.textContent + ": " + value.textContent + " remaining; " +
    projection.textContent + " projected remaining at reset; " +
    usageForecastDetail(forecast) + "; " + usageReset(details.resets_at);
  return chip;
}

function renderProviderUsage(payload) {
  var providers = list(record(payload).providers);
  [
    {
      id: "codex",
      provider: el("usage-codex"),
      value: el("usage-codex-value"),
      plan: el("usage-codex-plan"),
    },
    {
      id: "claude",
      provider: el("usage-claude"),
      value: el("usage-claude-value"),
      plan: el("usage-claude-plan"),
    },
  ].forEach(function (target) {
    var provider = providers.find(function (candidate) {
      return text(record(candidate).id) === target.id;
    });
    var details = record(provider);
    var remaining = details.remaining_percent;
    var providerElement = target.provider;
    var value = target.value;
    if (!providerElement || !value) return;
    var title = text(details.name, target.id === "codex" ? "Codex" : "Claude");
    var plan = text(details.plan);
    var stale = details.status === "stale" || details.stale === true;
    providerElement.dataset.status = text(details.status, "unavailable");
    if (target.plan) {
      target.plan.textContent = [plan, stale ? "stale" : ""].filter(Boolean).join(" · ");
      target.plan.dataset.stale = stale ? "true" : "false";
    }
    if (plan) title += " " + plan;
    var windows = list(details.windows);
    if (!windows.length && typeof remaining === "number") {
      windows = [{ label: "Remaining", remaining_percent: remaining }];
    }
    value.replaceChildren();
    if (windows.length) {
      windows.forEach(function (windowValue) {
        value.append(usageWindowChip(windowValue));
      });
      title += " — " + windows.map(function (windowValue) {
        var windowDetails = record(windowValue);
        return text(windowDetails.label, "rolling") + ": " +
          usagePercent(windowDetails.remaining_percent) + " remaining, " +
          usageReset(windowDetails.resets_at);
      }).join("; ");
    } else {
      var unavailable = usageWindowChip({
        label: "Unavailable",
        remaining_percent: null,
      });
      unavailable.title = text(details.message, "Usage unavailable");
      value.append(unavailable);
      title += " — " + text(details.message, "Usage unavailable");
    }
    if (stale) {
      var lastSuccess = new Date(details.last_success_at).getTime();
      title += " — stale: " + text(details.message, "The latest refresh failed.");
      if (Number.isFinite(lastSuccess)) {
        title += " Last updated " + new Date(lastSuccess).toLocaleString() + ".";
      }
    }
    providerElement.title = title;
  });
  var refresh = el("usage-refresh");
  if (refresh) refresh.disabled = record(payload).refreshing === true;
}

function loadProviderUsage() {
  return fetch(PROVIDER_USAGE_URL, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  }).then(answer).then(function (result) {
    if (result.status < 200 || result.status >= 300) {
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
    if (result.status < 200 || result.status >= 300) {
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

/* Historical provider value ---------------------------------------------- */

function usageMoney(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (value > 0 && value < 0.01) return "$" + value.toFixed(4);
  return "$" + value.toFixed(2);
}

function usageAxis(value, metric) {
  if (metric === "cost") return usageMoney(value);
  if (value >= 1000000) return (value / 1000000).toFixed(1) + "M";
  if (value >= 1000) return (value / 1000).toFixed(0) + "k";
  return counts.format(Math.round(value));
}

function usageSvg(name, attributes, content) {
  var namespace = ["http:", "", "www.w3.org", "2000", "svg"].join("/");
  var node = document.createElementNS(namespace, name);
  Object.keys(attributes || {}).forEach(function (key) {
    node.setAttribute(key, attributes[key]);
  });
  if (typeof content === "string") node.textContent = content;
  return node;
}

function renderUsageChart(provider, payload) {
  var chart = el("usage-history-chart");
  var wrap = el("usage-chart-wrap");
  if (!chart || !wrap) return;
  var metric = text(provider.metric, "tokens");
  var series = list(provider.series).map(function (point) {
    return {
      at: new Date(record(point).timestamp).getTime(),
      value: metric === "cost" ? Number(record(point).cost_usd || 0) : Number(record(point).tokens || 0),
    };
  }).filter(function (point) {
    return Number.isFinite(point.at) && Number.isFinite(point.value);
  }).sort(function (left, right) { return left.at - right.at; });
  chart.replaceChildren();
  chart.append(
    usageSvg("title", { id: "usage-chart-title" }, text(provider.name, "Provider") + " usage over time"),
    usageSvg("desc", { id: "usage-chart-description" },
      "Cumulative " + (metric === "cost" ? "estimated API value" : "token usage") + " for the selected range.")
  );
  if (!series.length) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  var start = new Date(record(payload).from || series[0].at).getTime();
  var end = new Date(record(payload).to || series[series.length - 1].at).getTime();
  if (!Number.isFinite(start)) start = series[0].at;
  if (!Number.isFinite(end) || end <= start) end = start + 1;
  var running = 0;
  var cumulative = [{ at: start, value: 0 }];
  series.forEach(function (point) {
    running += point.value;
    cumulative.push({ at: point.at, value: running, increment: point.value });
  });
  var max = Math.max(running, metric === "cost" ? 0.01 : 1);
  var left = 72, right = 24, top = 20, bottom = 44;
  var width = 960 - left - right, height = 320 - top - bottom;
  function x(at) { return left + ((at - start) / (end - start)) * width; }
  function y(value) { return top + height - (value / max) * height; }
  for (var grid = 0; grid <= 4; grid += 1) {
    var amount = max * grid / 4;
    var gridY = y(amount);
    chart.append(
      usageSvg("line", { x1: left, x2: left + width, y1: gridY, y2: gridY, class: "usage-grid" }),
      usageSvg("text", { x: left - 10, y: gridY + 4, class: "usage-axis-label", "text-anchor": "end" }, usageAxis(amount, metric))
    );
  }
  var path = cumulative.map(function (point, index) {
    return (index === 0 ? "M" : "L") + x(point.at).toFixed(1) + " " + y(point.value).toFixed(1);
  }).join(" ");
  chart.append(usageSvg("path", { d: path, class: "usage-value-line" }));
  cumulative.slice(1).forEach(function (point) {
    var marker = usageSvg("circle", {
      cx: x(point.at).toFixed(1), cy: y(point.value).toFixed(1), r: 3,
      class: "usage-value-point", tabindex: "0",
    });
    marker.append(usageSvg("title", {}, new Date(point.at).toLocaleString() + " — +" +
      usageAxis(point.increment, metric) + ", cumulative " + usageAxis(point.value, metric)));
    chart.append(marker);
  });
  [start, start + (end - start) / 2, end].forEach(function (at, index) {
    chart.append(usageSvg("text", {
      x: x(at), y: 304, class: "usage-axis-label",
      "text-anchor": index === 0 ? "start" : index === 2 ? "end" : "middle",
    }, new Date(at).toLocaleDateString([], { month: "short", day: "numeric", hour: "numeric" })));
  });
}

function renderUsageHistory(payload) {
  usageHistoryPayload = payload;
  var providers = list(record(payload).providers);
  var tabs = el("usage-history-tabs");
  if (!tabs) return;
  if (!providers.some(function (provider) { return text(record(provider).id, "") === usageHistoryProvider; })) {
    usageHistoryProvider = providers.length ? text(record(providers[0]).id, "") : "";
  }
  tabs.replaceChildren.apply(tabs, providers.map(function (providerValue) {
    var provider = record(providerValue);
    var button = document.createElement("button");
    var selected = text(provider.id, "") === usageHistoryProvider;
    button.type = "button";
    button.className = "provider-tab";
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", selected ? "true" : "false");
    button.textContent = text(provider.name, text(provider.id, "Provider"));
    button.addEventListener("click", function () {
      usageHistoryProvider = text(provider.id, "");
      renderUsageHistory(usageHistoryPayload);
    });
    return button;
  }));
  var selected = providers.find(function (provider) {
    return text(record(provider).id, "") === usageHistoryProvider;
  });
  var details = record(selected);
  var totals = record(details.totals);
  var feedback = el("usage-history-feedback");
  var summary = el("usage-value-summary");
  var modelsBody = el("usage-model-rows");
  if (!selected) {
    if (feedback) feedback.textContent = "No usage adapters are configured.";
    if (summary) summary.hidden = true;
    if (modelsBody) modelsBody.replaceChildren();
    renderUsageChart({}, payload);
    return;
  }
  var metric = text(details.metric, "tokens");
  var totalTokens = Number(totals.total_tokens || 0);
  if (feedback) {
    feedback.textContent = details.status === "error" ? text(details.message, "Usage import failed.") :
      totalTokens ? "Imported native telemetry. Costs are API-equivalent estimates, not subscription charges." :
        record(payload).refreshing === true ? "Importing native telemetry…" : "No usage was found in this range.";
  }
  if (summary) summary.hidden = false;
  setText("usage-value-label", metric === "cost" ? text(details.cost_label, "Estimated API value") : "Recorded usage");
  setText("usage-value-total", metric === "cost" ? usageMoney(Number(totals.cost_usd || 0)) : counts.format(totalTokens) + " tokens");
  setText("usage-token-total", counts.format(totalTokens));
  setText("usage-price-coverage", typeof details.coverage_percent === "number" ? details.coverage_percent.toFixed(1) + "%" : "not applicable");
  if (modelsBody) {
    var rows = list(details.models).map(function (modelValue) {
      var model = record(modelValue);
      var row = document.createElement("tr");
      row.append(
        document.createElement("td"), document.createElement("td"), document.createElement("td")
      );
      row.children[0].textContent = text(model.model, "unknown");
      row.children[1].textContent = counts.format(Number(model.tokens || 0));
      row.children[2].textContent = metric === "cost" && Number(model.valued_tokens || 0) > 0 ?
        usageMoney(Number(model.cost_usd || 0)) : "—";
      return row;
    });
    if (!rows.length) {
      var empty = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = 3;
      cell.textContent = "No model usage in this range.";
      empty.append(cell);
      rows.push(empty);
    }
    modelsBody.replaceChildren.apply(modelsBody, rows);
  }
  renderUsageChart(details, payload);
  var refresh = el("usage-history-refresh");
  if (refresh) refresh.disabled = record(payload).refreshing === true || usageHistoryLoading;
}

function loadUsageHistory(force) {
  if (usageHistoryLoading) return Promise.resolve();
  usageHistoryLoading = true;
  var rangeNode = el("usage-history-range");
  var range = rangeNode ? rangeNode.value : "7d";
  var url = (force ? USAGE_HISTORY_REFRESH_URL : USAGE_HISTORY_URL) + "?range=" + encodeURIComponent(range);
  var options = { cache: "no-store", headers: { Accept: "application/json" } };
  if (force) {
    options.method = "POST";
    options.headers["X-CSRF-Token"] = csrfToken;
  }
  return fetch(url, options).then(answer).then(function (result) {
    if (result.status < 200 || result.status >= 300) throw new Error("usage history request failed");
    usageHistoryLoaded = true;
    renderUsageHistory(result.payload);
    if (!force && !record(result.payload).generated_at) {
      window.setTimeout(function () { loadUsageHistory(false); }, 1500);
    }
  }).catch(function () {
    setText("usage-history-feedback", "Usage history is temporarily unavailable.");
  }).finally(function () {
    usageHistoryLoading = false;
    var refresh = el("usage-history-refresh");
    if (refresh) refresh.disabled = false;
  });
}

function wireUsageHistory() {
  var range = el("usage-history-range");
  var refresh = el("usage-history-refresh");
  if (range) range.addEventListener("change", function () { loadUsageHistory(false); });
  if (refresh) refresh.addEventListener("click", function () { loadUsageHistory(true); });
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

function toggleExecutorFields() {
  var form = el("executor-settings-form");
  if (!form) return;
  var strategy = form.elements.namedItem("execution_strategy").value;
  var local = strategy !== "claude" || form.elements.namedItem("primary_adapter").value === "mini-swe-agent";
  el("local-model-settings").hidden = !local;
  var supervisor = document.querySelector('.role-card[data-role="supervisor"]');
  if (supervisor) supervisor.dataset.inactive = strategy === "mini-swe-agent" ? "true" : "false";
  setText(
    "executor-fallback",
    strategy === "claude-local"
      ? "Fallback: Claude keeps rejected, risky, or failed local work."
      : strategy === "mini-swe-agent"
        ? "Fallback: none; a failed local turn stops for review."
        : "Fallback: Claude handles the turn directly."
  );
  updateHandoffBudgetSummary();
}

function updateHandoffBudgetSummary() {
  var form = el("executor-settings-form");
  if (!form) return;
  var strategy = form.elements.namedItem("execution_strategy").value;
  var local = strategy === "mini-swe-agent";
  var limit = Number(form.elements.namedItem(local ? "mini_swe_step_limit" : "claude_max_turns").value);
  var unitCost = local ? 4 : 6;
  if (!Number.isFinite(limit) || limit < unitCost + 2) {
    setText("handoff-budget-summary", "The runtime limit is too small for one work unit and verification reserve.");
    return;
  }
  var reserve = Math.max(2, Math.ceil(limit * 0.25));
  var units = Math.max(1, Math.floor((limit - reserve) / unitCost));
  setText(
    "handoff-budget-summary",
    "Task ceiling: " + units + " work unit" + (units === 1 ? "" : "s") +
      "; " + reserve + " of " + limit + " " + (local ? "steps" : "turns") +
      " reserved for verification and recovery."
  );
}

function togglePrimaryFields() {
  var form = el("executor-settings-form");
  if (!form) return;
  var selected = form.elements.namedItem("primary_adapter").value;
  Array.prototype.forEach.call(document.querySelectorAll("[data-primary-runtime]"), function (node) {
    node.hidden = node.dataset.primaryRuntime !== selected;
  });
  toggleExecutorFields();
}

function setRoleHealth(nodeId, status) {
  var node = el(nodeId);
  if (!node) return;
  if (status.active === false) {
    node.textContent = "not in path";
    node.dataset.tone = "neutral";
  } else if (status.executable_available === true) {
    node.textContent = "CLI found";
    node.dataset.tone = "ok";
  } else {
    node.textContent = "missing";
    node.dataset.tone = "bad";
  }
}

function ensureModelOption(control, value) {
  if (!control || control.tagName !== "SELECT") return;
  var exists = Array.prototype.some.call(control.options, function (option) {
    return option.value === String(value);
  });
  if (!exists) {
    var option = document.createElement("option");
    option.value = String(value);
    option.textContent = String(value) + " (saved; not currently advertised)";
    option.dataset.stale = "true";
    control.appendChild(option);
  }
}

function populateModelSelect(control, models, options) {
  if (!control) return;
  var settings = options || {};
  var selected = control.value;
  var entries = [];
  if (settings.allowDefault) {
    var defaultModel = models.find(function (model) { return record(model).default === true; });
    var defaultLabel = defaultModel ? text(record(defaultModel).label, text(record(defaultModel).id, "")) : "provider default";
    var fallback = document.createElement("option");
    fallback.value = "";
    fallback.textContent = "CLI default (" + defaultLabel + ")";
    entries.push(fallback);
  }
  models.forEach(function (rawModel) {
    var model = record(rawModel);
    var identifier = text(model.id, "");
    if (!identifier) return;
    var option = document.createElement("option");
    option.value = identifier;
    option.textContent = text(model.label, identifier);
    if (model.description) option.title = text(model.description, "");
    entries.push(option);
  });
  if (selected && !entries.some(function (option) { return option.value === selected; })) {
    var stale = document.createElement("option");
    stale.value = selected;
    stale.textContent = selected + " (saved; not currently advertised)";
    stale.dataset.stale = "true";
    entries.push(stale);
  }
  control.replaceChildren.apply(control, entries);
  if (entries.some(function (option) { return option.value === selected; })) control.value = selected;
}

function populateEffortSelect(control, modelControl, models, options) {
  if (!control || !modelControl) return;
  var settings = options || {};
  var selected = control.value;
  var model = models.find(function (candidate) {
    return text(record(candidate).id, "") === modelControl.value;
  });
  if (!model && modelControl.value === "") {
    model = models.find(function (candidate) { return record(candidate).default === true; });
  }
  var modelRecord = record(model);
  var defaultEffort = text(modelRecord.default_effort, "");
  var fallback = document.createElement("option");
  fallback.value = "";
  fallback.textContent = settings.inherit
    ? "Inherit supervisor"
    : "Model default" + (defaultEffort ? " (" + defaultEffort + ")" : "");
  var entries = [fallback];
  list(modelRecord.efforts).forEach(function (rawEffort) {
    var effort = record(rawEffort);
    var identifier = text(effort.id, "");
    if (!identifier) return;
    var option = document.createElement("option");
    option.value = identifier;
    option.textContent = identifier === "xhigh" ? "Xhigh" : identifier.charAt(0).toUpperCase() + identifier.slice(1);
    if (effort.description) option.title = text(effort.description, "");
    entries.push(option);
  });
  if (selected && !entries.some(function (option) { return option.value === selected; })) {
    var stale = document.createElement("option");
    stale.value = selected;
    stale.textContent = selected + " (saved; unsupported by selected model)";
    stale.dataset.stale = "true";
    entries.push(stale);
  }
  control.replaceChildren.apply(control, entries);
  if (entries.some(function (option) { return option.value === selected; })) control.value = selected;
}

function refreshRoleEfforts(form) {
  populateEffortSelect(
    form.elements.namedItem("codex_effort"),
    form.elements.namedItem("codex_model"),
    roleModelCatalogs.codex
  );
  populateEffortSelect(
    form.elements.namedItem("primary_claude_effort"),
    form.elements.namedItem("primary_claude_model"),
    roleModelCatalogs.claude
  );
  populateEffortSelect(
    form.elements.namedItem("claude_effort"),
    form.elements.namedItem("claude_model"),
    roleModelCatalogs.claude
  );
  populateEffortSelect(
    form.elements.namedItem("claude_subagent_effort"),
    form.elements.namedItem("claude_subagent_model"),
    roleModelCatalogs.claude,
    { inherit: true }
  );
}

function loadRoleModelCatalogs() {
  var form = el("executor-settings-form");
  if (!form) return Promise.resolve();
  return Promise.all([
    apiGet("/api/executor-settings/models?source=codex"),
    apiGet("/api/executor-settings/models?source=claude"),
  ]).then(function (catalogs) {
    roleModelCatalogs.codex = list(catalogs[0].models);
    roleModelCatalogs.claude = list(catalogs[1].models);
    populateModelSelect(
      form.elements.namedItem("codex_model"),
      roleModelCatalogs.codex,
      { allowDefault: true }
    );
    var claudeModels = roleModelCatalogs.claude;
    populateModelSelect(form.elements.namedItem("primary_claude_model"), claudeModels);
    populateModelSelect(form.elements.namedItem("claude_model"), claudeModels);
    populateModelSelect(form.elements.namedItem("claude_subagent_model"), claudeModels);
    refreshRoleEfforts(form);
  }).catch(function (error) {
    setText("executor-settings-feedback", "Could not load installed CLI models: " + describe(error));
  });
}

function renderExecutorSettings(payload) {
  var configuration = record(payload.configuration);
  var status = record(payload.status);
  var form = el("executor-settings-form");
  if (!form) return;
  Object.keys(configuration).forEach(function (name) {
    var control = form.elements.namedItem(name);
    ensureModelOption(control, configuration[name]);
    if (control && control.type === "checkbox") control.checked = configuration[name] === true;
    else if (control) control.value = String(configuration[name]);
  });
  var permission = form.elements.namedItem("codex_permission_mode");
  if (permission) permission.dataset.savedValue = permission.value;
  var strategy = configuration.executor_adapter === "mini-swe-agent"
    ? "mini-swe-agent"
    : configuration.claude_local_delegation === true ? "claude-local" : "claude";
  form.elements.namedItem("execution_strategy").value = strategy;
  togglePrimaryFields();
  toggleExecutorFields();
  var state = el("executor-settings-state");
  var roles = record(status.roles);
  var reviewerRole = record(roles.reviewer);
  var supervisorRole = record(roles.supervisor);
  var executorRole = record(roles.executor);
  var pipelineReady = reviewerRole.executable_available === true &&
    executorRole.executable_available === true &&
    (supervisorRole.active === false || supervisorRole.executable_available === true);
  state.textContent = pipelineReady ? "pipeline available" : "setup required";
  state.dataset.tone = pipelineReady ? "ok" : "bad";
  setRoleHealth("reviewer-role-health", reviewerRole);
  setRoleHealth("supervisor-role-health", supervisorRole);
  setRoleHealth("executor-role-health", executorRole);
  if (status.load_warning) {
    setText("executor-settings-feedback", text(status.load_warning, "Stored settings were ignored."));
  }
}

function loadExecutorSettings() {
  return apiGet("/api/executor-settings").then(function (payload) {
    executorSettingsLoaded = true;
    renderExecutorSettings(payload);
    return loadRoleModelCatalogs();
  }).catch(function (error) {
    setText("executor-settings-feedback", "Could not load executor settings: " + describe(error));
  });
}

function executorSettingsPayload(form) {
  var strategy = form.elements.namedItem("execution_strategy").value;
  return {
    primary_adapter: form.elements.namedItem("primary_adapter").value,
    primary_claude_model: form.elements.namedItem("primary_claude_model").value.trim(),
    primary_claude_effort: form.elements.namedItem("primary_claude_effort").value,
    primary_local_model: form.elements.namedItem("primary_local_model").value.trim(),
    primary_local_effort: form.elements.namedItem("primary_local_effort").value,
    primary_local_step_limit: Number(form.elements.namedItem("primary_local_step_limit").value),
    primary_local_timeout_seconds: Number(form.elements.namedItem("primary_local_timeout_seconds").value),
    codex_model: form.elements.namedItem("codex_model").value.trim(),
    codex_effort: form.elements.namedItem("codex_effort").value,
    codex_permission_mode: form.elements.namedItem("codex_permission_mode").value,
    executor_adapter: strategy === "mini-swe-agent" ? "mini-swe-agent" : "claude",
    claude_model: form.elements.namedItem("claude_model").value.trim(),
    claude_effort: form.elements.namedItem("claude_effort").value,
    claude_subagent_model: form.elements.namedItem("claude_subagent_model").value.trim(),
    claude_subagent_effort: form.elements.namedItem("claude_subagent_effort").value,
    claude_max_turns: Number(form.elements.namedItem("claude_max_turns").value),
    claude_local_delegation: strategy === "claude-local",
    mini_swe_model: form.elements.namedItem("mini_swe_model").value.trim(),
    mini_swe_effort: form.elements.namedItem("mini_swe_effort").value,
    mini_swe_api_base: form.elements.namedItem("mini_swe_api_base").value.trim(),
    mini_swe_provider: form.elements.namedItem("mini_swe_provider").value.trim(),
    mini_swe_api_key_env: form.elements.namedItem("mini_swe_api_key_env").value.trim(),
    mini_swe_step_limit: Number(form.elements.namedItem("mini_swe_step_limit").value),
    mini_swe_cost_limit: Number(form.elements.namedItem("mini_swe_cost_limit").value),
    mini_swe_timeout_seconds: Number(form.elements.namedItem("mini_swe_timeout_seconds").value),
  };
}

function persistExecutorSettings(form) {
  if (executorSettingsSaving) {
    executorSettingsSaveQueued = true;
    return;
  }
  var strategy = form.elements.namedItem("execution_strategy").value;
  var localModel = form.elements.namedItem("mini_swe_model").value.trim();
  var localPrimary = form.elements.namedItem("primary_adapter").value === "mini-swe-agent";
  var primaryLocalModel = form.elements.namedItem("primary_local_model").value.trim();
  if (!form.checkValidity() || (strategy !== "claude" && localModel === "") || (localPrimary && primaryLocalModel === "")) {
    setText("executor-settings-feedback", "Complete the highlighted fields to save these selections.");
    return;
  }
  executorSettingsSaving = true;
  setText("executor-settings-feedback", "Saving agent selections…");
  apiPost("/api/executor-settings", executorSettingsPayload(form)).then(function (result) {
    if (result.status !== 200) {
      throw new Error(text(result.payload.message, "save failed"));
    }
    var permission = form.elements.namedItem("codex_permission_mode");
    if (permission) permission.dataset.savedValue = permission.value;
    form.dataset.dirty = "false";
    setText("executor-settings-feedback", text(result.payload.message, "Agent selections saved."));
    restartStateFeed();
  }).catch(function (error) {
    setText("executor-settings-feedback", "Could not save agent selections: " + describe(error));
  }).finally(function () {
    executorSettingsSaving = false;
    if (executorSettingsSaveQueued) {
      executorSettingsSaveQueued = false;
      scheduleExecutorSettingsSave(form, 0);
    }
  });
}

function scheduleExecutorSettingsSave(form, delay) {
  form.dataset.dirty = "true";
  if (executorSettingsSaveTimer !== null) window.clearTimeout(executorSettingsSaveTimer);
  executorSettingsSaveTimer = window.setTimeout(function () {
    executorSettingsSaveTimer = null;
    persistExecutorSettings(form);
  }, typeof delay === "number" ? delay : 500);
}

function applyRoleProfile(name, form) {
  var profiles = {
    frontier: { strategy: "claude", claude: "opus", effort: "high", subagent: "sonnet", subagentEffort: "high" },
    balanced: { strategy: "claude", claude: "sonnet", effort: "high", subagent: "sonnet", subagentEffort: "medium" },
    "local-heavy": { strategy: "claude-local", claude: "sonnet", effort: "high", subagent: "sonnet", subagentEffort: "medium" },
  };
  var profile = profiles[name];
  if (!profile) return;
  form.elements.namedItem("execution_strategy").value = profile.strategy;
  form.elements.namedItem("claude_model").value = profile.claude;
  form.elements.namedItem("claude_subagent_model").value = profile.subagent;
  refreshRoleEfforts(form);
  form.elements.namedItem("claude_effort").value = profile.effort;
  form.elements.namedItem("claude_subagent_effort").value = profile.subagentEffort;
  toggleExecutorFields();
  form.dataset.dirty = "true";
  setText("executor-settings-feedback", "Profile applied. Review the models, then save the role assignment.");
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

function renderRepositoryCI(value) {
  var ci = record(value);
  var confirmation = el("repository-ci-confirmation");
  var workflows = el("repository-ci-workflows");
  if (!confirmation || !workflows) return;
  var requiresConfirmation = ci.requires_confirmation === true;
  confirmation.hidden = !requiresConfirmation;
  workflows.replaceChildren.apply(workflows, list(ci.workflows).map(function (workflow) {
    var item = document.createElement("li");
    item.textContent = text(workflow, "GitHub Actions workflow");
    return item;
  }));
  var repository = text(ci.github_repository, "");
  var message = text(ci.message, "Choose how Coordinator should handle CI.");
  if (repository !== "") message += " GitHub repository: " + repository + ".";
  setText("repository-ci-message", message);
}

function initializeActiveRepository(ciAction) {
  var projectName = el("repository-project-name").value;
  var body = { project_name: projectName };
  if (ciAction) body.ci_action = ciAction;
  setText(
    "repository-initialize-feedback",
    ciAction === "add" ? "Adding Coordinator CI…" :
      ciAction === "skip" ? "Keeping existing CI…" : "Initializing coordination…"
  );
  return apiPost("/api/repository/initialize", body).then(function (result) {
    if (result.status !== 200) {
      throw new Error(text(result.payload.message, "initialization failed"));
    }
    setText("repository-initialize-feedback", text(result.payload.message, "Coordination initialized."));
    renderRepositoryCI(result.payload.ci);
    restartStateFeed();
  }).catch(function (error) {
    setText("repository-initialize-feedback", "Could not initialize: " + describe(error));
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
    initializeActiveRepository();
  });
  var ciAdd = el("repository-ci-add");
  if (ciAdd) ciAdd.addEventListener("click", function () {
    initializeActiveRepository("add");
  });
  var ciSkip = el("repository-ci-skip");
  if (ciSkip) ciSkip.addEventListener("click", function () {
    initializeActiveRepository("skip");
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
  var executorForm = el("executor-settings-form");
  if (executorForm) {
    executorForm.addEventListener("input", function (event) {
      scheduleExecutorSettingsSave(executorForm);
    });
    executorForm.addEventListener("change", function (event) {
      scheduleExecutorSettingsSave(executorForm, 100);
    });
    executorForm.elements.namedItem("execution_strategy").addEventListener("change", toggleExecutorFields);
    executorForm.elements.namedItem("primary_adapter").addEventListener("change", togglePrimaryFields);
    executorForm.elements.namedItem("mini_swe_step_limit").addEventListener("input", updateHandoffBudgetSummary);
    executorForm.elements.namedItem("claude_max_turns").addEventListener("input", updateHandoffBudgetSummary);
    var effortForModel = {
      codex_model: "codex_effort",
      primary_claude_model: "primary_claude_effort",
      claude_model: "claude_effort",
      claude_subagent_model: "claude_subagent_effort",
    };
    Object.keys(effortForModel).forEach(function (name) {
      executorForm.elements.namedItem(name).addEventListener("change", function () {
        executorForm.elements.namedItem(effortForModel[name]).value = "";
        refreshRoleEfforts(executorForm);
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-role-profile]"), function (button) {
      button.addEventListener("click", function () {
        applyRoleProfile(button.dataset.roleProfile, executorForm);
        scheduleExecutorSettingsSave(executorForm, 0);
      });
    });
    executorForm.addEventListener("submit", function (event) {
      event.preventDefault();
      scheduleExecutorSettingsSave(executorForm, 0);
    });
  }
  var discover = el("executor-discover");
  if (discover && executorForm) discover.addEventListener("click", function () {
    discover.disabled = true;
    setText("executor-settings-feedback", "Discovering models…");
    apiPost("/api/executor-settings/discover", {
      api_base: executorForm.elements.namedItem("mini_swe_api_base").value.trim(),
      api_key_env: executorForm.elements.namedItem("mini_swe_api_key_env").value.trim(),
    }).then(function (result) {
      if (result.status !== 200) throw new Error(text(result.payload.message, "discovery failed"));
      var models = list(result.payload.models);
      populateModelSelect(
        executorForm.elements.namedItem("mini_swe_model"),
        models.map(function (model) {
          return { id: text(model, ""), label: text(model, ""), description: "Endpoint model" };
        })
      );
      populateModelSelect(
        executorForm.elements.namedItem("primary_local_model"),
        models.map(function (model) {
          return { id: text(model, ""), label: text(model, ""), description: "Endpoint model" };
        })
      );
      if (models.length === 1) executorForm.elements.namedItem("mini_swe_model").value = models[0];
      if (models.length === 1) executorForm.elements.namedItem("primary_local_model").value = models[0];
      setText("executor-settings-feedback", "Discovered " + models.length + " model" + (models.length === 1 ? "." : "s."));
    }).catch(function (error) {
      setText("executor-settings-feedback", "Could not discover models: " + describe(error));
    }).then(function () { discover.disabled = false; });
  });
  var rolesTest = el("roles-test");
  if (rolesTest && executorForm) rolesTest.addEventListener("click", function () {
    rolesTest.disabled = true;
    setText("executor-settings-feedback", "Checking configured runtimes…");
    apiGet("/api/executor-settings").then(function (payload) {
      renderExecutorSettings(payload);
      var strategy = executorForm.elements.namedItem("execution_strategy").value;
      if (strategy === "claude") return null;
      return apiPost("/api/executor-settings/discover", {
        api_base: executorForm.elements.namedItem("mini_swe_api_base").value.trim(),
        api_key_env: executorForm.elements.namedItem("mini_swe_api_key_env").value.trim(),
      }).then(function (result) {
        if (result.status !== 200) throw new Error(text(result.payload.message, "local endpoint unavailable"));
        return list(result.payload.models).length;
      });
    }).then(function (models) {
      setText(
        "executor-settings-feedback",
        models === null
          ? "CLI readiness updated."
          : "CLI readiness updated; local endpoint returned " + models + " model" + (models === 1 ? "." : "s.")
      );
    }).catch(function (error) {
      setText("executor-settings-feedback", "Runtime check failed: " + describe(error));
    }).then(function () { rolesTest.disabled = false; });
  });
  loadPreferences();
  loadExecutorSettings();
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
    var destinations = { w: "monitor", r: "runs", l: "logs", s: "settings", u: "usage" };
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
  } else if (route === "usage") {
    loadUsageHistory(false);
  } else if (route === "runs") {
    loadRuns();
  } else if (route === "settings") {
    if (!preferencesLoaded) loadPreferences();
    if (!executorSettingsLoaded) loadExecutorSettings();
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
  wireUsageHistory();
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
