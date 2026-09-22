// Persisted extension settings + the stable browser identity.
//
// Everything lives in chrome.storage.local: no sync, no cloud, and the token
// never leaves the machine it was pasted on.

import { DEFAULT_HOST, DEFAULT_PORT } from "./protocol.js";

const KEYS = {
  token: "agentToken",
  port: "agentPort",
  host: "agentHost",
  browserId: "browserId",
  lastConnectedAt: "lastConnectedAt",
  enabled: "enabled",
};

/** Cryptographically random UUID (persisted once per profile). */
export function newBrowserId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  (globalThis.crypto || {}).getRandomValues?.(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export async function getSettings() {
  const stored = await chrome.storage.local.get(Object.values(KEYS));
  let browserId = stored[KEYS.browserId];
  if (!browserId) {
    browserId = newBrowserId();
    await chrome.storage.local.set({ [KEYS.browserId]: browserId });
  }
  return {
    token: stored[KEYS.token] || "",
    host: stored[KEYS.host] || DEFAULT_HOST,
    port: Number(stored[KEYS.port] || DEFAULT_PORT),
    browserId,
    enabled: stored[KEYS.enabled] !== false,
    lastConnectedAt: stored[KEYS.lastConnectedAt] || null,
  };
}

export async function saveSettings({ token, host, port, enabled }) {
  const patch = {};
  if (typeof token === "string") patch[KEYS.token] = token.trim();
  if (typeof host === "string" && host.trim()) patch[KEYS.host] = host.trim();
  if (port !== undefined && port !== null) patch[KEYS.port] = Number(port);
  if (typeof enabled === "boolean") patch[KEYS.enabled] = enabled;
  await chrome.storage.local.set(patch);
  return getSettings();
}

export async function markConnected() {
  await chrome.storage.local.set({ [KEYS.lastConnectedAt]: Date.now() });
}

export function socketUrl({ host, port }) {
  return `ws://${host}:${port}/`;
}

export function tokenLooksValid(token) {
  return typeof token === "string" && /^[0-9a-fA-F]{64}$/.test(token.trim());
}
