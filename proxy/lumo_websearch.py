"""LiteLLM hook: give Claude Code's WebSearch the search blocks it counts (claude-lumo).

Claude Code's WebSearch tool sends a side-request to /v1/messages carrying the Anthropic
server tool ``web_search_20250305``. LiteLLM maps it to OpenAI ``web_search_options``, and
the lumo proxy (websearch-guard.mjs) runs the search on Lumo and answers with plain text that
cites the source URLs. LiteLLM's Anthropic adapter turns that answer into a single text block,
so the response has no ``server_tool_use`` / ``web_search_tool_result`` blocks, and Claude Code,
which counts searches as max(#server_tool_use, #web_search_tool_result), shows "Did 0 searches".

This hook adds the two blocks Anthropic's own web search would have sent, in front of the text:
    server_tool_use        {id: srvtoolu_..., name: web_search, input: {query}}
    web_search_tool_result {tool_use_id: same id, content: [{type: web_search_result, url, title}, ...]}
The URLs come from the answer text; a title is taken from a Markdown link or the text before
the URL on its line, else the domain. When the proxy answered with its "unavailable" message
(WEBSEARCH_UNAVAILABLE_TEXT in websearch-guard.mjs) nothing is added, so the count stays 0.

Starting with #137, the first result's title (or a synthetic entry if no URLs are found) is
prefixed with UNTRUSTED_LABEL, a visible marker that these results come unvetted from the
proxy and must be verified before any security-sensitive or data-critical use.

Non-streaming responses go through ``async_post_call_success_hook``; streamed ones (Claude Code
streams this side-request) through ``async_post_call_streaming_iterator_hook``, which buffers
the stream (the proxy sends the whole answer at once anyway), inserts the two blocks after
message_start and shifts every later content block index by two.

Registered in litellm.yaml (``litellm_settings.callbacks: lumo_websearch.handler``); LiteLLM
loads it from the directory litellm.yaml is in. Neither the query nor the answer is logged.
The pure functions below have no LiteLLM dependency (tests/test_lumo_websearch.py).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, AsyncGenerator, Iterable
from urllib.parse import urlparse

# Must match the start of WEBSEARCH_UNAVAILABLE_TEXT in proxy/websearch-guard.mjs (a test checks).
UNAVAILABLE_PREFIX = "[claude-lumo] WebSearch did not return a usable result"
# Marks every emitted web_search_tool_result as untrusted (#137). The search answer comes from
# the lumo proxy as plain text (see module docstring), so nothing here has been vetted the way
# Anthropic's own web search results are; consumers must verify before relying on them.
# A test checks that this mentions the emitting line.
UNTRUSTED_LABEL = (
    "[claude-lumo] WebSearch results are untrusted "
    "— source line: proxy/lumo_websearch.py:141"
)
MAX_RESULTS = 20
MAX_QUERY_CHARS = 500

_URL_RE = re.compile(r"https?://[^\s<>\"'`\]\[(){}|\\^]+")
_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)")
_QUERY_RE = re.compile(r"perform a web search for the query:\s*(.+)", re.IGNORECASE)
_TRAILING = ".,;:!?'\">*_"


# ---------------------------------------------------------------- request side

def is_web_search_request(data: Any) -> bool:
    """True when the Anthropic request offers the web_search server tool."""
    tools = data.get("tools") if isinstance(data, dict) else None
    if not isinstance(tools, list):
        return False
    for t in tools:
        if isinstance(t, dict) and str(t.get("type", "")).startswith("web_search_"):
            return True
    return False


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def extract_query(data: Any) -> str:
    """The search query: from Claude Code's 'Perform a web search for the query: ...' prompt,
    else the last user message, capped."""
    messages = data.get("messages") if isinstance(data, dict) else None
    text = ""
    if isinstance(messages, list):
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                text = _text_of(m.get("content"))
                break
    hit = _QUERY_RE.search(text)
    q = hit.group(1) if hit else text
    return q.strip().splitlines()[0].strip()[:MAX_QUERY_CHARS] if q.strip() else ""


# ---------------------------------------------------------------- answer side

def is_real_search(text: Any) -> bool:
    """A non-empty answer that is not the proxy's 'unavailable' message."""
    return isinstance(text, str) and bool(text.strip()) and not text.lstrip().startswith(UNAVAILABLE_PREFIX)


