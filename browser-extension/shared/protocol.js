// Wire protocol v1 — MUST mirror app/ipc/protocol.py and docs/PROTOCOL.md.
//
// The extension is the client side of a local-only WebSocket. Everything here
// is deliberately defensive: malformed agent messages must never throw in the
// service worker, and unknown message types must be ignored rather than
// crashing (the agent may add messages in later versions).

export const PROTOCOL_VERSION = "1.0.0";
export const EXTENSION_VERSION = "0.2.0";

export const HEARTBEAT_INTERVAL_MS = 5000;
export const RECONNECT_MIN_MS = 1000;
export const RECONNECT_MAX_MS = 30000;
export const HELLO_TIMEOUT_MS = 5000;

export const DEFAULT_PORT = 17846;
export const DEFAULT_HOST = "127.0.0.1";

export const INBOUND_TYPES = {
  WELCOME: "WELCOME",
  BLOCK_DECISION: "BLOCK_DECISION",
  RULE_UPDATE: "RULE_UPDATE",
  PING: "PING",
  ERROR: "ERROR",
};

export const BLOCK_REASONS = {
  DAILY_LIMIT_REACHED: "DAILY_LIMIT_REACHED",
  SESSION_LIMIT_REACHED: "SESSION_LIMIT_REACHED",
  SCHEDULE_BLOCKED: "SCHEDULE_BLOCKED",
  MANUAL: "MANUAL",
  UNBLOCKED: "UNBLOCKED",
};

export function nowMs() {
  return Date.now();
}

export function buildHello({ browser, browserId, token }) {
  return {
    type: "HELLO",
    browser,
    browser_id: browserId,
    version: EXTENSION_VERSION,
    token,
    timestamp: nowMs(),
  };
}

export function buildTabActivity({
  browser,
  browserId,
  tabId,
  domain,
  active,
  windowFocused,
  audible,
}) {
  return {
    type: "TAB_ACTIVITY",
    browser,
    browser_id: browserId,
    tab_id: tabId,
    domain: domain || "",
    active: Boolean(active),
    window_focused: Boolean(windowFocused),
    audible: Boolean(audible),
    timestamp: nowMs(),
  };
}

export function buildHeartbeat({ browser, browserId }) {
  return { type: "HEARTBEAT", browser, browser_id: browserId, timestamp: nowMs() };
}

export function buildBlockQuery({ browser, browserId, domain }) {
  return { type: "BLOCK_QUERY", browser, browser_id: browserId, domain, timestamp: nowMs() };
}

export function buildPong({ browserId }) {
  return { type: "PONG", browser_id: browserId, timestamp: nowMs() };
}

/** Parse an agent message; returns null for anything unusable. */
export function parseAgentMessage(raw) {
  if (typeof raw !== "string" || raw.length > 64 * 1024) return null;
  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!message || typeof message !== "object" || typeof message.type !== "string") return null;
  return message;
}

/** Normalize `website_rules` from WELCOME/RULE_UPDATE into a Map by domain. */
export function parseRules(websiteRules) {
  const rules = new Map();
  if (!Array.isArray(websiteRules)) return rules;
  for (const entry of websiteRules) {
    if (!entry || typeof entry.domain !== "string" || !entry.domain) continue;
    rules.set(entry.domain, {
      domain: entry.domain,
      blocked: entry.blocked === true,
      remainingSeconds:
        typeof entry.remaining_seconds === "number" ? entry.remaining_seconds : null,
      usedSeconds: typeof entry.used_seconds === "number" ? entry.used_seconds : null,
      limitSeconds: typeof entry.limit_seconds === "number" ? entry.limit_seconds : null,
      schedule: entry.schedule ?? null,
      reason: typeof entry.reason === "string" ? entry.reason : "UNBLOCKED",
    });
  }
  return rules;
}

export function isBrowserName(value) {
  return value === "chrome" || value === "edge" || value === "firefox";
}

/** Detect which Chromium-family browser we are running in. */
export function detectBrowserName() {
  const ua = (globalThis.navigator && globalThis.navigator.userAgent) || "";
  if (ua.includes("Edg/")) return "edge";
  return "chrome";
}
