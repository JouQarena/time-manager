// Time Manager Companion — MV3 service worker.
//
// Responsibilities:
//   1. keep one authenticated WebSocket to ws://127.0.0.1:<port>/ alive,
//   2. report the *active* tab's domain + focus state on every change,
//   3. enforce the agent's BLOCK_DECISION answers inside pages,
//   4. never lie: if the agent is unreachable, nothing is counted.
//
// MV3 lifetime note: a service worker is normally evicted after ~30 s idle.
// Since Chrome 116 (our minimum) activity on a WebSocket resets that timer, so
// the 5 s HEARTBEAT below also serves as the keep-alive. If the worker is
// evicted anyway (sleep, extension reload), the agent stops counting website
// time after 15 s — failing closed, never inventing usage — until we reconnect
// on the next tab event, alarm or browser restart.

import {
  HEARTBEAT_INTERVAL_MS,
  HELLO_TIMEOUT_MS,
  RECONNECT_MAX_MS,
  RECONNECT_MIN_MS,
  buildBlockQuery,
  buildHeartbeat,
  buildHello,
  buildPong,
  buildTabActivity,
  detectBrowserName,
  parseAgentMessage,
  parseRules,
} from "../shared/protocol.js";
import { getSettings, markConnected, socketUrl } from "../shared/settings.js";
import { domainFromUrl, domainMatches } from "../shared/normalize.js";

const RECONNECT_ALARM = "tm-reconnect";
const STATE_ALARM = "tm-state-refresh";

/** In-memory session state (rebuilt on every worker start). */
const state = {
  socket: null,
  status: "disconnected", // disconnected | connecting | connected | error
  lastError: "",
  agentVersion: "",
  rules: new Map(), // domain -> rule payload
  decisions: new Map(), // domain -> {blocked, reason, message, reset_at}
  tabId: null,
  tabDomain: "",
  tabActive: false,
  windowFocused: true,
  reconnectDelay: RECONNECT_MIN_MS,
  heartbeatTimer: null,
  helloDeadline: null,
  browserId: "",
  port: 0,
  lastActivitySent: 0,
  sentCount: 0,
  receivedCount: 0,
};

// --------------------------------------------------------------------- logging
function log(...args) {
  console.debug("[time-manager]", ...args);
}

// ------------------------------------------------------------------ connection
async function ensureConnected(force = false) {
  const settings = await getSettings();
  state.browserId = settings.browserId;
  state.port = settings.port;

  if (!settings.enabled) {
    state.status = "disabled";
    closeSocket();
    return;
  }
  if (!settings.token) {
    state.status = "error";
    state.lastError = "No agent token configured. Open the extension options.";
    return;
  }
  if (state.socket && (state.socket.readyState === WebSocket.OPEN ||
                       state.socket.readyState === WebSocket.CONNECTING) && !force) {
    return;
  }
  closeSocket();
  state.status = "connecting";
  state.lastError = "";
  let socket;
  try {
    socket = new WebSocket(socketUrl(settings));
  } catch (error) {
    state.status = "error";
    state.lastError = String(error);
    scheduleReconnect();
    return;
  }
  state.socket = socket;

  socket.addEventListener("open", () => {
    log("socket open; sending HELLO");
    socket.send(JSON.stringify(buildHello({
      browser: detectBrowserName(),
      browserId: settings.browserId,
      token: settings.token,
    })));
    // If the agent does not answer HELLO in time, treat it as a failed link.
    state.helloDeadline = setTimeout(() => {
      if (state.status !== "connected") {
        state.lastError = "Agent did not reply to HELLO (wrong token or wrong port?).";
        socket.close();
      }
    }, HELLO_TIMEOUT_MS);
  });

  socket.addEventListener("message", (event) => {
    state.receivedCount += 1;
    handleAgentMessage(event.data);
  });

  socket.addEventListener("close", (event) => {
    stopHeartbeat();
    clearTimeout(state.helloDeadline);
    if (state.status === "connected") log("socket closed", event.code, event.reason);
    state.status = "disconnected";
    state.socket = null;
    scheduleReconnect();
  });

  socket.addEventListener("error", () => {
    // "close" always follows; keep this quiet to avoid console spam.
    state.lastError = state.lastError || "WebSocket error (is the agent running?)";
  });
}

