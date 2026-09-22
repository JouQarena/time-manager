// Popup: connection status + live usage for the limited sites.
// All data comes from the service worker's cached state, so the popup opens
// instantly even when the agent is unreachable.

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(total % 60).padStart(2, "0")}s`;
  return `${total}s`;
}

const STATUS_TEXT = {
  connected: (s) => `Connected to the agent (v${s.agentVersion || "?"}) — port ${s.port}`,
  connecting: () => "Connecting to the agent…",
  disconnected: () => "Agent not reachable — nothing is being counted",
  disabled: () => "Turned off in settings",
  error: (s) => s.lastError || "Error talking to the agent",
};

function renderStatus(status) {
  const dot = document.getElementById("status-dot");
  dot.className = "dot " + (status.status === "connected"
    ? "ok"
    : status.status === "connecting" ? "warn" : "bad");
  const line = document.getElementById("status-line");
  line.textContent = (STATUS_TEXT[status.status] || (() => status.status))(status);

  const error = document.getElementById("error");
  if (status.status === "error" && status.lastError) {
    error.textContent = status.lastError;
    error.classList.remove("hidden");
  } else {
    error.classList.add("hidden");
  }
}

function renderCurrent(status) {
  document.getElementById("current-domain").textContent = status.tabDomain || "no web page";
  const state = document.getElementById("current-state");
  const rules = status.rules || [];
  const match = rules.find(
    (rule) => status.tabDomain &&
      (status.tabDomain === rule.domain || status.tabDomain.endsWith("." + rule.domain)),
  );
  if (!match) {
    state.textContent = "Not limited";
    state.className = "muted";
    return;
  }
  state.textContent = match.blocked ? `Blocked (${match.reason})` : "Counting towards your limit";
  state.className = match.blocked ? "state blocked" : "state free";
}

function renderRules(status) {
  const list = document.getElementById("rules");
  const empty = document.getElementById("no-rules");
  const rules = (status.rules || []).slice().sort((a, b) => a.domain.localeCompare(b.domain));
  list.replaceChildren();
  if (rules.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  for (const rule of rules) {
    const item = document.createElement("li");

    const row = document.createElement("div");
    row.className = "row";
    const domain = document.createElement("span");
    domain.className = "domain";
    domain.textContent = rule.domain;
    const state = document.createElement("span");
    state.className = "state " + (rule.blocked ? "blocked" : "free");
    state.textContent = rule.blocked
      ? (rule.reason === "SCHEDULE_BLOCKED" ? "outside schedule" : "blocked")
      : `${formatDuration(rule.remainingSeconds)} left`;
    row.append(domain, state);

    item.appendChild(row);

    if (typeof rule.usedSeconds === "number" && rule.limitSeconds) {
      const bar = document.createElement("div");
      bar.className = "bar";
      const fill = document.createElement("span");
      const ratio = Math.min(1, rule.usedSeconds / rule.limitSeconds);
      fill.style.width = `${Math.round(ratio * 100)}%`;
      if (ratio >= 1) fill.className = "full";
      else if (ratio >= 0.8) fill.className = "high";
      bar.appendChild(fill);
      item.appendChild(bar);

      const used = document.createElement("span");
      used.className = "used";
      used.textContent = `${formatDuration(rule.usedSeconds)} of ` +
        `${formatDuration(rule.limitSeconds)} today`;
      item.appendChild(used);
    }
    list.appendChild(item);
  }
}

function ask() {
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, (status) => {
    if (chrome.runtime.lastError || !status) {
      renderStatus({ status: "error", lastError: "Service worker unavailable." });
      return;
    }
    renderStatus(status);
    renderCurrent(status);
    renderRules(status);
  });
}

document.getElementById("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "RECONNECT" }, () => ask());
});
document.getElementById("options").addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

ask();
setInterval(ask, 2000);
