// Blocked page: show status, offer to launch the exe via the custom
// protocol handler, and auto-retry the ping so the page unblocks itself
// the moment the tracker comes up.

const params = new URLSearchParams(location.search);
const initialStatus = params.get("status") || "down";
const dest = params.get("dest") || "https://claude.ai/";

const statusEl = document.getElementById("status");
const destEl = document.getElementById("dest");
const hintDown = document.getElementById("hint-down");
const hintPaused = document.getElementById("hint-paused");
const autoNote = document.getElementById("auto-retry-note");

destEl.textContent = dest;
applyStatus(initialStatus);

document.getElementById("btn-install").href =
  "https://github.com/samirtak-dynatechconsultancy/claude-tracker/releases/latest";

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
