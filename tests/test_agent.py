"""Agent loop with a mocked LLM: tool dispatch and the empty-sources guardrail."""

import json
from types import SimpleNamespace

from app.agent.loop import run_agent
from app.schemas import AskResponse


def _completion(*calls: SimpleNamespace) -> SimpleNamespace:
    message = SimpleNamespace(content=None, tool_calls=list(calls), model_extra=None)

    def model_dump(exclude_none: bool = True) -> dict:
        payload = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in calls
            ],
        }
        if not exclude_none:
            payload["content"] = None
        return payload

    message.model_dump = model_dump
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


class _Completions:
    def __init__(self, steps: list, recorded: list[dict]) -> None:
        self.steps = steps
        self.recorded = recorded

    def create(self, **kwargs) -> SimpleNamespace:
        self.recorded.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class _FakeClient:
    def __init__(self, steps: list, recorded: list[dict] | None = None) -> None:
        self.recorded: list[dict] = [] if recorded is None else recorded
        self.chat = SimpleNamespace(completions=_Completions(steps, self.recorded))


def _fact_source() -> dict:
    return {
        "type": "fact",
        "ticker": "NVDA",
        "concept": "revenue",
        "fiscal_year": 2025,
        "period_end": "2025-01-26",
    }


def test_normal_tool_path_returns_answer_and_trace() -> None:
    recorded: list[dict] = []
    client = _FakeClient(
        [
            _completion(_call("call-1", "list_companies", {})),
            _completion(
                _call(
                    "call-2",
                    "final_answer",
                    {
                        "answer": "The universe includes NVIDIA.",
                        "declined": False,
                        "data_used": "numbers",
                        "sources": [_fact_source()],
                    },
                )
            ),
        ],
        recorded,
    )
    result = run_agent("Which companies are covered?", client=client)
    assert isinstance(result, AskResponse)
    assert result.declined is False
    assert result.answer == "The universe includes NVIDIA."
    assert result.data_used == "numbers"
    assert result.sources[0].ticker == "NVDA"
    assert result.sources[0].concept == "revenue"
    assert [(entry.tool, entry.ok) for entry in result.tool_trace] == [
        ("list_companies", True),
        ("final_answer", True),
    ]
    tool_messages = [message for message in recorded[1]["messages"] if message["role"] == "tool"]
    assert tool_messages
    assert "NVDA" in tool_messages[0]["content"]


def test_empty_sources_are_overridden_to_a_decline() -> None:
    client = _FakeClient(
        [
            _completion(
                _call(
                    "call-1",
                    "final_answer",
                    {
                        "answer": "NVIDIA revenue was $100 billion.",
                        "declined": False,
                        "data_used": "numbers",
                        "sources": [],
                    },
                )
            )
        ]
    )
    result = run_agent("What was NVDA revenue?", client=client)
    assert result.declined is True
    assert result.data_used == "none"
    assert result.sources == []
    assert "100 billion" not in result.answer
    assert result.tool_trace[0].tool == "final_answer"
    assert result.tool_trace[0].ok is True


def test_iteration_limit_returns_a_decline_with_the_trace() -> None:
    client = _FakeClient(
        [
            _completion(_call("call-1", "list_companies", {})),
            _completion(_call("call-2", "list_companies", {})),
        ]
    )
    result = run_agent("List the companies.", client=client, max_iterations=2)
    assert result.declined is True
    assert result.data_used == "none"
    assert result.sources == []
    assert [(entry.tool, entry.ok) for entry in result.tool_trace] == [
        ("list_companies", True),
        ("list_companies", True),
    ]


def test_rate_limit_retries_with_backoff() -> None:
    class _RateLimited(Exception):
        status_code = 429

    sleeps: list[float] = []
    client = _FakeClient(
        [
            _RateLimited("slow down"),
            _completion(
                _call(
                    "call-1",
                    "final_answer",
                    {
                        "answer": "Declined. Forward estimates are not in the filings database.",
                        "declined": True,
                        "data_used": "none",
                        "sources": [],
                    },
                )
            ),
        ]
    )
    result = run_agent(
        "What is next quarter's guidance?",
        client=client,
        sleep=sleeps.append,
    )
    assert sleeps == [1.0]
    assert result.declined is True
    assert result.answer.startswith("Declined. Forward estimates")
