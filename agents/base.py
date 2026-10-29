"""Shared plumbing for the agents: runtime container and audit trail helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from sqlalchemy import Engine

from config.settings import ModelSpec, Settings


@dataclass
class AgentRuntime:
    """Everything an agent node needs, injected at graph-build time."""

    engine: Engine
    llms: dict[str, ChatOpenAI]
    routing: dict[str, ModelSpec]
    settings: Settings
    # Optional observability recorder - one per pipeline run. When present,
    # every LLM call is logged as a trace step with the run's correlation ID.
    obs: Any = None

    def llm(self, role: str) -> ChatOpenAI:
        try:
            return self.llms[role]
        except KeyError as exc:  # pragma: no cover - configuration error
            raise KeyError(f"No model configured for role '{role}'") from exc

    def spec(self, role: str) -> ModelSpec:
        return self.routing[role]


def trace_message(summary: str) -> BaseMessage:
    """A short message recorded on the graph's message channel for tracing."""
    from langchain_core.messages import AIMessage

    return AIMessage(content=summary)


def as_messages(system_prompt: str, user_payload: Any) -> list[BaseMessage]:
    """Build a two-message prompt with the payload rendered as readable text."""
    from langchain_core.messages import HumanMessage, SystemMessage

    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=render_payload(user_payload)),
    ]


def render_payload(payload: Any) -> str:
    """Render a payload for a prompt: JSON for structured data, plain text otherwise."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, indent=2, default=str)
    return str(payload)
