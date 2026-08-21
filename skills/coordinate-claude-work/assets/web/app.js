/*
 * Render the read-only coordination state API as a live dashboard.
 *
 * Every value from the API reaches the page through textContent or a fixed
 * attribute value, so no API string is ever parsed as HTML.
 */
"use strict";

var STATE_URL = "/api/state";
var REPOSITORY_SELECT_URL = "/api/repository/select";
var REPOSITORY_SELECT_TIMEOUT_MS = 30000;
var CONTROL_URLS = { start: "/api/watcher/start", stop: "/api/watcher/stop" };
var CODEX_CONTROL_URLS = { start: "/api/codex/start", stop: "/api/codex/stop" };
var CODEX_CONTROL_TIMEOUT_MS = 30000;
var CODEX_OUTPUT_URL = "/api/codex/output";
var CODEX_INPUT_URL = "/api/codex/input";
var CODEX_RESIZE_URL = "/api/codex/resize";
var CODEX_OUTPUT_POLL_MS = 250;
var CODEX_OUTPUT_TIMEOUT_MS = 4000;
var CODEX_INPUT_TIMEOUT_MS = 4000;
var CODEX_RESIZE_TIMEOUT_MS = 4000;
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
var pollTimer = null;
var tickTimer = null;
var inFlight = false;
var lastSuccessAt = null;
var failures = 0;
var lastFailure = "";
var renderFailure = "";
var managed = Object.create(null);
var pendingControl = "";
var renderedLog = null;
var csrfToken = "";

var repositoryCatalog = { root: "", active: "", entries: [] };
var repositorySwitching = false;
var stateEpoch = 0;
var pollRefreshQueued = false;

var codexTerminal = null;
var codexTerminalReady = false;
var codexFitAddon = null;
var codexSession = Object.create(null);
var codexPendingControl = "";

var codexOutputCursor = null;
var codexOutputTimer = null;
var codexOutputInFlight = false;
var codexOutputPollActive = false;
var codexOutputRunningKnown = false;
var codexOutputGeneration = 0;

var codexInputQueue = [];
var codexInputInFlight = false;
var codexOnDataDisposable = null;

var codexResizeTimer = null;
var codexLastSentRows = null;
var codexLastSentCols = null;
var codexResizeObserver = null;
var codexResizeFallbackWired = false;

