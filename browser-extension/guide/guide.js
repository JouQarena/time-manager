// Guide page behaviour: show the live link state and provide the two actions
// a reader needs while following the steps. Nothing here is required for the
// guide to be readable — with no service worker the status pill simply stays
// at "not reachable", and the instructions still apply.

const state = {
  pill: document.getElementById("link-state"),
  text: document.getElementById("link-text"),
};

function render(status) {
  if (!status || !status.status) {
    describe("bad", "Not reachable — is the desktop app running?");
    return;
  }
  switch (status.status) {
    case "connected":
      describe("ok", `Connected to the agent (v${status.agentVersion || "?"}) on port ${status.port}`);
      break;
    case "connecting":
      describe("", "Connecting to the desktop app…");
      break;
    case "disabled":
      describe("bad", "Reporting is turned off in settings");
      break;
    case "error":
      describe("bad", status.lastError || "Error talking to the desktop app");
      break;
    default:
      describe("bad", "Not reachable — is the desktop app running?");
  }
}

function describe(kind, message) {
  state.pill.className = "pill" + (kind ? " " + kind : "");
  state.text.textContent = message;
}

function ask() {
  // Inside the extension `chrome.runtime` always exists. The guard keeps the
  // page honest if it is ever opened outside that context (a saved copy, a
  // preview): it says "not reachable" instead of spinning forever.
  if (!globalThis.chrome || !chrome.runtime || !chrome.runtime.sendMessage) {
    render(null);
    return;
  }
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, (status) => {
    if (chrome.runtime.lastError) {
      render(null);
      return;
    }
    render(status);
  });
}

function openOptions() {
  if (globalThis.chrome && chrome.runtime && chrome.runtime.openOptionsPage) {
    chrome.runtime.openOptionsPage();
    return;
  }
  window.location.href = "../options/options.html";
}

for (const id of ["open-options", "open-options-2"]) {
  const element = document.getElementById(id);
  if (element) element.addEventListener("click", openOptions);
}

document.getElementById("reconnect-link").addEventListener("click", (event) => {
  event.preventDefault();
  if (!globalThis.chrome || !chrome.runtime || !chrome.runtime.sendMessage) {
    describe("bad", "Not reachable — is the desktop app running?");
    return;
  }
  describe("", "Reconnecting…");
  chrome.runtime.sendMessage({ type: "RECONNECT" }, () => setTimeout(ask, 800));
});

ask();
setInterval(ask, 3000);
