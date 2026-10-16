"""Generic two-phase agent runner: (1) a tool-calling research loop, then
(2) a structured-JSON extraction call validated against a pydantic schema.

Kept as one shared function so the four agents (literature/chemist/
physicist/experimentalist) differ only in their system prompt, skills, and
output schema, not in how they talk to the API.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

ToolCallback = Callable[[str, dict], None]


def get_client(api_key: str, base_url: str | None) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=base_url)


def new_run_stats() -> dict:
    """The counters run_research_agent fills in when handed a stats_sink. Kept as a plain dict so
    this module stays independent of the report schemas; pipeline.py turns it into AgentRunStats.
    """
    return {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "tool_error_samples": [],
        "nudges": 0,
        "json_repairs": 0,
        "output_retries": 0,
        "output_check_passed": None,
        "elapsed_s": 0.0,
    }


def _record_usage(stats: dict, resp) -> None:
    stats["llm_calls"] += 1
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    stats["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
    stats["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)


def _shrink_for_context(value, _list_len_threshold: int = 12):
    """Replace long numeric/array lists (e.g. a simulated Z-spectrum's 61-point curves) with a
    short placeholder before a tool result is shown to the model. The model only needs to reason
    over the summary scalars a tool returns (contrast %, warnings, etc.), never the raw curve --
    callers that need the exact data (e.g. pipeline.py's report/plot generation) read it from
    trace_sink's untruncated result, captured before this shrinking is applied.
    """
    if isinstance(value, dict):
        return {k: _shrink_for_context(v, _list_len_threshold) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > _list_len_threshold:
            return f"<{len(value)} values omitted to save context -- read back exactly for the report>"
        return [_shrink_for_context(v, _list_len_threshold) for v in value]
    return value


# Roughly 4 characters per token, against a 131072-token server window, leaving room for the
# model's own reply and the final JSON-extraction turn. Only an estimate -- the point is to prune
# well before the hard limit, not to predict the tokenizer exactly.
_CONTEXT_CHAR_BUDGET = int(__import__("os").environ.get("LLM_CONTEXT_CHAR_BUDGET", 360_000))
_ELIDED_TOOL_RESULT = ('{"note": "older tool result elided to stay within the context window; its '
                       'exact values were captured for the report at the time of the call"}')


def _prune_tool_history(messages: list[dict], budget: int = _CONTEXT_CHAR_BUDGET) -> None:
    """Blank out the oldest tool RESULTS in place once the conversation outgrows the window.

    A long agent loop resends its whole history on every turn, so a run thorough enough to cover
    every metabolite can overflow the server's context even after big arrays are stripped -- the
    experimentalist agent hit exactly this at 43 tool calls. The results themselves are not lost:
    pipeline.py reconciles its numbers from trace_sink, which captured each result untruncated
    when the call was made, so the model losing sight of an old raw payload costs nothing that
    ends up in the report.

    Only the CONTENT of tool messages is replaced, never the messages themselves -- the API
    requires every assistant tool_call to keep its matching tool response, so dropping one
    outright would make the conversation unsendable.
    """
    total = sum(len(m.get("content") or "") for m in messages)
    if total <= budget:
        return
    for msg in messages:
        if total <= budget:
            return
        if msg.get("role") != "tool" or msg.get("content") == _ELIDED_TOOL_RESULT:
            continue
        total -= len(msg.get("content") or "") - len(_ELIDED_TOOL_RESULT)
        msg["content"] = _ELIDED_TOOL_RESULT


def run_research_agent(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    skills: list,
    schema_model: Type[T],
    max_tool_iters: int = 8,
    temperature: float | None = None,
    on_tool_call: ToolCallback | None = None,
    trace_sink: list[dict] | None = None,
    extra_body: dict | None = None,
    coverage_check: "Callable[[list[dict]], str | None] | None" = None,
    max_nudges: int = 3,
    output_check: "Callable[[T], str | None] | None" = None,
    max_output_retries: int = 2,
    stats_sink: dict | None = None,
    nudge_extra_iters: int = 10,
) -> T:
    # Reasoning models (e.g. the gpt-5.x family) reject any non-default temperature outright;
    # leaving it unset lets every model family work without per-model special-casing.
    temp_kwargs = {"temperature": temperature} if temperature is not None else {}
    body_kwargs = {"extra_body": extra_body} if extra_body else {}

    # Filled in place so the caller can read the counters even if this raises part-way through.
    stats = stats_sink if stats_sink is not None else new_run_stats()
    stats.update(new_run_stats())
    started = time.monotonic()

    def finish(passed: bool | None) -> None:
        stats["output_check_passed"] = passed
        stats["elapsed_s"] = round(time.monotonic() - started, 1)

    tools_spec = [s.TOOL_SPEC for s in skills]
    handlers = {s.TOOL_SPEC["function"]["name"]: s.call for s in skills}

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Kept regardless of trace_sink so coverage_check always has the full tool history to judge.
    own_trace: list[dict] = []
    nudges_used = 0

    iters_left = max_tool_iters
    stopped = False
    while iters_left > 0:
        iters_left -= 1
        _prune_tool_history(messages)
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=tools_spec, tool_choice="auto", **temp_kwargs, **body_kwargs,
        )
        _record_usage(stats, resp)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            # A model that stops calling tools is not necessarily finished. Observed with a local
            # Qwen3-32B: the physicist agent completed ONE metabolite of eight, wrote a summary
            # message, and this loop accepted that as "research done" -- the remaining seven were
            # silently dropped from the report while only 6 of 74 allowed tool calls had been
            # used. When the caller can state what complete coverage looks like, check it here and
            # send the model back to work instead of taking an early stop at face value.
            nudge = coverage_check(own_trace) if coverage_check and nudges_used < max_nudges else None
            if not nudge:
                stopped = True
                break
            nudges_used += 1
            stats["nudges"] = nudges_used
            messages.append({"role": "user", "content": nudge})
            continue

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if on_tool_call:
                on_tool_call(name, args)
            stats["tool_calls"] += 1
            try:
                result = handlers[name](args)
            except Exception as exc:  # tool failures are fed back to the model, not fatal
                result = {"error": str(exc)}
            if "error" in result:
                stats["tool_errors"] += 1
                if len(stats["tool_error_samples"]) < 5:
                    stats["tool_error_samples"].append(f"{name}: {str(result['error'])[:200]}")
            own_trace.append({"name": name, "args": args, "result": result})
            if trace_sink is not None:
                trace_sink.append({"name": name, "args": args, "result": result})
            messages.append({
                "role": "tool", "tool_call_id": tc.id, "content": json.dumps(_shrink_for_context(result))[:12000],
            })

        if iters_left == 0 and coverage_check and nudges_used < max_nudges:
            # The budget is spent but the model never stopped on its own, so the check above was
            # never consulted. Observed: an experimentalist stage used all 40 iterations on PubMed
            # searches and made zero simulate calls, leaving every organ prediction unsimulated
            # with no nudge ever sent. Say what is missing and grant a bounded extension.
            nudge = coverage_check(own_trace)
            if nudge:
                nudges_used += 1
                stats["nudges"] = nudges_used
                iters_left = nudge_extra_iters
                messages.append({
                    "role": "user",
                    "content": f"{nudge} You have {nudge_extra_iters} more tool calls for exactly this; "
                               "spend them on it and nothing else.",
                })
    if not stopped:
        messages.append({
            "role": "user",
            "content": "You have reached the tool-call limit. Stop researching and produce your final answer now.",
        })

    schema_json = schema_model.model_json_schema()
    extraction_prompt = (
        "Based on everything above, output ONLY a single JSON object (no markdown fences, no "
        f"commentary) that matches this JSON Schema for `{schema_model.__name__}`:\n\n"
        f"{json.dumps(schema_json)}\n\nDo not call any tools. Populate every field."
    )
    messages.append({"role": "user", "content": extraction_prompt})

    last_error: Exception | None = None
    last_valid: T | None = None
    output_retries = 0

    for _ in range(3 + max_output_retries):
        _prune_tool_history(messages)
        resp = client.chat.completions.create(
            model=model, messages=messages, response_format={"type": "json_object"}, **temp_kwargs, **body_kwargs,
        )
        _record_usage(stats, resp)
        content = resp.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
            candidate = schema_model.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            stats["json_repairs"] += 1
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"That JSON was invalid: {exc}\nRe-emit a corrected JSON object only."})
            continue

        # Schema-valid is not the same as complete. Observed with a local Qwen3-32B: the
        # literature agent ran six good PubMed searches and then wrote a single metabolite into
        # its JSON, and a separate run dropped seven of eight metabolites at this same step. The
        # research is already in the conversation, so asking for the omission to be filled in
        # costs one more extraction call and no extra tool work.
        last_valid = candidate
        complaint = output_check(candidate) if output_check else None
        if not complaint:
            # None when there was no check to pass; True when the check accepted this output.
            finish(True if output_check else None)
            return candidate
        if output_retries >= max_output_retries:
            # Out of retries: fall through to returning the incomplete output, recorded as such.
            # (Previously the check was simply skipped once the budget was spent, which let an
            # incomplete output through as if it had passed.)
            break
        output_retries += 1
        stats["output_retries"] = output_retries
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": complaint})

    if last_valid is not None:
        # Incomplete but structurally valid beats nothing: the caller's own deterministic checks
        # flag what is missing, and callers downstream would rather have partial data than an
        # exception that discards a whole run's research. The stats record that the output never
        # satisfied its completeness check, so a report can say so rather than hide it.
        finish(False)
        return last_valid
    finish(False)
    raise RuntimeError(f"Failed to get valid structured output for {schema_model.__name__} after retries: {last_error}")
