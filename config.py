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

class ModelsFile(Strict):
    provider_defaults: dict[str, dict[str, Any]] = Field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)

class ModelRegistry:
    """
    Resolves a profile name or a 'provider:model' reference to a ModelSpec
    """

    def __init__(self, path: Path = CONFIG_DIR / "models.yaml"):
        f = ModelsFile(**_read_yaml(path))
        self.defaults = f.provider_defaults
        self.profiles = {name: self._build(spec) for name, spec in f.profiles.items()}

    def _build(self, spec: dict) -> ModelSpec:
        base = self.defaults.get(spec.get("provider"), {})
        return ModelSpec(**deep_merge(base, spec))

    def resolve(self, ref: str) -> ModelSpec:
        if ref in self.profiles:
            return self.profiles[ref]

        provider, sep, model = ref.partition(":")
        if not sep or not model:
            raise ValueError(f"unknown model '{ref}': use a provider name or 'provider:model'")
        return self._build({"provider": provider, "model": model})
    

class TimingConfig(Strict):
    think_s: Range = (1.0, 3.0)          # pause before deciding
    cooldown_after_reply_s: Range = (2, 8)
    cooldown_after_silence_s: Range = (6, 14)
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


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}



def load_experiment(name: str, registry: ModelRegistry) -> Experiment:

    if not NAME_PATTERN.match(name):
        raise ValueError(f"invalid experiment name '{name}'")

    exp = Experiment(**_read_yaml(CONFIG_DIR / "experiments" / f"{name}.yaml"))
    
    for agent_id in exp.agents:

        registry.resolve(exp.agent(agent_id).model)
        
    return exp


def snapshot(exp: Experiment, registry: ModelRegistry) -> dict:
    """
    The fully resolved configuration, for the trial log
    """

    agents = {}
    for agent_id in exp.agents:
        cfg = exp.agent(agent_id)
        agents[agent_id] = {**cfg.model_dump(),
                            "model_spec": registry.resolve(cfg.model).model_dump()}

    return {"experiment": exp.name, "task": exp.task, "seed": exp.seed, "agents": agents}
