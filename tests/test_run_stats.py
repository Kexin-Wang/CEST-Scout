"""The counters run_research_agent keeps for a run's provenance record must reflect what the
loop actually did -- they are what the model comparison is built on."""
from __future__ import annotations

import json
from types import SimpleNamespace

from pydantic import BaseModel

from cest_scout.llm import new_run_stats, run_research_agent
from cest_scout.pipeline import _agent_stats


class _Out(BaseModel):
    items: list[str]


class _EchoSkill:
    TOOL_SPEC = {"type": "function", "function": {"name": "echo", "description": "echo", "parameters": {"type": "object"}}}

    @staticmethod
    def call(args: dict) -> dict:
        if args.get("boom"):
            raise ValueError("boom")
        return {"echo": args}


def _tool_call(args: dict):
    return SimpleNamespace(id="c1", function=SimpleNamespace(name="echo", arguments=json.dumps(args)))


def _resp(content: str | None = None, tool_calls=None, prompt=100, completion=20):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                           usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion))


class _FakeClient:
    def __init__(self, responses, base_url="http://node:8000/v1"):
        self._responses = iter(responses)
        self.base_url = base_url
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        return next(self._responses)


def test_stats_count_calls_tokens_tool_errors_repairs_and_retries():
    responses = [
        _resp(tool_calls=[_tool_call({"boom": 1})]),          # research: one failing tool call
        _resp(content="done"),                                # research: model stops
        _resp(content="{not json"),                           # extraction: invalid JSON
        _resp(content=json.dumps({"items": ["a"]})),          # valid but incomplete
        _resp(content=json.dumps({"items": ["a", "b"]})),     # complete
    ]
    stats = new_run_stats()
    out = run_research_agent(
        _FakeClient(responses), "m", "sys", "user", [_EchoSkill], _Out,
        output_check=lambda o: None if len(o.items) >= 2 else "need two items",
        stats_sink=stats,
    )
    assert out.items == ["a", "b"]
    assert stats["llm_calls"] == 5
    assert stats["prompt_tokens"] == 500 and stats["completion_tokens"] == 100
    assert stats["tool_calls"] == 1 and stats["tool_errors"] == 1
    assert stats["tool_error_samples"] == ["echo: boom"]
    assert stats["json_repairs"] == 1
    assert stats["output_retries"] == 1
    assert stats["output_check_passed"] is True
    assert stats["elapsed_s"] >= 0.0


def test_stats_record_a_failed_completeness_check_when_the_incomplete_output_is_kept():
    responses = [_resp(content="done")] + [_resp(content=json.dumps({"items": ["a"]}))] * 3
    stats = new_run_stats()
    out = run_research_agent(
        _FakeClient(responses), "m", "sys", "user", [], _Out,
        output_check=lambda o: "need two items", max_output_retries=2, stats_sink=stats,
    )
    assert out.items == ["a"]
    assert stats["output_retries"] == 2
    assert stats["output_check_passed"] is False


def test_stats_have_no_check_verdict_when_the_agent_has_no_check():
    stats = new_run_stats()
    run_research_agent(
        _FakeClient([_resp(content="done"), _resp(content=json.dumps({"items": []}))]),
        "m", "sys", "user", [], _Out, stats_sink=stats,
    )
    assert stats["output_check_passed"] is None


def test_agent_stats_records_endpoint_host_only_and_thinking_flag():
    counters = new_run_stats()
    counters.update(llm_calls=3, prompt_tokens=10, elapsed_s=1.5)
    s = _agent_stats("physicist", "qwen3.8-27b", _FakeClient([], base_url="http://esplhpc-cp087:8001/v1"),
                     {"chat_template_kwargs": {"enable_thinking": False}}, counters)
    assert s.endpoint == "esplhpc-cp087:8001"
    assert s.thinking_disabled is True
    assert s.llm_calls == 3 and s.prompt_tokens == 10 and s.elapsed_s == 1.5
    assert _agent_stats("x", "m", _FakeClient([]), None, counters).thinking_disabled is False


def test_budget_exhaustion_with_incomplete_coverage_grants_a_bounded_extension():
    # The model keeps calling tools until the budget runs out; the coverage check wants 'c'.
    responses = [
        _resp(tool_calls=[_tool_call({"x": "a"})]),
        _resp(tool_calls=[_tool_call({"x": "b"})]),       # budget of 2 spent here -> extension
        _resp(tool_calls=[_tool_call({"x": "c"})]),
        _resp(content="done"),                              # voluntary stop, coverage satisfied
        _resp(content=json.dumps({"items": ["a", "b", "c"]})),
    ]
    seen = lambda trace: {e["args"].get("x") for e in trace}  # noqa: E731
    stats = new_run_stats()
    out = run_research_agent(
        _FakeClient(responses), "m", "sys", "user", [_EchoSkill], _Out, max_tool_iters=2,
        coverage_check=lambda trace: None if "c" in seen(trace) else "still missing c",
        max_nudges=1, nudge_extra_iters=2, stats_sink=stats,
    )
    assert out.items == ["a", "b", "c"]
    assert stats["tool_calls"] == 3 and stats["nudges"] == 1 and stats["llm_calls"] == 5


def test_extensions_are_bounded_by_max_nudges():
    responses = [_resp(tool_calls=[_tool_call({"x": "a"})])] * 2 + [_resp(content=json.dumps({"items": []}))]
    stats = new_run_stats()
    run_research_agent(
        _FakeClient(responses), "m", "sys", "user", [_EchoSkill], _Out, max_tool_iters=1,
        coverage_check=lambda trace: "never satisfied", max_nudges=1, nudge_extra_iters=1, stats_sink=stats,
    )
    # 1 original iteration + 1 extension, then the loop gives up and extracts.
    assert stats["tool_calls"] == 2 and stats["nudges"] == 1 and stats["llm_calls"] == 3
