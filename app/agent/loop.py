"""Tool-calling loop. The model must finish by calling final_answer."""

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import ToolError, dispatch, tool_schemas
from app.config import get_settings
from app.schemas import AskResponse, FinalAnswer, ToolTraceEntry

logger = logging.getLogger(__name__)

UNGROUNDED_DECLINE = (
    "Declined because the answer had no supporting sources. "
    "An answer with no supporting data is not returned."
)
LIMIT_DECLINE = (
    "Declined because the tool-call limit was reached before a final answer. "
    "No unsupported answer is returned."
)
_RATE_LIMIT_ATTEMPTS = 3
_RATE_LIMIT_DELAY_SECONDS = 1.0


def run_agent(
    question: str,
    *,
    client: Any = None,
    max_iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AskResponse:
    settings = get_settings()
    limit = settings.llm_max_tool_iterations if max_iterations is None else max_iterations
    llm = client or OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trace: list[ToolTraceEntry] = []
    tools = tool_schemas()
    for _ in range(limit):
        message, message_dict = _complete(llm, messages, tools, sleep)
        messages.append(message_dict)
        calls = _tool_calls(message)
        if not calls:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Call a tool. When you are ready to finish, call final_answer. "
                        "Do not answer in plain text."
                    ),
                }
            )
            continue
        finished = _handle_calls(calls, messages, trace)
        if finished is not None:
            return finished
    logger.info("tool iteration limit reached (%s)", limit)
    return AskResponse(
        answer=LIMIT_DECLINE,
        declined=True,
        data_used="none",
        sources=[],
        tool_trace=trace,
    )


def _handle_calls(
    calls: list[Any],
    messages: list[dict[str, Any]],
    trace: list[ToolTraceEntry],
) -> AskResponse | None:
    data_calls = [call for call in calls if _call_name(call) != "final_answer"]
    final_calls = [call for call in calls if _call_name(call) == "final_answer"]
    if data_calls and final_calls:
        for call in data_calls:
            _execute_data_tool(call, messages, trace)
        for call in final_calls:
            args, _error = _call_args(call)
            trace.append(ToolTraceEntry(tool="final_answer", args=args, ok=False))
            messages.append(
                _tool_message(
                    _call_id(call),
                    "final_answer",
                    {
                        "error": (
                            "Ignored because other tools were called in the same step. "
                            "Read those results, then call final_answer."
                        )
                    },
                )
            )
        return None
    if final_calls:
        accepted: FinalAnswer | None = None
        for call in final_calls:
            parsed = _parse_final_answer(call, messages, trace)
            if parsed is not None:
                accepted = parsed
        if accepted is not None:
            return _response_from_final(accepted, trace)
        return None
    for call in data_calls:
        _execute_data_tool(call, messages, trace)
    return None


def _parse_final_answer(
    call: Any,
    messages: list[dict[str, Any]],
    trace: list[ToolTraceEntry],
) -> FinalAnswer | None:
    args, error = _call_args(call)
    if error is not None:
        trace.append(ToolTraceEntry(tool="final_answer", args=args, ok=False))
        messages.append(
            _tool_message(
                _call_id(call),
                "final_answer",
                {"error": f"invalid JSON arguments: {error}"},
            )
        )
        return None
    try:
        parsed = FinalAnswer.model_validate(args)
    except ValidationError as exc:
        trace.append(ToolTraceEntry(tool="final_answer", args=args, ok=False))
        messages.append(
            _tool_message(
                _call_id(call),
                "final_answer",
                {"error": "final_answer failed validation", "details": json.loads(exc.json())},
            )
        )
        return None
    trace.append(ToolTraceEntry(tool="final_answer", args=args, ok=True))
    return parsed


def _execute_data_tool(
    call: Any,
    messages: list[dict[str, Any]],
    trace: list[ToolTraceEntry],
) -> None:
    name = _call_name(call)
    args, error = _call_args(call)
    if error is not None:
        logger.info("tool %s rejected: invalid JSON", name)
        trace.append(ToolTraceEntry(tool=name, args=args, ok=False))
        messages.append(
            _tool_message(_call_id(call), name, {"error": f"invalid JSON arguments: {error}"})
        )
        return
    try:
        payload = dispatch(name, args)
    except ToolError as exc:
        logger.info("tool %s rejected: %s", name, exc)
        trace.append(ToolTraceEntry(tool=name, args=args, ok=False))
        messages.append(_tool_message(_call_id(call), name, {"error": str(exc)}))
        return
    except Exception as exc:
        logger.exception("tool %s failed", name)
        trace.append(ToolTraceEntry(tool=name, args=args, ok=False))
        messages.append(_tool_message(_call_id(call), name, {"error": str(exc)}))
        return
    logger.info("tool %s ok", name)
    trace.append(ToolTraceEntry(tool=name, args=args, ok=True))
    messages.append(_tool_message(_call_id(call), name, payload))