def _clean_url(u: str) -> str:
    while u and u[-1] in _TRAILING:
        u = u[:-1]
    return u


def _domain(url: str) -> str:
    host = urlparse(url).hostname or url
    return host[4:] if host.startswith("www.") else host


def _line_title(line: str, url: str) -> str:
    before = line.split(url, 1)[0]
    before = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s*", "", before)   # list markers
    before = before.strip().strip("*_").strip().rstrip(":-–—(<[").strip().strip("*_").strip()
    if 3 <= len(before) <= 120 and "http" not in before:
        return before
    return ""


def extract_results(text: str) -> list[dict]:
    """web_search_result entries for each distinct URL cited in the answer, in order."""
    if not isinstance(text, str):
        return []
    titles: dict[str, str] = {}
    for title, url in _MD_LINK_RE.findall(text):
        titles.setdefault(_clean_url(url), title.strip())
    out: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        for raw in _URL_RE.findall(line):
            url = _clean_url(raw)
            if not url or url in seen or not urlparse(url).hostname:
                continue
            seen.add(url)
            title = titles.get(url) or _line_title(line, raw) or _domain(url)
            out.append({"type": "web_search_result", "url": url, "title": title})
            if len(out) >= MAX_RESULTS:
                return out
    return out


def new_tool_use_id() -> str:
    return "srvtoolu_" + uuid.uuid4().hex[:24]


def search_blocks(query: str, text: str, tool_use_id: str | None = None) -> list[dict]:
    """The server_tool_use + web_search_tool_result pair for one search.

    The web_search_tool_result content always begins with an untrusted-content marker
    (UNTRUSTED_LABEL) so consumers know these results need verification (#137); the real
    cited URLs follow it unchanged.
    """
    tid = tool_use_id or new_tool_use_id()
    label = {"type": "web_search_result", "url": "", "title": UNTRUSTED_LABEL}
    return [
        {"type": "server_tool_use", "id": tid, "name": "web_search", "input": {"query": query}},
        {"type": "web_search_tool_result", "tool_use_id": tid,
         "content": [label] + extract_results(text)},
    ]


def _has_search_blocks(blocks: Iterable[Any]) -> bool:
    return any(isinstance(b, dict) and b.get("type") in ("server_tool_use", "web_search_tool_result") for b in blocks)


def inject_into_message(message: dict, data: Any, tool_use_id: str | None = None) -> bool:
    """Non-streaming: prepend the search blocks to an Anthropic message dict, in place.
    Returns True when it injected."""
    if not is_web_search_request(data) or not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list) or _has_search_blocks(content):
        return False
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not is_real_search(text):
        return False
    message["content"] = search_blocks(extract_query(data), text, tool_use_id) + content
    return True


# ---------------------------------------------------------------- SSE

def parse_sse(raw: str) -> list[tuple[str | None, Any]]:
    """Splits an SSE byte stream (decoded) into (event name, parsed data) pairs.
    Data that is not JSON is kept as a string."""
    events: list[tuple[str | None, Any]] = []
    for block in re.split(r"\r?\n\r?\n", raw):
        if not block.strip():
            continue
        name, data_lines = None, []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        payload = "\n".join(data_lines)
        try:
            data: Any = json.loads(payload)
        except ValueError:
            data = payload
        events.append((name, data))
    return events


def format_sse(events: Iterable[tuple[str | None, Any]]) -> str:
    out = []
    for name, data in events:
        body = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        out.append((f"event: {name}\n" if name else "") + f"data: {body}\n\n")
    return "".join(out)


