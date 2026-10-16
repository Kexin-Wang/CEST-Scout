"""Check that a freshly started server can do the two things every CEST_Scout agent needs before
a 10-30 minute pipeline run is spent on it: (1) emit an OpenAI-style tool call for a request that
plainly needs a tool, and follow a tool result with a plain-text answer, and (2) return a parsable
JSON object under response_format={"type": "json_object"}. A model served with the wrong tool-call
parser fails (1) -- it writes the call as text -- and would otherwise "complete" a run with zero
tool calls and hallucinated findings (see hpc/README.md).

    python hpc/smoke_test.py <endpoint-url> <served-model-name> [--disable-thinking]

Exit status 0 when both checks pass. Reads the API key from ~/hpc-llm/.api_key.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from openai import OpenAI

TOOL = {
    "type": "function",
    "function": {
        "name": "search_pubmed",
        "description": "Search PubMed for articles matching a query and return their PMIDs and titles.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PubMed query string"},
                "max_results": {"type": "integer", "description": "How many results to return"},
            },
            "required": ["query"],
        },
    },
}


def _reasoning_len(msg) -> int:
    extra = getattr(msg, "model_extra", None) or {}
    text = extra.get("reasoning_content") or extra.get("reasoning") or ""
    return len(text or "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("model")
    ap.add_argument("--disable-thinking", action="store_true",
                    help="send chat_template_kwargs.enable_thinking=false, as the pipeline does with LLM_DISABLE_THINKING=true")
    args = ap.parse_args(argv)

    key = open(os.path.expanduser("~/hpc-llm/.api_key")).read().strip()
    client = OpenAI(api_key=key, base_url=args.url)
    # The template variable that switches thinking off differs per model family (Qwen3:
    # enable_thinking; DeepSeek V3.1+/V4: thinking); the preset exports LLM_THINKING_KWARG.
    kw = os.environ.get("LLM_THINKING_KWARG", "enable_thinking").strip() or "enable_thinking"
    body = {"extra_body": {"chat_template_kwargs": {kw: False}}} if args.disable_thinking else {}
    ok = True

    # (1) tool call, then a plain answer after the tool result.
    messages = [
        {"role": "system", "content": "You are a research assistant. Always look literature up with the "
                                      "search_pubmed tool before answering; never answer from memory."},
        {"role": "user", "content": "Which PubMed papers measured glycogen concentration in human liver by "
                                    "13C MRS? Search PubMed first."},
    ]
    r = client.chat.completions.create(model=args.model, messages=messages, tools=[TOOL], tool_choice="auto", **body)
    msg = r.choices[0].message
    print(f"[1] reasoning chars returned: {_reasoning_len(msg)}; content chars: {len(msg.content or '')}")
    if msg.tool_calls:
        tc = msg.tool_calls[0]
        try:
            call_args = json.loads(tc.function.arguments or "{}")
            print(f"[1] tool call OK: {tc.function.name}({json.dumps(call_args)[:200]})")
        except json.JSONDecodeError:
            print(f"[1] tool call emitted but arguments are not JSON: {tc.function.arguments[:200]!r}")
            ok = False
        messages.append({
            "role": "assistant", "content": msg.content or "",
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}],
        })
        messages.append({
            "role": "tool", "tool_call_id": tc.id,
            "content": json.dumps({"results": [{"pmid": "12345678", "title": "Hepatic glycogen quantified by "
                                                "natural-abundance 13C MRS in healthy volunteers"}]}),
        })
        r2 = client.chat.completions.create(model=args.model, messages=messages, tools=[TOOL], tool_choice="auto", **body)
        m2 = r2.choices[0].message
        if m2.tool_calls or (m2.content or "").strip():
            print(f"[1] follow-up after tool result OK ({'another tool call' if m2.tool_calls else str(len(m2.content)) + ' content chars'})")
        else:
            print("[1] follow-up after tool result returned nothing")
            ok = False
    else:
        print(f"[1] NO TOOL CALL. content: {(msg.content or '')[:300]!r}")
        ok = False

    # (2) structured JSON output, as the extraction step requests it.
    r3 = client.chat.completions.create(
        model=args.model,
        messages=[{"role": "user", "content": "Return a JSON object with keys 'metabolite' (string) and "
                                              "'concentration_mM' (number) for glycogen in human liver. "
                                              "Output only the JSON object."}],
        response_format={"type": "json_object"}, **body,
    )
    content = r3.choices[0].message.content or ""
    try:
        data = json.loads(content)
        print(f"[2] json_object OK: {json.dumps(data)[:200]}")
    except json.JSONDecodeError:
        print(f"[2] json_object FAILED to parse: {content[:300]!r}")
        ok = False

    usage = r.usage
    if usage:
        print(f"usage of call 1: prompt {usage.prompt_tokens}, completion {usage.completion_tokens}")
    print("SMOKE TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