var ROUTES = ["monitor", "terminal", "work", "agents", "logs"];
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
  if (value === "accepted" || value === "completed" || value === "done" || value === "running") {
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
  if (value === "blocked" || value === "failed" || value === "error" || value === "changes_requested") {
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
  stopCodexOutputPolling();
  codexOutputGeneration += 1;
  codexOutputCursor = null;
  codexOutputRunningKnown = false;
  if (codexTerminalReady && codexTerminal) {
    codexTerminal.reset();
  }
  /*
   * Only clear queued unsent input here. Any in-flight input request keeps
   * codexInputInFlight true until its own promise settles and releases it,
   * so we never clobber a serialization flag we do not own.
   */
  codexInputQueue = [];
  codexLastSentRows = null;
  codexLastSentCols = null;
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
        schedule(0);
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
      schedule(0);
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
      queueCodexInput(data);
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

/* Codex output polling ----------------------------------------------------- */

function applyCodexOutput(output) {
  if (!codexTerminalReady || !codexTerminal) {
    return;
  }
  var payload = output && typeof output === "object" ? output : {};
  var chunk = typeof payload.text === "string" ? payload.text : "";
  var nextCursor =
    typeof payload.next_cursor === "number" && isFinite(payload.next_cursor)
      ? payload.next_cursor
      : null;
  var reset = payload.reset === true;
  if (reset) {
    var hadCursor = codexOutputCursor !== null;
    codexTerminal.reset();
    if (chunk !== "") {
      codexTerminal.write(chunk);
    }
    if (hadCursor) {
      codexReport(
        "The retained output cursor expired; replayed the retained terminal history.",
        "warn"
      );
    }
  } else if (chunk !== "") {
    codexTerminal.write(chunk);
  }
  if (nextCursor !== null) {
    codexOutputCursor = nextCursor;
  }
}

function pollCodexOutput(isFinal) {
  if (codexOutputInFlight) {
    return;
  }
  codexOutputInFlight = true;
  var myGeneration = codexOutputGeneration;
  var controller = typeof AbortController === "function" ? new AbortController() : null;
  var timeout = window.setTimeout(function () {
    if (controller) {
      controller.abort();
    }
  }, CODEX_OUTPUT_TIMEOUT_MS);
  var url =
    CODEX_OUTPUT_URL +
    (codexOutputCursor === null ? "" : "?cursor=" + encodeURIComponent(String(codexOutputCursor)));
  var options = { cache: "no-store", headers: { Accept: "application/json" } };
  if (controller) {
    options.signal = controller.signal;
  }

  fetch(url, options)
    .then(function (response) {
      if (!response.ok) {
        throw new Error("the output server answered " + response.status);
      }
      return response.json();
    })
    .then(function (payload) {
      if (myGeneration !== codexOutputGeneration) {
        return;
      }
      if (payload && typeof payload === "object") {
        applyCodexOutput(payload.output);
      }
    })
    .catch(function (error) {
      if (myGeneration !== codexOutputGeneration) {
        return;
      }
      codexReport("Output poll failed: " + describe(error), "bad");
    })
    .then(function () {
      window.clearTimeout(timeout);
      codexOutputInFlight = false;
      /*
       * codexOutputInFlight is released above regardless of generation, so
       * a stale in-flight request never blocks the current generation's
       * polling. Reschedule whenever polling is still active, regardless of
       * this call's generation or isFinal flag, so an active poll loop is
       * never silently dropped by a stale/final call; the payload/error
       * handlers above remain generation-guarded so stale data is ignored.
       */
      if (codexOutputPollActive) {
        codexOutputTimer = window.setTimeout(function () {
          pollCodexOutput(false);
        }, CODEX_OUTPUT_POLL_MS);
      }
    });
}

function startCodexOutputPolling() {
  if (codexOutputPollActive) {
    return;
  }
  codexOutputPollActive = true;
  window.clearTimeout(codexOutputTimer);
  pollCodexOutput(false);
}

function stopCodexOutputPolling() {
  codexOutputPollActive = false;
  window.clearTimeout(codexOutputTimer);
}

function manageCodexOutputPolling() {
  var running = codexSession.running === true;
  if (running && !codexOutputRunningKnown) {
    codexOutputRunningKnown = true;
    startCodexOutputPolling();
  } else if (!running && codexOutputRunningKnown) {
    codexOutputRunningKnown = false;
    stopCodexOutputPolling();
    pollCodexOutput(true);
  }
}

/* Codex input --------------------------------------------------------------- */

function queueCodexInput(data) {
  if (typeof data !== "string" || data === "") {
    return;
  }
  for (var i = 0; i < data.length; i += CODEX_INPUT_CHUNK_CHARS) {
    codexInputQueue.push(data.slice(i, i + CODEX_INPUT_CHUNK_CHARS));
  }
  drainCodexInputQueue();
}

function drainCodexInputQueue() {
  if (codexInputInFlight || codexInputQueue.length === 0) {
    return;
  }
  var chunk = codexInputQueue.shift();
  codexInputInFlight = true;
  var controller = typeof AbortController === "function" ? new AbortController() : null;
  var timeout = window.setTimeout(function () {
    if (controller) {
      controller.abort();
    }
  }, CODEX_INPUT_TIMEOUT_MS);
  var options = {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify({ data: chunk }),
  };
  if (controller) {
    options.signal = controller.signal;
  }

  fetch(CODEX_INPUT_URL, options)
    .then(function (response) {
      if (!response.ok) {
        throw new Error("the input server answered " + response.status);
      }
    })
    .catch(function (error) {
      codexReport("Input send failed: " + describe(error), "bad");
    })
    .then(function () {
      window.clearTimeout(timeout);
      codexInputInFlight = false;
      drainCodexInputQueue();
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
  var controller = typeof AbortController === "function" ? new AbortController() : null;
  var timeout = window.setTimeout(function () {
    if (controller) {
      controller.abort();
    }
  }, CODEX_RESIZE_TIMEOUT_MS);
  var options = {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify({ rows: rows, cols: cols }),
  };
  if (controller) {
    options.signal = controller.signal;
  }

  fetch(CODEX_RESIZE_URL, options)
    .then(function (response) {
      if (!response.ok) {
        throw new Error("the resize server answered " + response.status);
      }
    })
    .catch(function (error) {
      codexReport("Resize failed: " + describe(error), "bad");
    })
    .then(function () {
      window.clearTimeout(timeout);
    });
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
  window.clearTimeout(codexOutputTimer);
  window.clearTimeout(codexResizeTimer);
  codexOutputPollActive = false;
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
    startNode.disabled = busy || codexSession.can_start !== true;
  }
  if (stopNode) {
    stopNode.disabled = busy || codexSession.can_stop !== true;
  }
  if (clearNode) {
    clearNode.disabled = !codexTerminalReady;
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
  paintCodexControls();
  manageCodexOutputPolling();
}

function renderCodexSession(state) {
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
        codexOutputGeneration += 1;
        codexOutputCursor = null;
        if (codexTerminalReady && codexTerminal) {
          codexTerminal.reset();
        }
        stopCodexOutputPolling();
        startCodexOutputPolling();
      }
    })
    .catch(function (error) {
      codexReport("the " + kind + " request failed: " + describeControl(error), "bad");
    })
    .then(function () {
      window.clearTimeout(timeout);
      codexPendingControl = "";
      paintCodexControls();
      schedule(0);
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
  initCodexTerminal();
  paintCodexControls();
}

function render(state) {
  var security = record(state.security);
  csrfToken = text(security.csrf_token, "");
  var user = record(security.user);
  var userNode = el("authenticated-user");
  var logoutNode = el("logout");
  var authenticated = security.authenticated === true;
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
  reportActiveRepositoryReadiness(state);
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

/* Polling ----------------------------------------------------------------- */

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
    detail = "Retrying every second: " + lastFailure + ".";
  } else if (stale) {
    status = "stale";
    detail = "The last snapshot is older than " + STALE_AFTER_MS / 1000 + " seconds.";
  } else {
    status = "connected";
    detail = "Coordination snapshot refreshed every second; agent activity is shown separately.";
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

function schedule(delay) {
  if (delay === 0 && inFlight) {
    /*
     * A poll is already in flight, so an immediate setTimeout(poll, 0)
     * would just find inFlight true and no-op, silently losing the
     * refresh until the next 1s tick. Queue it instead so the in-flight
     * request's completion triggers an immediate refresh.
     */
    pollRefreshQueued = true;
    return;
  }
  window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(poll, delay);
}

function poll() {
  if (inFlight) {
    return;
  }
  inFlight = true;
  var pollEpoch = stateEpoch;
  var controller = typeof AbortController === "function" ? new AbortController() : null;
  var timeout = window.setTimeout(function () {
    if (controller) {
      controller.abort();
    }
  }, REQUEST_TIMEOUT_MS);
  var options = { cache: "no-store", headers: { Accept: "application/json" } };
  if (controller) {
    options.signal = controller.signal;
  }

  fetch(STATE_URL, options)
    .then(function (response) {
      if (response.status === 401) {
        var destination = window.location.pathname + window.location.search + window.location.hash;
        window.location.assign("/auth/login?next=" + encodeURIComponent(destination));
        throw new Error("the authenticated session expired");
      }
      if (!response.ok) {
        throw new Error("the state server answered " + response.status);
      }
      return response.json();
    })
    .then(function (state) {
      if (!state || typeof state !== "object" || Array.isArray(state)) {
        throw new Error("the state server returned an unexpected payload");
      }
      lastSuccessAt = now();
      failures = 0;
      lastFailure = "";
      if (pollEpoch !== stateEpoch) {
        return;
      }
      try {
        renderFailure = "";
        render(state);
      } catch (error) {
        renderFailure = describe(error);
      }
    })
    .catch(function (error) {
      failures += 1;
      lastFailure = describe(error);
    })
    .then(function () {
      window.clearTimeout(timeout);
      inFlight = false;
      paintConnection();
      if (pollRefreshQueued) {
        pollRefreshQueued = false;
        schedule(0);
      } else {
        schedule(POLL_INTERVAL_MS);
      }
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

  if (route === "terminal") {
    terminalEverVisible = true;
    if (codexTerminalReady) {
      scheduleCodexFitAndResize();
    }
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
  paintConnection();
  poll();
  tickTimer = window.setInterval(paintConnection, POLL_INTERVAL_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      schedule(0);
    }
  });
  window.addEventListener("online", function () {
    schedule(0);
  });
  window.addEventListener("pagehide", function () {
    window.clearTimeout(pollTimer);
    window.clearInterval(tickTimer);
    teardownCodexTerminal();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
