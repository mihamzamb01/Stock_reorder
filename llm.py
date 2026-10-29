"""LLM construction and robust structured-output invocation.

Both providers are OpenAI-compatible, so a single ``ChatOpenAI`` instance with a
different ``base_url`` covers OpenRouter and NVIDIA NIM.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, TypeVar

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from config.settings import PROVIDER_ENDPOINTS, ModelSpec, api_key_for

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Structured-output strategies, tried in order.
#
# function_calling comes first: it is the only method every provider here
# supports reliably, and it is markedly faster than strict json_schema on
# several models (measured: deepseek-v4.1-flash 3.9s vs 32.2s,
# nemotron-ultra 13.8s vs 22.9s). The result is validated by Pydantic either
# way, so the looser schema adherence of a tool call costs nothing.
_METHODS = ("function_calling", "json_schema", "json_mode")


def build_llm(spec: ModelSpec, usage: Any = None) -> ChatOpenAI:
    """Create a chat model for one agent role.

    ``usage`` is an optional observability token-use callback, attached to the
    model instance so it records every call - retries included.
    """
    base_url = spec.base_url or PROVIDER_ENDPOINTS.get(spec.provider, "")
    if not base_url:
        raise RuntimeError(
            f"No base URL for provider '{spec.provider}'. Set "
            f"MODEL_{spec.role.upper()}_BASE_URL or PROVIDER_BASE_URL in .env."
        )

    kwargs: dict[str, Any] = {
        "model": spec.model,
        "api_key": api_key_for(spec),
        "base_url": base_url,
        "max_tokens": spec.max_tokens,
        "timeout": 120,
        "max_retries": 3,
    }
    if spec.temperature is not None:
        kwargs["temperature"] = spec.temperature

    reasoning_body = _reasoning_body(spec)
    if reasoning_body:
        kwargs["extra_body"] = reasoning_body
    if usage is not None:
        kwargs["callbacks"] = [usage]

    if spec.provider == "openrouter":
        headers = {}
        site = _env("OPENROUTER_SITE_URL")
        app = _env("OPENROUTER_APP_NAME")
        if site:
            headers["HTTP-Referer"] = site
        if app:
            headers["X-Title"] = app
        if headers:
            kwargs["default_headers"] = headers
    return ChatOpenAI(**kwargs)


def _reasoning_body(spec: ModelSpec) -> dict[str, Any] | None:
    """Build the provider-specific request body that controls reasoning.

    The providers do not share a vocabulary here:

    - OpenRouter normalises every reasoning model behind ``reasoning.effort``
      (``low`` / ``medium`` / ``high`` / ``xhigh`` / ``max``).
    - NVIDIA NIM exposes reasoning through the chat template, as
      ``chat_template_kwargs.enable_thinking``.
    - Gateways such as LarpRouter proxy to whatever is upstream, so they get
      nothing by default - set ``MODEL_<ROLE>_REASONING_BODY`` for them.

    An explicit ``MODEL_<ROLE>_REASONING_BODY`` (raw JSON) always wins, so a
    model that needs something else can be configured without code changes.
    """
    if spec.reasoning_body:
        return spec.reasoning_body
    if not spec.reasoning_effort:
        return None
    if spec.provider == "openrouter":
        return {"reasoning": {"effort": spec.reasoning_effort}}
    if spec.provider == "nvidia":
        return {"chat_template_kwargs": {"enable_thinking": True}}
    # A gateway: the upstream decides, so send nothing.
    return None


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def structured_call(
    llm: ChatOpenAI,
    schema: type[T],
    messages: list[BaseMessage],
    *,
    attempts: int = 2,
    fallback_instruction: str | None = None,
    obs: Any = None,
    spec: ModelSpec | None = None,
    stage: str = "",
) -> T:
    """Invoke the model and parse a validated ``schema`` instance out of it.

    Tries ``json_schema`` -> ``function_calling`` -> ``json_mode``. Within each
    method a validation failure triggers one corrective retry. If every method
    fails the last raw content is parsed as plain JSON as a last resort.

    When ``obs`` (a :class:`observability.RunRecorder`) and ``spec`` are given,
    the whole resolution - retries and raw-JSON fallback included - is timed
    and logged as one trace step with the correlation ID of the current run.
    """
    if obs is None or spec is None:
        return _structured_call_raw(
            llm, schema, messages, attempts, fallback_instruction
        )

    with obs.step(stage=stage, role=spec.role, provider=spec.provider, model=spec.model):
        return _structured_call_raw(
            llm, schema, messages, attempts, fallback_instruction
        )


def _structured_call_raw(
    llm: ChatOpenAI,
    schema: type[T],
    messages: list[BaseMessage],
    attempts: int = 2,
    fallback_instruction: str | None = None,
) -> T:
    """The structured-output strategies, without observability wiring."""
    errors: list[str] = []

    for method in _METHODS:
        for attempt in range(attempts):
            try:
                runner = llm.with_structured_output(schema, method=method)
                result = runner.invoke(messages)
                if isinstance(result, schema):
                    return result
                if isinstance(result, dict):
                    return schema.model_validate(result)
                errors.append(
                    f"{method}#{attempt} returned {type(result).__name__}, expected {schema.__name__}"
                )
            except (ValidationError, ValueError) as exc:
                errors.append(f"{method}#{attempt} validation: {exc}")
                messages = _repair(messages, schema, exc)
            except Exception as exc:  # provider-level failure: try next method
                errors.append(f"{method}#{attempt} error: {exc}")
                break

    raw = _last_json_object(llm, messages, schema, fallback_instruction)
    if raw is not None:
        return schema.model_validate(raw)

    raise RuntimeError(
        "All structured-output methods failed:\n  " + "\n  ".join(errors[-6:])
    )


def _repair(
    messages: list[BaseMessage], schema: type[T], exc: Exception
) -> list[BaseMessage]:
    """Append a corrective message so the model can fix its previous output."""
    schema_hint = json.dumps(schema.model_json_schema(), indent=2)[:4000]
    return list(messages) + [
        HumanMessage(
            content=(
                "Your previous response did not satisfy the required schema.\n"
                f"Validation error: {exc}\n\n"
                "Return ONLY valid JSON matching this JSON Schema:\n"
                f"{schema_hint}"
            )
        )
    ]


def _last_json_object(
    llm: ChatOpenAI,
    messages: list[BaseMessage],
    schema: type[T],
    fallback_instruction: str | None,
) -> dict[str, Any] | None:
    """Ask for raw JSON and extract the first parsable JSON object."""
    instruction = fallback_instruction or (
        "Respond with a single JSON object and nothing else. "
        "Do not wrap it in markdown fences. Do not add commentary."
    )
    try:
        response = llm.invoke(
            list(messages)
            + [
                HumanMessage(
                    content=(
                        f"{instruction}\n\n"
                        "Required JSON Schema:\n"
                        f"{json.dumps(schema.model_json_schema(), indent=2)[:4000]}"
                    )
                )
            ]
        )
        return _extract_json(response.content)
    except Exception as exc:  # pragma: no cover - network/provider failure
        log.warning("Raw JSON fallback failed: %s", exc)
        return None


def _extract_json(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None
