// Service worker. Jobs:
//   - Pre-navigation check: ping 127.0.0.1:47821 before claude.ai loads.
//     If the tracker is down/paused, redirect to blocked.html.
//   - First-run handshake to retrieve the shared secret.
//   - Forward token events from content scripts to POST /log.
//
// Storage:
//   chrome.storage.local
//     sharedSecret:   string | null
//     user:           string | null
//     lastStatus:     "ok" | "paused" | "down" | null
//     lastStatusAt:   number (epoch ms)

const TRACKER_BASE = "http://127.0.0.1:47821";
const PING_TIMEOUT_MS = 2500;

// Baked-in shared secret. Seeded into chrome.storage.local on install/startup
// so events can be signed even if the tracker app isn't running. The user
// attribution is NOT baked — it's picked up dynamically from /ping and
// /handshake, which return the OS username (so attribution matches the
// `code` and `desktop` sources). Until the tracker runs at least once,
// `user` stays null and events fall back to whatever the tracker writes
// when it eventually handshakes.
const BAKED_SHARED_SECRET = "69ZMYi_csvAq3nirVUrOfykrINLQw_ZDQFgrhf-vmCU";

async function seedCredentials() {
  await storageSet({ sharedSecret: BAKED_SHARED_SECRET });
}

async function storageGet(keys) {
  return new Promise((res) => chrome.storage.local.get(keys, res));
}
async function storageSet(obj) {
  return new Promise((res) => chrome.storage.local.set(obj, res));
}

async function fetchWithTimeout(url, opts = {}, ms = PING_TIMEOUT_MS) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), ms);
  try {
    return await fetch(url, { ...opts, signal: ac.signal });
  } finally {
    clearTimeout(t);
  }
}

async function doHandshake() {
  try {
    const r = await fetchWithTimeout(`${TRACKER_BASE}/handshake`);
    if (!r.ok) return false;
    const j = await r.json();
    if (j.shared_secret) {
      await storageSet({ sharedSecret: j.shared_secret, user: j.user || null });
      return true;
    }
  } catch (_) {}
  return false;
}

// Returns "ok" | "paused" | "down".
async function pingTracker() {
  try {
    const r = await fetchWithTimeout(`${TRACKER_BASE}/ping`);
    if (!r.ok) return "down";
    const j = await r.json();
    const status = j.status === "paused" ? "paused" : "ok";
    if (j.user) await storageSet({ user: j.user });
    await storageSet({ lastStatus: status, lastStatusAt: Date.now() });
    return status;
  } catch (_) {
    await storageSet({ lastStatus: "down", lastStatusAt: Date.now() });
    return "down";
  }
}

// Before claude.ai loads in a top-level frame, verify tracker.
chrome.webNavigation.onBeforeNavigate.addListener(
  async (details) => {
    if (details.frameId !== 0) return;
    let url;
    try { url = new URL(details.url); } catch (_) { return; }
    if (url.hostname !== "claude.ai") return;

    const status = await pingTracker();

    if (status !== "ok") {
      // No secret? Try a fresh handshake (only works within 60s of app launch).
      const { sharedSecret } = await storageGet(["sharedSecret"]);
      if (!sharedSecret && status === "down") {
        // fall through to block page — user needs to launch the app
      }
      const blocked = chrome.runtime.getURL(
        `blocked.html?status=${status}&dest=${encodeURIComponent(details.url)}`
      );
      chrome.tabs.update(details.tabId, { url: blocked });
      return;
    }

    // If we don't have a secret yet, attempt a handshake now (tracker just
    // launched within the last minute, probably).
    const { sharedSecret } = await storageGet(["sharedSecret"]);
    if (!sharedSecret) await doHandshake();
  },
  { url: [{ hostEquals: "claude.ai" }] }
);

// Messages from content scripts:
//   { type: "LOG_EVENT", payload: { conversation_id, tokens_in, tokens_out, model, timestamp, source, message_id } }
//   { type: "GET_STATUS" } -> current tracker status for popup
//   { type: "REQUEST_HANDSHAKE" }
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg?.type === "LOG_EVENT") {
      const { sharedSecret } = await storageGet(["sharedSecret"]);
      if (!sharedSecret) {
        // Try one handshake before giving up.
        await doHandshake();
      }
      const { sharedSecret: secret } = await storageGet(["sharedSecret"]);
      if (!secret) {
        sendResponse({ ok: false, error: "no_secret" });
        return;
      }
      try {
        const r = await fetchWithTimeout(`${TRACKER_BASE}/log`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Tracker-Secret": secret,
          },
          body: JSON.stringify(msg.payload),
        });
        sendResponse({ ok: r.ok, status: r.status });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
      return;
    }

    if (msg?.type === "GET_STATUS") {
      const status = await pingTracker();
      const stored = await storageGet(["user", "sharedSecret"]);
      sendResponse({ status, user: stored.user || null, hasSecret: !!stored.sharedSecret });
      return;
    }

    if (msg?.type === "REQUEST_HANDSHAKE") {
      const ok = await doHandshake();
      sendResponse({ ok });
      return;
    }

    sendResponse({ ok: false, error: "unknown_message_type" });
  })();
  return true; // async sendResponse
});

// Seed baked credentials on install/startup, then try a handshake to refresh
// them if the tracker is up (no-op if the tracker isn't reachable).
chrome.runtime.onInstalled.addListener(async () => {
  await seedCredentials();
  doHandshake();
});
chrome.runtime.onStartup.addListener(async () => {
  await seedCredentials();
  doHandshake();
});
