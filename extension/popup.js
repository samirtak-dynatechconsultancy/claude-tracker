// Popup script. Shows tracker status + today/week token counts.
// We query the background worker for status, and the tracker's /stats
// directly for numbers (the secret is cached in chrome.storage.local).

const TRACKER_BASE = "http://127.0.0.1:47821";
const fmt = new Intl.NumberFormat();

const $ = (id) => document.getElementById(id);

async function storageGet(keys) {
  return new Promise((r) => chrome.storage.local.get(keys, r));
}

function setStatus(status) {
  const dot = $("status-dot");
  const txt = $("status-text");
  dot.className = "status " + (status || "unknown");
  txt.textContent =
    status === "ok" ? "running" :
    status === "paused" ? "paused" :
    status === "down" ? "not running" : "unknown";
}

async function fetchStats(secret) {
  const r = await fetch(`${TRACKER_BASE}/stats`, {
    headers: { "X-Tracker-Secret": secret },
  });
  if (!r.ok) throw new Error("stats " + r.status);
  return r.json();
}

function showError(msg) { $("error").textContent = msg || ""; }

async function refresh() {
  showError("");
  setStatus("unknown");
  try {
    const resp = await new Promise((r) =>
      chrome.runtime.sendMessage({ type: "GET_STATUS" }, r)
    );
    setStatus(resp?.status || "down");
    $("user").textContent = resp?.user || "—";

    const { sharedSecret } = await storageGet(["sharedSecret"]);
    if (!sharedSecret) {
      showError("No shared secret yet. Restart the tracker app, then click Re-handshake.");
      return;
    }
    if (resp?.status !== "ok") return;

    const s = await fetchStats(sharedSecret);
    $("today-out").textContent = fmt.format(s.today?.output_tokens ?? 0);
    $("today-msgs").textContent = fmt.format(s.today?.messages ?? 0);
    $("week-out").textContent = fmt.format(s.week?.output_tokens ?? 0);
  } catch (e) {
    showError(String(e.message || e));
  }
}

$("btn-refresh").addEventListener("click", refresh);
$("btn-handshake").addEventListener("click", async () => {
  showError("");
  const resp = await new Promise((r) =>
    chrome.runtime.sendMessage({ type: "REQUEST_HANDSHAKE" }, r)
  );
  if (!resp?.ok) {
    showError("Handshake failed. Restart the tracker (handshake window is 60s after launch).");
  } else {
    refresh();
  }
});

refresh();
