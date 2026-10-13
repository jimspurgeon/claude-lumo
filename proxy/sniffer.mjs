// Sniffs Lumo error payloads out of chat responses (issue #43).
//
// Lumo reports some failures as HTTP 200 with a small non-SSE JSON error body
// (observed as constant-size "out=436B"/"out=432B" lines in proxy.log, e.g.
// {"error":{"message":"No connected db.","type":"no_db_connection","code":"400"}}),
// which the proxy used to pass through and log with no detail at all.
//
// upstreamChat() buffers non-SSE chat responses (with stream:true always
// requested by LiteLLM, a non-SSE chat response is always an error payload or a
// rare non-streamed completion) and inspects them before anything reaches the
// client. A no_db_connection error is retried once with a short backoff: it is
// transient at request level (the log shows single failures whose immediate
// successor with the same input size succeeded - see the analysis posted on
// issue #43). Only the error type and code are ever logged, never the message
// or any prompt content.

export const NO_DB_CONNECTION = "no_db_connection";
export const NO_DB_RETRY_MS = 1500;

// Parses a JSON body and returns error metadata, or null if it is not an error
// payload. Deliberately extracts only type/code - never message content.
export function errorMetaFrom(text) {
  let d;
  try { d = JSON.parse(text); } catch { return null; }
  const e = d?.error;
  if (!e || typeof e !== "object") return null;
  return { type: e.type ?? null, code: e.code != null ? String(e.code) : null, isNoDb: e.type === NO_DB_CONNECTION };
}

const isSSE = (r) => (r.headers.get("content-type") || "").includes("event-stream");

// Fetches the upstream chat response, buffering non-SSE bodies to detect error
// payloads. Returns { response, buffered, errMeta, retried }: buffered is the
// response text when it was consumed for inspection (null to stream normally),
// errMeta the parsed error metadata when the final response is an error.
export async function upstreamChat({ upstream, method, path, body, headers, log, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), retryDelayMs = NO_DB_RETRY_MS }) {
  if (!path.startsWith("/chat")) return { response: await upstream(path, method, body, headers), buffered: null, errMeta: null, retried: false };
  let retried = false;
  for (let attempt = 0; ; attempt++) {
    let res = await upstream(path, method, body, headers);
    // SSE responses (and bodiless ones) stream through untouched.
    if (isSSE(res) || !res.body) return { response: res, buffered: null, errMeta: null, retried };
    let text = "";
    try { text = await res.text(); } catch (e) {
      // Non-SSE response whose body could not be read (mid-buffer network failure):
      // forward the status with an empty body, and log that a buffer read failed.
      // errMeta stays null (empty text parses to no error payload).
      log(`${path} non-SSE response body read failed: ${e instanceof Error ? e.message : String(e)}`);
    }
    // Rebuild so the response body is readable by the caller (the original was consumed).
    res = new Response(text, { status: res.status, headers: res.headers });
    const errMeta = errorMetaFrom(text);
    if (res.status >= 400) {
      log(`${path} -> ${res.status}${errMeta ? ` type=${errMeta.type} code=${errMeta.code}` : ""}`);
      return { response: res, buffered: text, errMeta, retried };
    }
    if (!errMeta) return { response: res, buffered: text, errMeta: null, retried };
    if (!errMeta.isNoDb || attempt >= 1) {
      log(`${path} upstream error ${res.status} type=${errMeta.type} code=${errMeta.code}`);
      return { response: res, buffered: text, errMeta, retried };
    }
    log(`${path} upstream error ${res.status} type=${errMeta.type} code=${errMeta.code}; retrying once in ${retryDelayMs / 1000}s`);
    retried = true;
    await sleep(retryDelayMs);
  }
}
