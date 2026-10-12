// AiSquared Travel - Naver Flights capture (init script, see naver.py / DESIGN §13.1).
//
// Added with BrowserContext.add_init_script before flight.naver.com loads. It wraps
// window.fetch (the page's SSE client resolves window.fetch at call time) and, for
// POST .../flight/international/searchFlights only, reads a *clone* of the
// text/event-stream response in the page. The page keeps reading its own copy untouched.
//
// Messages sent to Python through the binding window.__aisqNaverSse (expose_binding):
//   {kind: "final", data: "<JSON text>"}   the snapshot with status.isCompleted === true and
//                                          isExpired !== true (the only result data forwarded)
//   {kind: "error", reason: "http", status, contentType}   non-2xx or non-event-stream reply
//   {kind: "error", reason: "network" | "stream", message} fetch rejected / stream broke
//   {kind: "error", reason: "incomplete", status, completed, requested, expired}
//                                          the stream ended without a usable final snapshot
// Intermediate snapshots (cumulative, up to ~3 MB each) are never forwarded. A completed
// snapshot marked isExpired is stale: Naver's own client alerts and searches again, so it is
// skipped and the re-search's stream supplies the final snapshot.
// Python accepts these messages from the top frame only (the binding exists in every frame).
(() => {
  "use strict";
  const BINDING = "__aisqNaverSse";
  const API_MARKER = "/flight/international/searchFlights";
  const COMPLETED_RE = /"isCompleted"\s*:\s*true/;
  const BOUNDARY_RE = /\r?\n\r?\n/g;

  if (window.top !== window || window.__aisqNaverCapture) return;
  Object.defineProperty(window, "__aisqNaverCapture", { value: true });
  const nativeFetch = window.fetch;
  if (typeof nativeFetch !== "function") return;

  function send(message) {
    try {
      const binding = window[BINDING];
      if (typeof binding === "function") {
        Promise.resolve(binding(message)).catch(() => {});
      }
    } catch (_e) {
      // never break the page
    }
  }

  function requestUrl(input) {
    if (typeof input === "string") return input;
    if (input && typeof input.href === "string") return input.href; // URL
    if (input && typeof input.url === "string") return input.url; // Request
    return "";
  }

  function errorText(error) {
    return String((error && error.message) || error || "unknown error").slice(0, 200);
  }

  function isAbort(error) {
    return !!error && error.name === "AbortError";
  }

  function eventData(block) {
    const lines = block.split(/\r?\n/);
    const parts = [];
    for (const line of lines) {
      if (line.startsWith("data:")) parts.push(line.slice(5).replace(/^ /, ""));
    }
    return parts.join("\n");
  }

  function counter(data, name) {
    const match = new RegExp('"' + name + '"\\s*:\\s*(\\d+)').exec(data.slice(0, 4096));
    return match ? Number(match[1]) : null;
  }

  // "final" (complete and fresh), "expired" (complete but stale) or "" (anything else).
  function snapshotKind(data) {
    if (!COMPLETED_RE.test(data)) return ""; // cheap test before parsing megabytes
    try {
      const parsed = JSON.parse(data);
      if (!(parsed && parsed.status && parsed.status.isCompleted === true)) return "";
      return parsed.isExpired === true ? "expired" : "final";
    } catch (_e) {
      return "";
    }
  }

  async function pump(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let scanFrom = 0;
    let last = "";
    let expired = false;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      for (;;) {
        BOUNDARY_RE.lastIndex = scanFrom;
        const boundary = BOUNDARY_RE.exec(buffer);
        if (!boundary) {
          scanFrom = Math.max(0, buffer.length - 3);
          break;
        }
        const data = eventData(buffer.slice(0, boundary.index));
        buffer = buffer.slice(boundary.index + boundary[0].length);
        scanFrom = 0;
        if (!data) continue;
        const kind = snapshotKind(data);
        if (kind === "final") {
          send({ kind: "final", data: data });
          reader.cancel().catch(() => {});
          return;
        }
        expired = expired || kind === "expired";
        last = data;
      }
    }
    const tail = eventData(buffer + decoder.decode());
    const tailKind = tail ? snapshotKind(tail) : "";
    if (tailKind === "final") {
      send({ kind: "final", data: tail });
      return;
    }
    if (tail) last = tail;
    expired = expired || tailKind === "expired";
    send({
      kind: "error",
      reason: "incomplete",
      status: response.status,
      completed: counter(last, "completedPartnerCount"),
      requested: counter(last, "requestedPartnerCount"),
      expired: expired,
    });
  }

  function inspect(response) {
    const type = (response.headers.get("content-type") || "").toLowerCase();
    if (!response.ok || type.indexOf("text/event-stream") < 0) {
      send({
        kind: "error",
        reason: "http",
        status: response.status,
        contentType: type.slice(0, 100),
      });
      return;
    }
    let copy;
    try {
      copy = response.clone();
    } catch (error) {
      send({ kind: "error", reason: "stream", message: errorText(error) });
      return;
    }
    if (!copy.body) {
      send({ kind: "error", reason: "stream", message: "response has no body" });
      return;
    }
    pump(copy).catch((error) => {
      if (!isAbort(error)) send({ kind: "error", reason: "stream", message: errorText(error) });
    });
  }

  window.fetch = function (input) {
    const promise = nativeFetch.apply(window, arguments);
    if (requestUrl(input).indexOf(API_MARKER) < 0) return promise;
    return promise.then(
      (response) => {
        try {
          inspect(response);
        } catch (error) {
          send({ kind: "error", reason: "stream", message: errorText(error) });
        }
        return response;
      },
      (error) => {
        if (!isAbort(error)) send({ kind: "error", reason: "network", message: errorText(error) });
        throw error;
      }
    );
  };
})();
