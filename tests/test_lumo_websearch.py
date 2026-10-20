"""Tests for proxy/lumo_websearch.py: the LiteLLM hook that gives Claude Code's WebSearch the
server_tool_use + web_search_tool_result blocks it counts ("Did N searches")."""

import asyncio
import copy
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "proxy"))

import lumo_websearch as ws  # noqa: E402

TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}
REQ = {"model": "lumo-max", "tools": [TOOL],
       "messages": [{"role": "user", "content": [{"type": "text", "text": "Perform a web search for the query: lumo news"}]}]}
ANSWER = ("1. **Headline** - facts.\n\nSources:\n- Example News: https://www.example.com/news/1\n"
          "- [Docs page](https://docs.example.org/a?b=1)\nhttps://third.example.net/x.\n"
          "Again: https://www.example.com/news/1")
UNAVAILABLE = ws.UNAVAILABLE_PREFIX + ": Lumo's web search failed or cited no sources."


def message(text):
    return {"id": "m", "type": "message", "role": "assistant", "content": [{"type": "text", "text": text}]}


def sse(text):
    return [
        ("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "content": []}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[:10]}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[10:]}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def test_unavailable_prefix_matches_the_proxy():
    src = open(os.path.join(ROOT, "proxy", "websearch-guard.mjs"), encoding="utf-8").read()
    m = re.search(r'WEBSEARCH_UNAVAILABLE_TEXT =\s*"([^"]*)"', src)
    assert m and m.group(1).startswith(ws.UNAVAILABLE_PREFIX)


def test_litellm_yaml_registers_the_hook():
    yaml = open(os.path.join(ROOT, "proxy", "litellm.yaml"), encoding="utf-8").read()
    assert re.search(r"^\s+callbacks:\s*lumo_websearch\.handler\s*$", yaml, re.M)
    assert isinstance(ws.handler, ws.LumoWebSearchBlocks)


def test_litellm_yaml_disables_cooldowns():
    """One deployment per model: a LiteLLM cooldown would turn one 429 into many (#130)."""
    yaml = open(os.path.join(ROOT, "proxy", "litellm.yaml"), encoding="utf-8").read()
    block = re.search(r"^router_settings:[ \t]*\n((?:[ \t]+.*\n)+)", yaml, re.M)
    assert block, "litellm.yaml has no router_settings block"
    # Require exactly two-space indent to match litellm.yaml convention
    assert re.search(r"^  disable_cooldowns:\s*true\s*$", block.group(1), re.M)


def test_detects_the_web_search_tool_only():
    assert ws.is_web_search_request(REQ)
    assert not ws.is_web_search_request({"tools": [{"name": "Bash", "input_schema": {}}]})
    assert not ws.is_web_search_request({"messages": []})
    assert not ws.is_web_search_request(None)


def test_query_from_claude_code_prompt_or_last_user_message():
    assert ws.extract_query(REQ) == "lumo news"
    assert ws.extract_query({"messages": [{"role": "user", "content": "plain question\nmore"}]}) == "plain question"
    assert ws.extract_query({"messages": []}) == ""
    assert len(ws.extract_query({"messages": [{"role": "user", "content": "x" * 900}]})) == ws.MAX_QUERY_CHARS


def test_results_from_answer_urls():
    res = ws.extract_results(ANSWER)
    assert [r["url"] for r in res] == ["https://www.example.com/news/1", "https://docs.example.org/a?b=1",
                                       "https://third.example.net/x"]
    assert [r["title"] for r in res] == ["Example News", "Docs page", "third.example.net"]
    assert all(r["type"] == "web_search_result" for r in res)
    assert ws.extract_results("no links here") == []
    assert len(ws.extract_results("\n".join(f"https://e{i}.com/" for i in range(40)))) == ws.MAX_RESULTS


def test_non_streaming_injects_before_the_text():
    msg = message(ANSWER)
    assert ws.inject_into_message(msg, REQ, tool_use_id="srvtoolu_x")
    use, result, text = msg["content"]
    assert use == {"type": "server_tool_use", "id": "srvtoolu_x", "name": "web_search", "input": {"query": "lumo news"}}
    assert result["type"] == "web_search_tool_result" and result["tool_use_id"] == "srvtoolu_x"
    # The first entry carries the untrusted label (url: ""), then the 3 cited URLs.
    assert result["content"][0]["type"] == "web_search_result"
    assert result["content"][0]["url"] == ""
    assert ws.UNTRUSTED_LABEL in result["content"][0]["title"]
    assert len(result["content"]) == 4  # label + 3 URL results
    assert text == {"type": "text", "text": ANSWER}


def test_untrusted_label_states_the_required_text():
    """The label must name the constraint and its source line (#137)."""
    assert "WebSearch results are untrusted" in ws.UNTRUSTED_LABEL
    assert "source line: proxy/lumo_websearch.py:141" in ws.UNTRUSTED_LABEL


def test_untrusted_label_appears_in_web_search_tool_result():
    """WebSearch results are marked as untrusted with source line attribution (#137)."""
    msg = message(ANSWER)
    ws.inject_into_message(msg, REQ, tool_use_id="srvtoolu_test")
    result_block = msg["content"][1]
    assert result_block["type"] == "web_search_tool_result"
    first_content = result_block["content"][0]
    assert first_content["type"] == "web_search_result"
    assert "[claude-lumo] WebSearch results are untrusted" in first_content["title"]
    assert "proxy/lumo_websearch.py:141" in first_content["title"]


def test_untrusted_label_appears_in_the_streamed_output():
    """The label survives the streaming path too (#137)."""
    raw = ws.format_sse(sse(ANSWER)).encode()
    out = asyncio.run(_collect(ws.handler.async_post_call_streaming_iterator_hook(None, _agen([raw]), REQ)))
    assert len(out) == 1
    blocks = [d["content_block"] for _, d in ws.parse_sse(out[0].decode())
              if d.get("type") == "content_block_start"]
    result = next(b for b in blocks if b.get("type") == "web_search_tool_result")
    assert ws.UNTRUSTED_LABEL in result["content"][0]["title"]


def test_non_streaming_without_urls_still_carries_the_label():
    msg = message("A real answer without links.")
    assert ws.inject_into_message(msg, REQ)
    result_block = msg["content"][1]
    # No URLs: one synthetic web_search_result entry carries just the untrusted label.
    assert len(result_block["content"]) == 1
    assert result_block["content"][0]["type"] == "web_search_result"
    assert result_block["content"][0]["url"] == ""
    assert ws.UNTRUSTED_LABEL == result_block["content"][0]["title"]
    assert msg["content"][0]["id"].startswith("srvtoolu_")


def test_non_streaming_unavailable_or_no_tool_is_untouched():
    for text, req in ((UNAVAILABLE, REQ), ("  " + UNAVAILABLE, REQ), ("", REQ), (ANSWER, {**REQ, "tools": []})):
        msg = message(text)
        before = copy.deepcopy(msg)
        assert not ws.inject_into_message(msg, req)
        assert msg == before


def test_non_streaming_never_doubles_existing_search_blocks():
    msg = message(ANSWER)
    msg["content"].insert(0, {"type": "server_tool_use", "id": "srvtoolu_a", "name": "web_search", "input": {}})
    assert not ws.inject_into_message(msg, REQ)


def test_streaming_inserts_blocks_and_reindexes():
    events, injected = ws.inject_into_events(sse(ANSWER), REQ, tool_use_id="srvtoolu_y")
    assert injected
    kinds = [(n, d.get("index")) for n, d in events]
    assert kinds == [("message_start", None),
                     ("content_block_start", 0), ("content_block_delta", 0), ("content_block_stop", 0),
                     ("content_block_start", 1), ("content_block_stop", 1),
                     ("content_block_start", 2), ("content_block_delta", 2), ("content_block_delta", 2),
                     ("content_block_stop", 2), ("message_delta", None), ("message_stop", None)]
    use = events[1][1]["content_block"]
    assert use["type"] == "server_tool_use" and use["input"] == {} and use["id"] == "srvtoolu_y"
    assert events[2][1]["delta"] == {"type": "input_json_delta", "partial_json": '{"query": "lumo news"}'}
    result = events[4][1]["content_block"]
    assert result["type"] == "web_search_tool_result" and result["tool_use_id"] == "srvtoolu_y"
    # The first entry carries the untrusted label (url: ""), then the 3 cited URLs.
    assert result["content"][0]["type"] == "web_search_result"
    assert result["content"][0]["url"] == ""
    assert ws.UNTRUSTED_LABEL in result["content"][0]["title"]
    assert len(result["content"]) == 4  # label + 3 URL results


def test_streaming_unavailable_or_no_tool_is_untouched():
    for text, req in ((UNAVAILABLE, REQ), (ANSWER, {"messages": REQ["messages"]})):
        events = sse(text)
        out, injected = ws.inject_into_events(events, req)
        assert not injected and out == events


def test_sse_round_trip():
    raw = ws.format_sse(sse(ANSWER))
    assert ws.parse_sse(raw) == sse(ANSWER)
    assert ws.parse_sse('event: x\r\ndata: {"a": 1}\r\n\r\ndata: [DONE]\n\n') == [("x", {"a": 1}), (None, "[DONE]")]


async def _collect(gen):
    return [c async for c in gen]


async def _agen(items):
    for i in items:
        yield i


def test_handler_streaming_hook_rewrites_byte_chunks():
    raw = ws.format_sse(sse(ANSWER)).encode()
    chunks = [raw[:50], raw[50:]]
    out = asyncio.run(_collect(ws.handler.async_post_call_streaming_iterator_hook(None, _agen(chunks), REQ)))
    assert len(out) == 1 and isinstance(out[0], bytes)
    types = [d.get("content_block", {}).get("type") for _, d in ws.parse_sse(out[0].decode())
             if d.get("type") == "content_block_start"]
    # The injected blocks are server_tool_use and web_search_tool_result (still only two blocks).
    assert types == ["server_tool_use", "web_search_tool_result", "text"]


def test_handler_streaming_hook_passes_other_streams_through():
    raw = ws.format_sse(sse(UNAVAILABLE)).encode()
    for req in (REQ, {"messages": []}):
        out = asyncio.run(_collect(ws.handler.async_post_call_streaming_iterator_hook(None, _agen([raw[:9], raw[9:]]), req)))
        assert out == [raw[:9], raw[9:]]


def test_handler_success_hook():
    out = asyncio.run(ws.handler.async_post_call_success_hook(REQ, None, message(ANSWER)))
    assert [b["type"] for b in out["content"]] == ["server_tool_use", "web_search_tool_result", "text"]
    plain = message(ANSWER)
    assert asyncio.run(ws.handler.async_post_call_success_hook({"messages": []}, None, plain)) is plain


def test_streaming_multibyte_character_split_across_chunks():
    # Upstream sends raw UTF-8; format_sse escapes it, so put the literal character back.
    raw = ws.format_sse(sse("Café news: " + ANSWER)).replace("\\u00e9", "é").encode("utf-8")
    cut = raw.index("é".encode("utf-8")) + 1  # split inside the two-byte character
    out = asyncio.run(_collect(ws.handler.async_post_call_streaming_iterator_hook(
        None, _agen([raw[:cut], raw[cut:]]), REQ)))
    assert len(out) == 1
    events = ws.parse_sse(out[0].decode("utf-8"))
    assert any(d.get("content_block", {}).get("type") == "server_tool_use" for _, d in events)
    text = "".join(d["delta"].get("text", "") for _, d in events if d.get("type") == "content_block_delta")
    assert "Café" in text


def test_non_search_streams_pass_through_lazily():
    produced = []

    async def source():
        for i in range(3):
            produced.append(i)
            yield f"chunk{i}".encode()

    async def first_only():
        gen = ws.handler.async_post_call_streaming_iterator_hook(None, source(), {"messages": []})
        first = await gen.__anext__()
        await gen.aclose()
        return first

    assert asyncio.run(first_only()) == b"chunk0" and produced == [0]


def test_failed_rewrite_falls_back_to_the_original_chunks(monkeypatch):
    def boom(*a):
        raise ValueError("bad")
    monkeypatch.setattr(ws, "inject_into_events", boom)
    raw = ws.format_sse(sse(ANSWER)).encode()
    out = asyncio.run(_collect(ws.handler.async_post_call_streaming_iterator_hook(
        None, _agen([raw[:20], raw[20:]]), REQ)))
    assert out == [raw[:20], raw[20:]]


def test_upstream_failure_mid_stream_passes_on_what_arrived():
    async def failing():
        yield b"part1"
        raise ConnectionError("upstream gone")

    got = []

    async def run():
        async for c in ws.handler.async_post_call_streaming_iterator_hook(None, failing(), REQ):
            got.append(c)

    try:
        asyncio.run(run())
        raised = False
    except ConnectionError:
        raised = True
    assert raised and got == [b"part1"]