def _response_from_final(final: FinalAnswer, trace: list[ToolTraceEntry]) -> AskResponse:
    if not final.declined and not final.sources:
        logger.info("overriding ungrounded final_answer to a decline")
        return AskResponse(
            answer=UNGROUNDED_DECLINE,
            declined=True,
            data_used="none",
            sources=[],
            tool_trace=trace,
        )
    return AskResponse(
        answer=final.answer,
        declined=final.declined,
        data_used=final.data_used,
        sources=list(final.sources),
        tool_trace=trace,
    )


def _complete(
    client: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    sleep: Callable[[float], None],
) -> tuple[Any, dict[str, Any]]:
    delay = _RATE_LIMIT_DELAY_SECONDS
    for attempt in range(_RATE_LIMIT_ATTEMPTS):
        try:
            return _request(client, messages, tools)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 429 or attempt == _RATE_LIMIT_ATTEMPTS - 1:
                raise
            logger.warning("LLM rate limited (429); retrying in %.0fs", delay)
            sleep(delay)
            delay *= 2
    raise RuntimeError("LLM rate limit retries exhausted")


def _request(
    client: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> tuple[Any, dict[str, Any]]:
    kwargs = {
        "model": get_settings().llm_model,
        "messages": messages,
        "tools": tools,
    }
    raw_api = getattr(client.chat.completions, "with_raw_response", None)
    if raw_api is None:
        response = client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        return message, _assistant_message(message)
    # Keep the provider message intact, including Gemini thought signatures.
    raw = raw_api.create(**kwargs)
    parsed = raw.parse()
    payload = json.loads(_raw_text(raw))
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("LLM returned no choices")
    message_dict = dict(choices[0]["message"])
    if message_dict.get("role") == "model":
        message_dict["role"] = "assistant"
    else:
        message_dict["role"] = "assistant"
    return parsed.choices[0].message, message_dict


def _raw_text(raw: Any) -> str:
    for candidate in (raw, getattr(raw, "http_response", None)):
        if candidate is None:
            continue
        text = getattr(candidate, "text", None)
        if isinstance(text, str):
            return text
        if callable(text):
            value = text()
            if isinstance(value, str):
                return value
        content = getattr(candidate, "content", None)
        if callable(content):
            content = content()
        if isinstance(content, bytes):
            return content.decode()
    raise RuntimeError("LLM raw response had no body")


def _assistant_message(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        payload = dict(message)
    else:
        payload = message.model_dump(exclude_none=True)
        extra = getattr(message, "model_extra", None) or {}
        for key, value in extra.items():
            payload.setdefault(key, value)
        dumped_calls = payload.get("tool_calls") or []
        calls = getattr(message, "tool_calls", None) or []
        if calls and dumped_calls:
            payload["tool_calls"] = [
                {**dumped, **(getattr(call, "model_extra", None) or {})}
                for call, dumped in zip(calls, dumped_calls)
            ]
    payload["role"] = "assistant"
    return payload


def _tool_calls(message: Any) -> list[Any]:
    if isinstance(message, dict):
        return list(message.get("tool_calls") or [])
    return list(getattr(message, "tool_calls", None) or [])


def _call_name(call: Any) -> str:
    if isinstance(call, dict):
        return call["function"]["name"]
    return call.function.name


def _call_id(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("id") or "")
    return str(call.id)


def _call_args(call: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(call, dict):
        raw = call.get("function", {}).get("arguments", {})
    else:
        raw = call.function.arguments
    if isinstance(raw, dict):
        return raw, None
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return {}, str(exc)
    if not isinstance(parsed, dict):
        return {}, "tool arguments must be a JSON object"
    return parsed, None


def _tool_message(call_id: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps(payload, default=_json_default),
    }


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
