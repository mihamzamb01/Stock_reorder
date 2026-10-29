"""Tests for the LLM layer: provider wiring and the structured-output fallback chain.

No network access is required.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from llm import structured_call  # noqa: E402


class Answer(BaseModel):
    name: str
    score: int


class BrokenLLM:
    """Fails every structured-output method except json_mode."""

    def __init__(self, raw: str = '{"name": "ok", "score": 7}'):
        self.raw = raw
        self.attempts: list[str] = []

    def with_structured_output(self, schema, method=None, **kwargs):
        self.attempts.append(method)

        def _run(_messages):
            if method == "json_mode":
                return json.loads(self.raw)
            raise RuntimeError(f"{method} unsupported by this provider")

        return RunnableLambda(_run)


class RepairingLLM:
    """Returns invalid data first, valid data on the retry."""

    def __init__(self):
        self.calls = 0

    def with_structured_output(self, schema, method=None, **kwargs):
        def _run(_messages):
            self.calls += 1
            if self.calls == 1:
                return {"name": "bad"}  # missing required field -> validation error
            return Answer(name="good", score=3)

        return RunnableLambda(_run)


def test_build_llm_plumbing() -> None:
    """Every provider must be wired to the right endpoint and key."""
    import importlib

    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["NVIDIA_API_KEY"] = "nvapi-test"
    os.environ["LARP_ROUTER_API_KEY"] = "lrsk-test"
    os.environ["OPENROUTER_SITE_URL"] = "http://localhost:3000"
    os.environ["OPENROUTER_APP_NAME"] = "stock-reorder-agents"

    import config.settings as settings_module

    importlib.reload(settings_module)
    import llm as llm_module

    importlib.reload(llm_module)
    from config.settings import ModelSpec

    routing = settings_module.load_routing()
    assert routing, "the routing must not be empty"
    assert all(spec.base_url for spec in routing.values()), (
        "every role must resolve to an endpoint"
    )

    openrouter_spec = ModelSpec(
        role="x",
        provider="openrouter",
        model="google/gemini-3.5-flash-lite",
        base_url=settings_module.PROVIDER_ENDPOINTS["openrouter"],
        key_env="OPENROUTER_API_KEY",
    )
    openrouter = llm_module.build_llm(openrouter_spec)
    assert "openrouter.ai" in str(openrouter.openai_api_base), openrouter.openai_api_base
    assert openrouter.openai_api_key.get_secret_value() == "sk-or-test"

    nvidia_spec = ModelSpec(
        role="x",
        provider="nvidia",
        model="nvidia/nemotron-3-super-120b-a12b",
        base_url=settings_module.PROVIDER_ENDPOINTS["nvidia"],
        key_env="NVIDIA_API_KEY",
    )
    nvidia = llm_module.build_llm(nvidia_spec)
    assert "integrate.api.nvidia.com" in str(nvidia.openai_api_base), nvidia.openai_api_base
    assert nvidia.openai_api_key.get_secret_value() == "nvapi-test"

    larp_spec = ModelSpec(
        role="x",
        provider="larprouter",
        model="deepseek-v4.1-flash",
        base_url=settings_module.PROVIDER_ENDPOINTS["larprouter"],
        key_env="LARP_ROUTER_API_KEY",
    )
    larp = llm_module.build_llm(larp_spec)
    assert "larprouter.com/v1" in str(larp.openai_api_base), larp.openai_api_base
    assert larp.openai_api_key.get_secret_value() == "lrsk-test"

    print("  provider endpoints ok")


def test_structured_call_falls_back_through_methods() -> None:
    """A provider that rejects json_schema must still work via json_mode."""
    llm = BrokenLLM()
    messages = [SystemMessage(content="s"), HumanMessage(content="u")]
    result = structured_call(llm, Answer, messages)
    assert isinstance(result, Answer)
    assert result.name == "ok" and result.score == 7
    assert "json_schema" in llm.attempts
    print(f"  fallback chain ok ({llm.attempts})")


def test_structured_call_repairs_invalid_output() -> None:
    """A validation failure triggers one corrective retry."""
    llm = RepairingLLM()
    messages = [SystemMessage(content="s"), HumanMessage(content="u")]
    result = structured_call(llm, Answer, messages)
    assert result.name == "good"
    assert llm.calls == 2
    print(f"  repair path ok ({llm.calls} calls)")


def test_structured_call_raises_when_all_methods_fail() -> None:
    import logging

    logging.getLogger("llm").setLevel(logging.ERROR)  # silence the expected warning

    class DeadLLM:
        def with_structured_output(self, schema, method=None, **kwargs):
            def _run(_messages):
                raise RuntimeError("provider down")

            return RunnableLambda(_run)

    try:
        structured_call(DeadLLM(), Answer, [HumanMessage(content="u")])
    except RuntimeError as exc:
        assert "All structured-output methods failed" in str(exc)
        print("  hard failure reported cleanly")
        return
    raise AssertionError("expected a RuntimeError")


def test_reasoning_body_per_provider() -> None:
    """Each provider must receive its own reasoning vocabulary."""
    from config.settings import ModelSpec
    from llm import _reasoning_body

    openrouter = ModelSpec(
        role="x", provider="openrouter", model="m", reasoning_effort="medium"
    )
    assert _reasoning_body(openrouter) == {"reasoning": {"effort": "medium"}}

    nvidia = ModelSpec(role="x", provider="nvidia", model="m", reasoning_effort="medium")
    assert _reasoning_body(nvidia) == {"chat_template_kwargs": {"enable_thinking": True}}

    # An explicit raw body always wins.
    override = ModelSpec(
        role="x",
        provider="openrouter",
        model="m",
        reasoning_effort="medium",
        reasoning_body={"reasoning": {"max_tokens": 2048}},
    )
    assert _reasoning_body(override) == {"reasoning": {"max_tokens": 2048}}

    # No reasoning configured -> no body at all.
    plain = ModelSpec(role="x", provider="openrouter", model="m")
    assert _reasoning_body(plain) is None
    print("  reasoning bodies are provider-specific")


def test_env_routing_overrides(monkeypatch) -> None:
    """The documented env var names must actually change the routing.

    Both the short alias (MODEL_COST_NAME) and the full role name
    (MODEL_COST_OPTIMIZER_NAME) must be honoured.
    """
    import importlib

    import config.settings as settings_module

    importlib.reload(settings_module)

    # Short alias, exactly as written in .env.example
    monkeypatch.setenv("MODEL_COST_NAME", "some/other-model")
    monkeypatch.setenv("MODEL_COST_PROVIDER", "nvidia")
    monkeypatch.setenv("MODEL_COST_TEMPERATURE", "0.7")
    monkeypatch.setenv("MODEL_COST_MAX_TOKENS", "1234")
    monkeypatch.setenv("MODEL_COST_REASONING", "high")
    monkeypatch.setenv("MODEL_COST_REASONING_BODY", '{"reasoning": {"max_tokens": 2048}}')

    routing = settings_module.load_routing()
    cost = routing["cost_optimizer"]
    assert cost.model == "some/other-model", cost.model
    assert cost.provider == "nvidia", cost.provider
    assert cost.temperature == 0.7, cost.temperature
    assert cost.max_tokens == 1234, cost.max_tokens
    assert cost.reasoning_effort == "high", cost.reasoning_effort
    assert cost.reasoning_body == {"reasoning": {"max_tokens": 2048}}, cost.reasoning_body

    # Full role name also works
    monkeypatch.delenv("MODEL_COST_NAME")
    monkeypatch.setenv("MODEL_COST_OPTIMIZER_NAME", "full/name-model")
    routing = settings_module.load_routing()
    assert routing["cost_optimizer"].model == "full/name-model"

    # A blank value means "provider default", not "fall back to the spec"
    monkeypatch.setenv("MODEL_COST_OPTIMIZER_TEMPERATURE", "")
    monkeypatch.setenv("MODEL_COST_TEMPERATURE", "")
    routing = settings_module.load_routing()
    assert routing["cost_optimizer"].temperature is None

    print("  env overrides are honoured")


def test_environment_overrides_are_detected(monkeypatch) -> None:
    """A variable set in the terminal must be reported as overriding .env.

    This is the trap that makes a correct .env appear to be ignored: VS Code
    copies .env into the terminal when it starts, and load_dotenv() never
    replaces a variable that already exists.
    """
    import importlib

    import config.settings as settings_module

    importlib.reload(settings_module)

    # A value that differs from the file must be reported.
    monkeypatch.setenv("MODEL_COST_NAME", "some/stale-model")
    overridden = settings_module.overridden_by_environment(["MODEL_COST_NAME"])
    assert "MODEL_COST_NAME" in overridden, overridden

    # A value that matches the file is not a conflict.
    file_values = settings_module.env_file_values()
    if "MODEL_COST_NAME" in file_values:
        monkeypatch.setenv("MODEL_COST_NAME", file_values["MODEL_COST_NAME"])
        overridden = settings_module.overridden_by_environment(["MODEL_COST_NAME"])
        assert "MODEL_COST_NAME" not in overridden, overridden

    # A variable that is not set at all is not a conflict either.
    monkeypatch.delenv("MODEL_COST_NAME", raising=False)
    assert "MODEL_COST_NAME" not in settings_module.overridden_by_environment(
        ["MODEL_COST_NAME"]
    )
    print("  terminal environment overrides are detected")


if __name__ == "__main__":
    failures = 0
    for test in (
        test_build_llm_plumbing,
        test_reasoning_body_per_provider,
        test_env_routing_overrides,
        test_environment_overrides_are_detected,
        test_structured_call_falls_back_through_methods,
        test_structured_call_repairs_invalid_output,
        test_structured_call_raises_when_all_methods_fail,
    ):
        print(f"[RUN] {test.__name__}")
        try:
            test()
            print(f"[PASS] {test.__name__}")
        except Exception as exc:
            failures += 1
            import traceback

            traceback.print_exc()
            print(f"[FAIL] {test.__name__}: {exc}")
    sys.exit(1 if failures else 0)