function closeSocket() {
  stopHeartbeat();
  if (state.socket) {
    try {
      state.socket.close();
    } catch {
      /* already gone */
    }
  }
  state.socket = null;
}

function scheduleReconnect() {
  const delay = Math.min(state.reconnectDelay, RECONNECT_MAX_MS);
  state.reconnectDelay = Math.min(state.reconnectDelay * 2, RECONNECT_MAX_MS);
  // setTimeout keeps working while the worker lives; the alarm is the safety
  // net if the worker is evicted before it fires.
  setTimeout(() => ensureConnected(), delay);
  chrome.alarms.create(RECONNECT_ALARM, { delayInMinutes: 1 });
}

// ------------------------------------------------------------------ heartbeat
function startHeartbeat() {
  stopHeartbeat();
  state.heartbeatTimer = setInterval(() => {
    send(buildHeartbeat({ browser: detectBrowserName(), browserId: state.browserId }));
  }, HEARTBEAT_INTERVAL_MS);
}

function stopHeartbeat() {
  if (state.heartbeatTimer) clearInterval(state.heartbeatTimer);
  state.heartbeatTimer = null;
}

// ------------------------------------------------------------------ sending
function send(payload) {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return false;
  try {
    state.socket.send(JSON.stringify(payload));
    state.sentCount += 1;
    return true;
  } catch (error) {
    log("send failed", error);
    return false;
  }
}

// ------------------------------------------------------- agent -> extension
function handleAgentMessage(raw) {
  const message = parseAgentMessage(raw);
  if (!message) return;
  switch (message.type) {
    case "WELCOME":
      state.status = "connected";
      state.lastError = "";
      state.agentVersion = String(message.agent_version || "");
      state.reconnectDelay = RECONNECT_MIN_MS;
      state.rules = parseRules(message.website_rules);
      markConnected();
      clearTimeout(state.helloDeadline);
      startHeartbeat();
      broadcastBadge();
      pushDecisionsToTabs();
      reportTabState({ urgent: true }); // announce where we are right now
      break;
    case "RULE_UPDATE":
      state.rules = parseRules(message.website_rules);
      pushDecisionsToTabs();
      break;
    case "BLOCK_DECISION":
      applyDecision(message);
      break;
    case "PING":
      send(buildPong({ browserId: state.browserId }));
      break;
    case "ERROR":
      state.lastError = `${message.code}: ${message.message}`;
      if (message.code === "AUTH_FAILED") {
        state.status = "error";
        closeSocket(); // do not hammer the agent with a bad token
      }
      break;
    default:
      log("ignoring unknown agent message", message.type);
  }
}

function applyDecision(message) {
  if (typeof message.domain !== "string" || !message.domain) return;
  const decision = {
    domain: message.domain,
    blocked: message.blocked === true,
    reason: String(message.reason || "UNBLOCKED"),
    message: String(message.message || ""),
    resetAt: String(message.reset_at || ""),
  };
  state.decisions.set(decision.domain, decision);
  const rule = state.rules.get(decision.domain);
  if (rule) rule.blocked = decision.blocked;
  chrome.tabs.query({}, (tabs) => {
    for (const tab of tabs) {
      if (!tab.id || !tab.url) continue;
      const domain = domainFromUrl(tab.url);
      if (!domain || !domainMatches(decision.domain, domain)) continue;
      chrome.tabs.sendMessage(tab.id, { type: "DECISION", decision }).catch(() => {});
    }
  });
  broadcastBadge();
}

function pushDecisionsToTabs() {
  chrome.tabs.query({}, (tabs) => {
    for (const tab of tabs) {
      if (!tab.id || !tab.url) continue;
      const domain = domainFromUrl(tab.url);
      if (!domain) continue;
      const decision = decisionFor(domain);
      chrome.tabs.sendMessage(tab.id, { type: "DECISION", decision }).catch(() => {});
    }
  });
  broadcastBadge();
}

/** Best answer we can give a page right now (cached rules, never blocking). */
function decisionFor(domain) {
  if (!domain) return { domain: "", blocked: false, reason: "UNBLOCKED", message: "", resetAt: "" };
  const cached = state.decisions.get(domain);
  if (cached) return cached;
  for (const [ruleDomain, rule] of state.rules) {
    if (domainMatches(ruleDomain, domain)) {
      return {
        domain: ruleDomain,
        blocked: rule.blocked === true,
        reason: rule.reason || (rule.blocked ? "DAILY_LIMIT_REACHED" : "UNBLOCKED"),
        message: "",
        resetAt: "",
      };
    }
  }
  return { domain, blocked: false, reason: "UNBLOCKED", message: "", resetAt: "" };
}