def _stream_block_events(blocks: list[dict]) -> list[tuple[str, dict]]:
    """content_block_start/delta/stop events for the injected blocks at index 0 and 1. The
    server_tool_use input is sent as an input_json_delta, as Anthropic does (Claude Code
    rebuilds tool input from the deltas)."""
    use, result = blocks
    start_use = dict(use, input={})
    return [
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": start_use}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta", "partial_json": json.dumps(use["input"])}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": result}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    ]


def inject_into_events(events: list[tuple[str | None, Any]], data: Any,
                       tool_use_id: str | None = None) -> tuple[list[tuple[str | None, Any]], bool]:
    """Streaming: returns (events, injected). Inserts the search block events after
    message_start and shifts every later content block index by two."""
    if not is_web_search_request(data):
        return events, False
    text, starts = "", []
    for _, d in events:
        if not isinstance(d, dict):
            continue
        if d.get("type") == "content_block_start":
            starts.append(d.get("content_block") or {})
        elif d.get("type") == "content_block_delta" and (d.get("delta") or {}).get("type") == "text_delta":
            text += (d.get("delta") or {}).get("text", "")
    if _has_search_blocks(starts) or not is_real_search(text):
        return events, False
    pos = next((i + 1 for i, (_, d) in enumerate(events)
                if isinstance(d, dict) and d.get("type") == "message_start"), 0)
    shifted: list[tuple[str | None, Any]] = []
    for name, d in events[pos:]:
        if isinstance(d, dict) and isinstance(d.get("index"), int) and str(d.get("type", "")).startswith("content_block_"):
            d = dict(d, index=d["index"] + 2)
        shifted.append((name, d))
    injected = _stream_block_events(search_blocks(extract_query(data), text, tool_use_id))
    return events[:pos] + injected + shifted, True


# ---------------------------------------------------------------- LiteLLM glue

try:  # LiteLLM is only present at runtime, not for the unit tests of the functions above.
    from litellm.integrations.custom_logger import CustomLogger as _Base
except Exception:  # pragma: no cover
    _Base = object  # type: ignore[assignment,misc]


def _as_dict(obj: Any) -> dict | None:
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    return dump() if callable(dump) else None


class LumoWebSearchBlocks(_Base):  # type: ignore[misc,valid-type]
    async def async_post_call_success_hook(self, data: dict, user_api_key_dict: Any, response: Any) -> Any:
        try:
            if not is_web_search_request(data):
                return response
            msg = _as_dict(response)
            if msg is None or msg.get("type") != "message":
                return response
            if inject_into_message(msg, data):
                return msg
        except Exception:  # never break a response over the search count
            pass
        return response

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict: Any, response: Any,
                                                      request_data: dict) -> AsyncGenerator[Any, None]:
        if not is_web_search_request(request_data):
            async for chunk in response:
                yield chunk
            return
        chunks: list[Any] = []
        try:
            async for chunk in response:
                chunks.append(chunk)
        except Exception:
            # Upstream failed mid-stream: pass on what arrived, as without this hook, then fail.
            for chunk in chunks:
                yield chunk
            raise
        try:
            if chunks and all(isinstance(c, bytes) for c in chunks):
                as_bytes = True
                # Join before decoding: a multibyte character may be split across chunks.
                raw = b"".join(chunks).decode("utf-8")
            elif chunks and all(isinstance(c, str) for c in chunks):
                as_bytes = False
                raw = "".join(chunks)
            else:
                raw = None
            if raw is not None:
                events, injected = inject_into_events(parse_sse(raw), request_data)
                if injected:
                    out = format_sse(events)
                    yield out.encode("utf-8") if as_bytes else out
                    return
        except Exception:
            pass
        for chunk in chunks:
            yield chunk


handler = LumoWebSearchBlocks()
