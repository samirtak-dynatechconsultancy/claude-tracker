// Content script (isolated world). Job: inject page-hook.js into the
// page's main world so it can monkey-patch fetch/XHR, then receive token
// events back via window.postMessage and forward them to the background
// worker.
//
// We keep this script tiny — all the real logic (and all dependencies on
// claude.ai's API shape) lives in page-hook.js so it can be updated
// without rewiring the Chrome messaging.

(function injectPageHook() {
  try {
    const s = document.createElement("script");
    s.src = chrome.runtime.getURL("page-hook.js");
    s.onload = () => s.remove();
    (document.head || document.documentElement).appendChild(s);
  } catch (e) {
    console.warn("[ClaudeTracker] failed to inject page hook:", e);
  }
})();

// Page-hook.js posts messages of the form:
//   { source: "claude-tracker-hook", payload: { conversation_id, tokens_in, tokens_out, model, timestamp, source, message_id } }
window.addEventListener("message", (ev) => {
  if (ev.source !== window) return;
  const data = ev.data;
  if (!data || data.source !== "claude-tracker-hook" || !data.payload) return;

  try {
    chrome.runtime.sendMessage(
      { type: "LOG_EVENT", payload: data.payload },
      (resp) => {
        if (!resp?.ok) {
          console.debug("[ClaudeTracker] log rejected:", resp);
        }
      }
    );
  } catch (e) {
    // Extension context can be invalidated on reload; swallow.
  }
});
