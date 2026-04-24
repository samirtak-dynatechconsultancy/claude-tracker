// Blocked page: show status, offer to launch the exe via the custom
// protocol handler, and auto-retry the ping so the page unblocks itself
// the moment the tracker comes up. If the protocol click doesn't bring
// the tracker online within a few seconds, assume it isn't installed and
// surface a direct download link.

const REPO = "samirtak-dynatechconsultancy/claude-tracker";
// `releases/latest/download/<asset>` always resolves to the asset on the
// latest GitHub release, so we never have to bump this page when cutting
// a new version. The /releases/latest page works the same way for humans.
const LATEST_EXE_URL = `https://github.com/${REPO}/releases/latest/download/ClaudeTracker.exe`;
const LATEST_PAGE_URL = `https://github.com/${REPO}/releases/latest`;
// How long to wait after an "Open Tracker" click before concluding the
// protocol handler isn't registered. Chrome shows its consent prompt
// essentially instantly; if the tracker were going to boot, /ping would
// succeed within a couple seconds of the user accepting.
const PROTOCOL_TIMEOUT_MS = 5000;

const params = new URLSearchParams(location.search);
const initialStatus = params.get("status") || "down";
const dest = params.get("dest") || "https://claude.ai/";

const statusEl = document.getElementById("status");
const destEl = document.getElementById("dest");
const hintDown = document.getElementById("hint-down");
const hintPaused = document.getElementById("hint-paused");
const autoNote = document.getElementById("auto-retry-note");
const dlPanel = document.getElementById("download-panel");
const dlBtn = document.getElementById("btn-download");
const openBtn = document.getElementById("btn-open");
const installBtn = document.getElementById("btn-install");

destEl.textContent = dest;
applyStatus(initialStatus);

installBtn.href = LATEST_PAGE_URL;
dlBtn.href = LATEST_EXE_URL;

// When the user clicks "Open Tracker", start a watchdog. If /ping is
// still failing after PROTOCOL_TIMEOUT_MS, surface the download panel —
// there's no browser API to tell us whether a custom protocol resolved,
// so absence of a successful ping is the best signal we get.
let protocolWatchdog = null;
openBtn.addEventListener("click", () => {
  if (protocolWatchdog) clearTimeout(protocolWatchdog);
  protocolWatchdog = setTimeout(() => {
    dlPanel.style.display = "";
  }, PROTOCOL_TIMEOUT_MS);
});

document.getElementById("btn-retry").addEventListener("click", () => pingOnce(true));

// Auto-retry loop: one GET_STATUS every 2s. If the tracker is up and not
// paused, jump back to the originally-requested page. "paused" is a
// deliberate user action, so we keep showing the paused hint instead of
// spinning forever.
let timer = setInterval(() => pingOnce(false), 2000);

async function pingOnce(userClicked) {
  const resp = await new Promise((r) =>
    chrome.runtime.sendMessage({ type: "GET_STATUS" }, r)
  );
  const status = resp?.status || "down";
  if (status === "ok") {
    clearInterval(timer);
    if (protocolWatchdog) clearTimeout(protocolWatchdog);
    location.href = dest;
    return;
  }
  applyStatus(status);
  if (userClicked && autoNote) {
    autoNote.textContent = "Still offline — auto-checking every 2s…";
  }
}

function applyStatus(status) {
  statusEl.textContent = status;
  statusEl.className = "status " + status;
  if (status === "paused") {
    hintDown.style.display = "none";
    hintPaused.style.display = "";
    if (autoNote) autoNote.style.display = "none";
  } else {
    hintDown.style.display = "";
    hintPaused.style.display = "none";
    if (autoNote) autoNote.style.display = "";
  }
}
