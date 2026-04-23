// Runs in the PAGE's main world (not the content script's isolated
// world) so it can wrap window.fetch before claude.ai's own scripts
// capture the original reference.
//
// Strategy
// --------
// claude.ai posts completions to an endpoint shaped like:
//   /api/organizations/{org}/chat_conversations/{conv}/completion
// The response is SSE with Anthropic-API-style events:
//   message_start, content_block_delta, message_delta, message_stop
// The final `message_delta` event carries cumulative `usage` for the
// response; `message_start` includes model + input_tokens.
//
// We clone each matching streaming Response, parse the SSE, and emit
// one tracker event per completion. If claude.ai changes the endpoint
// shape, this module fails closed (no events) — the rest of the
// extension keeps working; only the ai_web source stops reporting.

(function () {
  "use strict";
  if (window.__claudeTrackerHookInstalled) return;
  window.__claudeTrackerHookInstalled = true;

  function estimateTokens(text) {
    if (!text) return 0;
    const len = text.length;
    const ascii = (text.match(/[\x00-\x7F]/g) || []).length;
    const words = (text.match(/\s+/g) || []).length;
    return Math.ceil(ascii / 3.8 + (len - ascii) / 1.5 + words * 0.1);
  }

  const origFetch = window.fetch.bind(window);

  function post(payload) {
    try {
      window.postMessage(
        { source: "claude-tracker-hook", payload },
        "*"
      );
    } catch (_) {}
  }

  function extractConvId(url) {
    const m = url.match(/chat_conversations\/([0-9a-f-]+)/i);
    return m ? m[1] : null;
  }

  function isCompletionUrl(url) {
    return /\/api\/organizations\/[^/]+\/chat_conversations\/[^/]+\/completion/.test(url);
  }

  async function consumeSSE(readable, ctx) {
    // ctx: { conversation_id, prompt_tokens_est, model_hint }
    // claude.ai's SSE emits `message_start.message.model` as an empty string,
    // so we seed `model` from the POST request body (which carries the real
    // value) and let `message_start` override it if it ever starts populating.
    let model = ctx.model_hint || "unknown";
    let input_tokens = 0;
    let cache_creation = 0;
    let cache_read = 0;
    let output_tokens = 0;
    let message_id = null;
    let outputText = "";


    const reader = readable.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      // SSE frames are separated by blank lines. Each frame is a set of
      // "field: value" lines. We care about "data:" whose value is JSON.
      let m;
      while ((m = buf.match(/\r?\n\r?\n/))) {
        const idx = m.index;
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + m[0].length);
        const dataLines = [];
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        if (!dataLines.length) continue;
        const raw = dataLines.join("\n");
        if (raw === "[DONE]") continue;
        let j;
        try { j = JSON.parse(raw); } catch (_) { continue; }

        // Anthropic-style event envelope: { type: "...", ...fields }.
        const t = j.type || "";
        if (t === "message_start" && j.message) {
          model = j.message.model || model;
          message_id = j.message.id || message_id;
          const u = j.message.usage || {};
          input_tokens = u.input_tokens ?? input_tokens;
          cache_creation = u.cache_creation_input_tokens ?? cache_creation;
          cache_read = u.cache_read_input_tokens ?? cache_read;
          output_tokens = u.output_tokens ?? output_tokens;
        } else if (t === "message_delta" && j.usage) {
          // `message_delta.usage.output_tokens` is the cumulative count.
          if (typeof j.usage.output_tokens === "number") {
            output_tokens = j.usage.output_tokens;
          }
          if (typeof j.usage.input_tokens === "number") {
            input_tokens = j.usage.input_tokens;
          }
        } else if (t === "content_block_delta" && j.delta?.text) {
          outputText += j.delta.text;
        }

      }
    }

    // claude.ai's SSE doesn't emit `usage` fields, so we estimate output
    // tokens from the accumulated delta text. See estimateTokens().
    if (outputText.length > 0) {
      const out_est = estimateTokens(outputText);
      post({
        conversation_id: ctx.conversation_id || "",
        message_id,
        tokens_in: ctx.prompt_tokens_est || 0,
        tokens_out: out_est,
        model,
        timestamp: Date.now() / 1000,
        source: "ai_web",
      });
    }

  }

  window.fetch = async function (input, init) {
    const url =
      typeof input === "string" ? input : (input && input.url) || "";
    let prompt_tokens_est = 0;
    let model_hint = "";
    try {
      if (init?.body && typeof init.body === "string") {
        const b = JSON.parse(init.body);
        const s = b.prompt || b.message || JSON.stringify(b).slice(0, 200000);
        prompt_tokens_est = estimateTokens(s);
        // claude.ai puts the selected model on the request body
        // (e.g. "claude-haiku-4-5-20251001"); SSE `message_start.model` is
        // empty, so this is our primary source of truth.
        model_hint =
          b.model ||
          b.model_slug ||
          b.create_conversation_params?.model ||
          "";
      }
    } catch (_) {}

    const resp = await origFetch(input, init);

    try {
      if (isCompletionUrl(url) && resp.ok && resp.body) {
        const conversation_id = extractConvId(url);
        const [a, b] = resp.body.tee();
        // Consume `a` in the background; return a new Response built from `b`
        // so the page gets a fully-intact stream.
        consumeSSE(a, { conversation_id, prompt_tokens_est, model_hint }).catch((e) =>
          console.debug("[ClaudeTracker] SSE consume failed:", e)
        );
        return new Response(b, {
          status: resp.status,
          statusText: resp.statusText,
          headers: resp.headers,
        });
      }
    } catch (e) {
      console.debug("[ClaudeTracker] fetch hook failed:", e);
    }

    return resp;
  };
})();
