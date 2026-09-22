// Domain normalization — MUST mirror app/core/rules/matcher.py.
//
// Rule "youtube.com" matches "youtube.com" and "*.youtube.com" only, never
// "fakeyoutube.com" nor "youtube.com.evil.com". Invalid input returns "" so
// callers can treat it as "not a trackable web page".
//
// tests/test_extension_assets.py compares this file's output against Python's
// normalize_domain() on a shared fixture list (via node), so the two
// implementations cannot drift apart silently.

const IPV4 = /^\d+\.\d+\.\d+\.\d+$/;
const LABEL = /^(?!-)[a-z0-9-]{1,63}(?<!-)$/;

/** Lowercase, strip scheme/path/port/userinfo, punycode-encode, validate. */
export function normalizeDomain(input) {
  let host = String(input || "").trim().toLowerCase();
  if (host.includes("://")) host = host.split("://")[1];
  host = host.split("/")[0].split("?")[0].split("#")[0];
  host = host.split("@").pop();
  if (host.startsWith("[")) return ""; // IPv6 literal: never tracked
  if (host.includes(":")) host = host.split(":")[0]; // strip port
  host = host.replace(/^\.+|\.+$/g, ""); // trim leading/trailing dots
  if (!host || host.length > 253) return "";

  // IDNA: the URL parser turns "münchen.de" into "xn--mnchen-3ya.de" exactly
  // like Python's str.encode("idna") does.
  if (!/^[\x00-\x7F]*$/.test(host)) {
    try {
      host = new URL("http://" + host).hostname.toLowerCase();
    } catch {
      return "";
    }
  }
  if (host === "localhost") return host; // allowed, mirroring the agent
  if (IPV4.test(host)) return ""; // IPv4 literals are never tracked

  const labels = host.split(".");
  if (labels.length < 2) return "";
  if (!labels.every((label) => LABEL.test(label))) return "";
  return host;
}

/** True if `observedHost` is `ruleDomain` or a subdomain of it. */
export function domainMatches(ruleDomain, observedHost) {
  const rule = normalizeDomain(ruleDomain);
  const host = normalizeDomain(observedHost);
  if (!rule || !host) return false;
  return host === rule || host.endsWith("." + rule);
}

/** Extract the trackable domain from a full URL ("" for non-http(s) pages). */
export function domainFromUrl(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "";
    return normalizeDomain(parsed.hostname);
  } catch {
    return "";
  }
}
