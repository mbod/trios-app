"""
Condition files: specify who is in the group and how each agent behaves.

A condition file is placed in `config/conditions/<name>.yaml`.

Each participant is either `human` or an agent model and any settings that differ from the defaults are added.
"""

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_DIR = Path(__file__).resolve().parent / "config"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROVIDERS = {"openai", "anthropic", "google_genai", "ollama"}
Range = type[float, float]


class AgentSettings(BaseModel):
    """
    Setting for one agent

    These are the default settings for any agent. Condition files can
    override any of these settings.
    Changing a default changes every condition that relies on it,
    including one already run. The session log always records the
    values actually used.
    """

    model_config = ConfigDict(extra="forbid")

    # model
    model: str                              # "provider:name", e.g. "openai:gpt-5-nano"
    model_settings: dict[str, Any] = {}     # passed to provider, e.g. reasoning mode
    base_url: str | None = None             # for local or OpenAI compatible models
    temperature: float | None = None

    # Timing
    think_time: Range = (1.0, 3.0)          # pause before deciding whether to speak
    cooldown_after_reply: Range = (2,8)     # quiet period after speaking
    cooldown_after_silence: Range = (6, 14) # quiet period after breaking a silence

    # Breaking silences
    silence_wait: float = 6                 # how long the initial room silence should be
    silence_check_every: Range = (4,8)
    silence_chance: float = Field(0.25, ge=0, le=1)


    # Messages
    max_words: int | None = None            # sent to model to restrict output


    @field_validator("model")
    @classmethod
    def check_model(cls, value: str) -> str:
        provider, sep, name = value.partition(":")
        if not sep or not name or provider not in PROVIDERS:
            raise ValueError(f"model must be 'provider:name' with provider name of {sorted(PROVIDERS)}. Provided value {value}")
        return value


    @property
    def provider(self) -> str:
        return self.model.partition(":")[0]



class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    seed: int | None = None
    participants: dict[str, Literal["human"] | AgentSettings]
    
    
    @property
    def agent_ids(self) -> list[str]:
        return [part for part, kind in self.participants.items() if kind != "human"]

    @property
    def human_ids(self) -> list[str]:
        return [part for part, kind in self.participants.items() if kind == "human"]

    def agent(self, participant: str) -> AgentSettings:
        settings = self.participants.get(participant)
        if not isinstance(settings, AgentSettings):
            raise ValueError(f"{participant} is not an agent in condition {self.name}")
        return settings


def load_condition(name: str) -> Condition:
    if not NAME_PATTERN.match(name):
        raise ValueError(f"invalid condition name '{name}'")
    path = CONFIG_DIR / "conditions" / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"no condition file at {path}")
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    return Condition(name=name, **data)

