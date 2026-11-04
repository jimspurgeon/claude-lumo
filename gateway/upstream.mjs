// Upstream request seam for the Lumo proxy (audit §3, task 5a).
// Extracted verbatim from proxy.mjs so it can be tested without listening on
// a port, hitting the network, or touching the Firefox credential database:
// every dependency (fetch, credentials, logging, sleep, timeouts) is injected
// through createUpstream().
import { readLumoCredentials } from "./creds.mjs";

// Waits for the first byte of the upstream response before anything is
// streamed to the client (default 240s, overridable via LUMO_FIRST_BYTE_MS).
export const DEFAULT_FIRST_BYTE_MS = 240000;

// Creates the upstream(path, method, body, headers) function the proxy uses:
// - injects the Lumo login (Bearer token + x-pm-uid) from credsRef.current,
//   lazily (re-)reading credentials via readCreds() when absent;
// - retries connection errors, 429 (honouring Retry-After, max 30s), 5xx and
//   no-first-byte timeouts (2 retries, backoff);
// - on a 401, re-reads credentials once and rebuilds the auth headers with
//   the fresh token;
// - never logs or errors token values (only whether the token changed).
export function createUpstream({
  fetchFn = fetch,
  credsRef = { current: null },
  readCreds = readLumoCredentials,
  log = () => {},
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
  firstByteMs = Number(process.env.LUMO_FIRST_BYTE_MS || DEFAULT_FIRST_BYTE_MS),
  upstreamBase = "https://lumo.proton.me/api/ai/v1",
} = {}) {
  return async function upstream(path, method, body, headers) {
    for (let attempt = 0, refreshed = false; ; attempt++) {
      if (!credsRef.current) credsRef.current = readCreds();
      const creds = credsRef.current;
      if (!creds) return new Response(JSON.stringify({ error: { message: "lumo-proxy: no Lumo login found in Firefox. Log in at lumo.proton.me in Firefox." } }), { status: 401, headers: { "content-type": "application/json" } });
      let res;
      try {
        const ac = new AbortController();
        const timer = setTimeout(() => ac.abort(new Error(`no response from Lumo within ${firstByteMs / 1000}s`)), firstByteMs);
        try {
          res = await fetchFn(upstreamBase + path, { method, body, signal: ac.signal, headers: { ...headers, authorization: `Bearer ${creds.token}`, "x-pm-uid": creds.uid } });
          if (res.ok && res.body) {
            // Wait for the first body chunk under the same deadline, so a request that Lumo
            // accepted but never starts answering is retried before anything reaches the client.
            const reader = res.body.getReader();
            const first = await reader.read();
            const rest = new ReadableStream({
              start(ctrl) { if (!first.done) ctrl.enqueue(first.value); else ctrl.close(); },
              async pull(ctrl) { const { done, value } = await reader.read(); if (done) ctrl.close(); else ctrl.enqueue(value); },
              cancel(r) { reader.cancel(r); },
            });
            res = new Response(first.done ? null : rest, { status: res.status, headers: res.headers });
          }
        } finally { clearTimeout(timer); }
      } catch (e) {
        if (attempt < 2) { log(`${e.name === 'AbortError' || /no response/.test(String(e.message)) ? 'no first byte' : 'connection error'}, retry ${attempt + 1}: ${e.message}`); await sleep(1500 * (attempt + 1)); continue; }
        return new Response(JSON.stringify({ error: { message: `lumo-proxy: connection error: ${e.message}` } }), { status: 502, headers: { "content-type": "application/json" } });
      }
      if (res.status === 401 && !refreshed) {
        refreshed = true; await res.text().catch(() => {});
        const fresh = readCreds();
        log(`401 from Lumo; re-read login: ${fresh && fresh.token !== creds?.token ? "new token" : "same/none"}`);
        credsRef.current = fresh; continue;
      }
      if (res.status === 429 && attempt < 2) {
        await res.text().catch(() => {});
        const wait = Math.min(Number(res.headers.get("retry-after")) * 1000 || 5000 * (attempt + 1), 30000);
        log(`upstream 429, retry ${attempt + 1} in ${wait / 1000}s`); await sleep(wait); continue;
      }
      if (res.status >= 500 && attempt < 2) { await res.text().catch(() => {}); log(`upstream ${res.status}, retry ${attempt + 1}`); await sleep(1500 * (attempt + 1)); continue; }
      return res;
    }
  };
}
