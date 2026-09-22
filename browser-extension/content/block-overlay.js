// Content script: blocks a page locally when the agent says so.
//
// Runs at document_start (before the page's own scripts) on every http(s)
// page. If the domain is blocked we (a) stop the rest of the load with
// window.stop() and (b) replace the view with a block screen rendered inside a
// closed Shadow DOM, so site CSS/JS cannot restyle or remove it.
//
// Nothing here talks to the network: the decision comes from the service
// worker, which got it from the local agent.

(() => {
  const HOST_ID = "time-manager-block";
  let current = null; // rendered decision
  let shadowRoot = null;

  const STYLE = `
    :host { all: initial; }
    .wrap {
      position: fixed; inset: 0; z-index: 2147483647;
      display: flex; align-items: center; justify-content: center;
      background: #0f1220; color: #f4f6fb;
      font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      padding: 24px; text-align: center;
    }
    .card { max-width: 520px; }
    .badge {
      display: inline-flex; align-items: center; gap: 8px;
      background: #1c2140; border: 1px solid #313a6b; color: #aab4ff;
      border-radius: 999px; padding: 6px 14px; font-size: 13px;
      letter-spacing: .02em; margin-bottom: 20px;
    }
    h1 { font-size: 26px; margin: 0 0 12px; font-weight: 650; }
    p { margin: 0 0 8px; color: #b9c0d4; }
    .domain { color: #f4f6fb; font-weight: 600; word-break: break-all; }
    .countdown { margin-top: 18px; color: #8f9ac0; font-variant-numeric: tabular-nums; }
    button {
      margin-top: 22px; background: #4b5bd6; color: #fff; border: 0;
      border-radius: 10px; padding: 10px 18px; font-size: 15px; cursor: pointer;
    }
    button:hover { background: #5a6ae8; }
  `;

  function domainOf() {
    try {
      if (location.protocol !== "http:" && location.protocol !== "https:") return "";
      return location.hostname.toLowerCase().replace(/^www\./, "");
    } catch {
      return "";
    }
  }

  function ensureHost() {
    let host = document.getElementById(HOST_ID);
    if (host && shadowRoot) return host;
    host = document.createElement("div");
    host.id = HOST_ID;
    shadowRoot = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    shadowRoot.appendChild(style);
    (document.documentElement || document).appendChild(host);
    return host;
  }

  function render(decision) {
    const host = ensureHost();
    const reason = decision.reason || "DAILY_LIMIT_REACHED";
    const heading = {
      DAILY_LIMIT_REACHED: "That's your time for today",
      SESSION_LIMIT_REACHED: "Take a break",
      SCHEDULE_BLOCKED: "Not available right now",
      MANUAL: "Paused by Time Manager",
    }[reason] || "Blocked";

    const wrap = document.createElement("div");
    wrap.className = "wrap";
    const card = document.createElement("div");
    card.className = "card";

    const badge = document.createElement("div");
    badge.className = "badge";
    badge.textContent = "Time Manager";

    const title = document.createElement("h1");
    title.textContent = heading;

    const body = document.createElement("p");
    body.className = "domain";
    body.textContent = decision.domain || domainOf();

    const explain = document.createElement("p");
    explain.textContent = decision.message || "This site is limited to keep your day on track.";

    card.append(badge, title, body, explain);

    const countdown = document.createElement("div");
    countdown.className = "countdown";
    card.appendChild(countdown);

    const button = document.createElement("button");
    button.textContent = "Go back";
    button.addEventListener("click", () => {
      if (history.length > 1) history.back();
      else window.close();
    });
    card.appendChild(button);

    wrap.appendChild(card);
    // Replace previous card if we re-render (e.g. reason changed).
    for (const child of Array.from(shadowRoot.querySelectorAll(".wrap"))) child.remove();
    shadowRoot.appendChild(wrap);
    tickCountdown(countdown, decision.resetAt);
  }

  function tickCountdown(node, resetAt) {
    if (!resetAt) {
      node.textContent = "Your limits reset at midnight.";
      return;
    }
    const target = new Date(resetAt).getTime();
    if (!Number.isFinite(target)) {
      node.textContent = "Your limits reset at midnight.";
      return;
    }
    const update = () => {
      const left = target - Date.now();
      if (left <= 0) {
        node.textContent = "Limits have reset — reload to continue.";
        return;
      }
      const hours = Math.floor(left / 3600000);
      const minutes = Math.floor((left % 3600000) / 60000);
      const seconds = Math.floor((left % 60000) / 1000);
      node.textContent = `Available again in ${hours}h ${String(minutes).padStart(2, "0")}m ` +
        `${String(seconds).padStart(2, "0")}s`;
    };
    update();
    const timer = setInterval(() => {
      if (!node.isConnected) {
        clearInterval(timer);
        return;
      }
      update();
    }, 1000);
  }

  function apply(decision) {
    if (!decision) return;
    const blocked = decision.blocked === true;
    const alreadyBlocked = Boolean(current && current.blocked);
    current = decision;
    if (blocked) {
      if (!alreadyBlocked) {
        // Stop the page from loading anything else (images, scripts, media).
        try {
          window.stop();
        } catch {
          /* not fatal */
        }
        // Freeze background media: a blocked tab must not keep playing audio.
        for (const media of Array.from(document.querySelectorAll("audio, video"))) {
          try {
            media.pause();
            media.removeAttribute("src");
          } catch {
            /* ignore */
          }
        }
      }
      render(decision);
      return;
    }
    const host = document.getElementById(HOST_ID);
    if (host) host.remove();
    shadowRoot = null;
  }

  function askAgent(reason) {
    const domain = domainOf();
    if (!domain) return;
    try {
      chrome.runtime.sendMessage({ type: "GET_DECISION", domain }, (decision) => {
        if (chrome.runtime.lastError) return; // agent/worker gone: do nothing
        if (decision && decision.blocked) apply(decision);
        else if (decision && current && current.blocked) apply(decision);
      });
    } catch {
      /* extension context invalidated (reload): ignore */
    }
    void reason;
  }

  // Pushed updates from the service worker (limit hit while you are browsing).
  try {
    chrome.runtime.onMessage.addListener((message) => {
      if (!message || message.type !== "DECISION") return;
      apply(message.decision);
    });
  } catch {
    /* ignore */
  }

  askAgent("load");
  // SPA navigations do not reload the document; re-check after history changes.
  window.addEventListener("popstate", () => askAgent("popstate"));
  window.addEventListener("hashchange", () => askAgent("hashchange"));

  // Late-arriving decisions (the service worker may still be connecting).
  setTimeout(() => askAgent("late"), 1500);
  setTimeout(() => {
    if (!current) askAgent("late2");
  }, 5000);
})();
