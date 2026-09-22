// Options page: paste the agent token, choose the port, test the link.

import { getSettings, saveSettings, tokenLooksValid } from "../shared/settings.js";

const elements = {
  token: document.getElementById("token"),
  host: document.getElementById("host"),
  port: document.getElementById("port"),
  enabled: document.getElementById("enabled"),
  message: document.getElementById("message"),
  status: document.getElementById("status"),
  agent: document.getElementById("agent"),
  browserId: document.getElementById("browser-id"),
  messages: document.getElementById("messages"),
};

function setMessage(text, kind = "") {
  elements.message.textContent = text;
  elements.message.className = "message" + (kind ? " " + kind : "");
}

function renderStatus(status) {
  elements.status.textContent = {
    connected: "connected",
    connecting: "connecting…",
    disconnected: "not reachable — is the agent running?",
    disabled: "turned off",
    error: status.lastError || "error",
  }[status.status] || status.status;
  elements.agent.textContent = status.agentVersion || "—";
  elements.browserId.textContent = status.browserId || "—";
  elements.messages.textContent =
    `${status.sentCount ?? 0} sent / ${status.receivedCount ?? 0} received`;
}

async function load() {
  const settings = await getSettings();
  elements.token.value = settings.token;
  elements.host.value = settings.host;
  elements.port.value = settings.port;
  elements.enabled.checked = settings.enabled;
  elements.browserId.textContent = settings.browserId;
  refreshStatus();
}

function refreshStatus() {
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, (status) => {
    if (chrome.runtime.lastError || !status) {
      elements.status.textContent = "service worker unavailable";
      return;
    }
    renderStatus(status);
  });
}

document.getElementById("save").addEventListener("click", async () => {
  const token = elements.token.value.trim();
  const port = Number(elements.port.value);
  if (token && !tokenLooksValid(token)) {
    setMessage("That does not look like a 64-character hex token.", "bad");
    return;
  }
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    setMessage("Port must be between 1024 and 65535.", "bad");
    return;
  }
  await saveSettings({
    token,
    host: elements.host.value.trim() || "127.0.0.1",
    port,
    enabled: elements.enabled.checked,
  });
  setMessage("Saved — connecting…");
  chrome.runtime.sendMessage({ type: "RECONNECT" });
  setTimeout(() => {
    chrome.runtime.sendMessage({ type: "GET_STATUS" }, (status) => {
      renderStatus(status || { status: "error" });
      if (status && status.status === "connected") {
        setMessage(`Connected to agent v${status.agentVersion}.`, "ok");
      } else if (status && status.status === "error") {
        setMessage(status.lastError || "Could not connect.", "bad");
      } else {
        setMessage("Still connecting — check that the agent is running.", "bad");
      }
    });
  }, 1200);
});

document.getElementById("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "RECONNECT" });
  setMessage("Reconnecting…");
  setTimeout(refreshStatus, 800);
});

elements.enabled.addEventListener("change", async () => {
  await saveSettings({ enabled: elements.enabled.checked });
  setMessage(elements.enabled.checked ? "Reporting enabled." : "Reporting disabled.");
});

load();
setInterval(refreshStatus, 3000);