// ------------------------------------------------------------ tab reporting
async function reportTabState({ urgent = false } = {}) {
  const browser = detectBrowserName();
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const windows = await chrome.windows.getLastFocused().catch(() => null);
  const windowFocused = windows ? windows.focused !== false : true;

  if (!tab) {
    state.tabId = null;
    state.tabDomain = "";
    state.tabActive = false;
    send(buildTabActivity({
      browser, browserId: state.browserId, tabId: -1, domain: "",
      active: false, windowFocused, audible: false,
    }));
    return;
  }

  const fullUrl = tab.url || tab.pendingUrl || "";
  const domain = domainFromUrl(fullUrl);
  state.tabId = tab.id ?? null;
  state.tabDomain = domain;
  state.tabActive = tab.active !== false;
  state.windowFocused = windowFocused;
  state.lastActivitySent = Date.now();

  send(buildTabActivity({
    browser,
    browserId: state.browserId,
    tabId: typeof tab.id === "number" && tab.id >= 0 ? tab.id : 0,
    domain,
    active: state.tabActive,
    windowFocused,
    audible: tab.audible === true,
  }));
  if (urgent) {
    const decision = decisionFor(domain);
    if (decision.blocked) {
      chrome.tabs.sendMessage(tab.id, { type: "DECISION", decision }).catch(() => {});
    }
  }
  broadcastBadge();
}

function broadcastBadge() {
  const blockedHere = state.tabDomain && decisionFor(state.tabDomain).blocked;
  const text = state.status === "connected" ? (blockedHere ? "⛔" : "") : "!";
  try {
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color: blockedHere ? "#c0392b" : "#888888" });
    chrome.action.setTitle({
      title: state.status === "connected"
        ? `Time Manager — connected to the agent (v${state.agentVersion})`
        : `Time Manager — ${state.status}: ${state.lastError || "agent not reachable"}`,
    });
  } catch {
    /* action API unavailable in some contexts */
  }
}

// ------------------------------------------------------------------ listeners
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(STATE_ALARM, { periodInMinutes: 1 });
  ensureConnected(true);
});

chrome.runtime.onStartup.addListener(() => {
  ensureConnected(true);
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM || alarm.name === STATE_ALARM) {
    if (!state.socket || state.socket.readyState > WebSocket.OPEN) ensureConnected();
  }
});

chrome.tabs.onActivated.addListener(() => reportTabState());
chrome.tabs.onRemoved.addListener(() => reportTabState());
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!changeInfo.url && changeInfo.status !== "complete") return;
  if (tab && tab.active) reportTabState();
});
chrome.windows.onFocusChanged.addListener(() => reportTabState());

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.agentToken || changes.agentPort || changes.agentHost || changes.enabled) {
    log("settings changed; reconnecting");
    state.status = "disconnected";
    state.decisions.clear();
    ensureConnected(true);
  }
});

// Messages from content scripts and the popup.
chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
  if (!request || typeof request.type !== "string") return undefined;
  switch (request.type) {
    case "GET_DECISION":
      sendResponse(decisionFor(String(request.domain || "")));
      return true;
    case "GET_STATUS":
      sendResponse({
        status: state.status,
        lastError: state.lastError,
        agentVersion: state.agentVersion,
        port: state.port,
        browserId: state.browserId,
        rules: Array.from(state.rules.values()),
        sentCount: state.sentCount,
        receivedCount: state.receivedCount,
        tabDomain: state.tabDomain,
      });
      return true;
    case "RECONNECT":
      ensureConnected(true);
      sendResponse({ status: "connecting" });
      return true;
    case "BLOCK_QUERY":
      send(buildBlockQuery({
        browser: detectBrowserName(),
        browserId: state.browserId,
        domain: String(request.domain || ""),
      }));
      sendResponse({ sent: true });
      return true;
    default:
      return undefined;
  }
});

// Keep the worker warm enough for the heartbeat chain.
setInterval(() => {
  if (state.status === "connected") return; // heartbeat already pings the agent
  if (state.status !== "connecting") ensureConnected();
}, 20000);

ensureConnected(true);
