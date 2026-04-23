// Blocked page: read ?status=&dest=, show the right hint, retry ping.

const params = new URLSearchParams(location.search);
const status = params.get("status") || "down";
const dest = params.get("dest") || "https://claude.ai/";

document.getElementById("status").textContent = status;
document.getElementById("status").className = "status " + status;
document.getElementById("dest").textContent = dest;

if (status === "paused") {
  document.getElementById("hint-down").style.display = "none";
  document.getElementById("hint-paused").style.display = "";
}

// Swap the install guide link to the project's install.md on disk if we
// know the path (we don't by default, so this stays as a placeholder).
document.getElementById("btn-install").href =
  "https://github.com/anthropics/claude-code"; // placeholder; replace with your install page

document.getElementById("btn-retry").addEventListener("click", async () => {
  // Ask the background worker to re-ping; if OK, navigate to the original URL.
  const resp = await new Promise((r) =>
    chrome.runtime.sendMessage({ type: "GET_STATUS" }, r)
  );
  if (resp?.status === "ok") {
    location.href = dest;
  } else {
    // Refresh this page's status text.
    document.getElementById("status").textContent = resp?.status || "down";
    document.getElementById("status").className = "status " + (resp?.status || "down");
    if ((resp?.status || "down") === "paused") {
      document.getElementById("hint-down").style.display = "none";
      document.getElementById("hint-paused").style.display = "";
    } else {
      document.getElementById("hint-down").style.display = "";
      document.getElementById("hint-paused").style.display = "none";
    }
  }
});
