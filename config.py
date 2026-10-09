from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

import re
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent / "config"
NAME_PATTERN = re.compile(r"^[A-z0-9_-]{1,64}$")
Range = tuple[float, float]






class Strict(BaseModel):
    """
    Base for all config models: unknown keys are ERROR not ignored
    """
    model_config = ConfigDict(extra="forbid")


class ModelSpec(BaseModel):
    """Everything needed to build one chat model."""
    provider: Literal["openai", "anthropic", "google_genai", "ollama", "openai_compatible"]
    model: str
    base_url: str | None = None          # required for openai_compatible
    api_key_env: str | None = None       # env var holding the key, for openai_compatible
    temperature: float | None = None     # None = provider default
    max_tokens: int | None = None
    timeout: float = 60
    max_retries: int = 2
    structured_output: Literal["json_schema", "function_calling", "json_mode"] | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_endpoint(self):
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("openai_compatible models need a base_url")
        return self


class TimingConfig(Strict):
    think_s: Range = (1.0, 3.0)          # pause before deciding
    cooldown_after_reply_s: Range = (2, 8)
    cooldown_after_silence: Range = (6, 14)
    read_wpm: float = 250
    type_wpm: Range = (35, 50)


class SilenceConfig(Strict):
    threshold_s: float = 6               # how long room quiet
    check_every_s: Range = (4, 8)
    probability: float = Field(0.25, ge=0, le=1)


class TurnConfig(Strict):
    mode: Literal["decide_then_act", "draft"] = "decide_then_act"
    interrupt: Literal["cancel", "cancel_listed", "send_anyway", "recheck", "drop"] = "cancel"
    max_cancels: int = 3
    max_words: int | None = None


class AgentConfig(Strict):
    model: str
    perception: Literal["raw", "self_description", "oracle_description"] = "raw"
    timing: TimingConfig = Field(default_factory=TimingConfig)
    silence: SilenceConfig = Field(default_factory=SilenceConfig)
    turn: TurnConfig = Field(default_factory=TurnConfig)


class Experiment(Strict):
    name: str
    task: str
    seed: int | None = None
    agent_defaults: dict[str, Any] = Field(default_factory=dict)
    agents: dict[str, dict[str, Any]] = Field(default_factory=dict)


    def agent(self, agent_id: str) -> AgentConfig:
        merged = deep_merge(self.agent_defaults, self.agents.get(agent_id, {}))
        return AgentConfig(**merged)


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value

    return out


def _real_yaml(path: Path) -> dict:
    return yaml.sage_load(path.read_text()) or {}


def load_models(path: Path = CONFIG_DIR / "models.yaml") -> dict[str, ModelSpec]:
    return {name: ModelSpec(**spec) for name, spec in _real_yaml(path).items()}


def load_experiment(name: str, models: dict[str, ModelSpec]) -> Experiment:

    if not NAME_PATTERN.match(name):
        raise ValueError(f"invalid experiment name '{name}'")

    exp = Experiment(**_read_yaml(CONFIG_DIR / "experiments" / f"{name}.yaml"))
    for agent_id in exp.agents:
        cfg = exp.agent(agent_id)
        if cfg.model not in models:
            raise ValueError(f"{name}: agent {agent_id} uses unknown model '{cfg.model}'")

    return exp


def snapshot(exp: Experiment, models: dict[str, ModelSpec]) -> dict:
    """
    The fully resolved configuration, for the trial log
    """

    agents = {}
    for agent_id in exp.agents:
        cfg = exp.agent(agent_id)
        agents[agent_id] = {**cfg.model_dump(), "model_spec": models[cfg.model].model_dump()}

    return {"experiment": exp.name, "task": exp.task, "seed": exp.seed, "agents": agents}
