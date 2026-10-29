"""Agent card registry.

Loads agent card YAML files from ``agent_cards/`` and provides lookup by role.
Agent cards describe each agent's model, provider, capabilities, guardrails,
and alert thresholds - used by CI validation, observability, and dashboards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CARD_DIR = ROOT / "agent_cards"


@dataclass
class AgentCard:
    """A single agent card."""

    role: str
    name: str
    version: str
    status: str
    provider: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    description: str = ""
    capabilities: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    guardrails: dict[str, Any] = field(default_factory=dict)
    alerts: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, role: str, data: dict[str, Any]) -> AgentCard:
        agent = data.get("agent", {})
        return cls(
            role=role,
            name=agent.get("name", role),
            version=agent.get("version", "0.0.0"),
            status=agent.get("status", "unknown"),
            provider=agent.get("provider", "unknown"),
            model=agent.get("model", "unknown"),
            temperature=agent.get("temperature"),
            max_tokens=agent.get("max_tokens"),
            description=agent.get("description", ""),
            capabilities=agent.get("capabilities", []),
            inputs=agent.get("inputs", []),
            outputs=agent.get("outputs", []),
            guardrails=agent.get("guardrails", {}),
            alerts=agent.get("alerts", {}),
            cost=agent.get("cost", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "name": self.name,
            "version": self.version,
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "description": self.description,
            "capabilities": self.capabilities,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "guardrails": self.guardrails,
            "alerts": self.alerts,
            "cost": self.cost,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_registry: dict[str, AgentCard] | None = None


def load_cards(card_dir: Path | None = None) -> dict[str, AgentCard]:
    """Load all agent cards from ``card_dir`` (default: ``agent_cards/``)."""
    card_dir = card_dir or CARD_DIR
    cards: dict[str, AgentCard] = {}
    if not card_dir.exists():
        return cards
    for path in sorted(card_dir.glob("*.yaml")):
        role = path.stem
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        cards[role] = AgentCard.from_dict(role, data)
    return cards


def get_card(role: str) -> AgentCard | None:
    """Return the agent card for ``role``, or None if not found."""
    global _registry
    if _registry is None:
        _registry = load_cards()
    return _registry.get(role)


def list_roles() -> list[str]:
    """Return all known agent roles."""
    return sorted(get_card(r).role for r in (_registry or load_cards()))


def validate_card(card: AgentCard) -> list[str]:
    """Validate an agent card, returning a list of problem messages."""
    problems: list[str] = []
    if not card.role:
        problems.append("missing role")
    if not card.name:
        problems.append("missing name")
    if not card.version:
        problems.append("missing version")
    if not card.provider:
        problems.append("missing provider")
    if not card.model:
        problems.append("missing model")
    # SemVer check on version
    import re

    if card.version and not re.match(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$", card.version):
        problems.append(f"version {card.version!r} is not valid SemVer")
    return problems
