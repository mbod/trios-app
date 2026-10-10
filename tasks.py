

from pathlib import Path
from typing import Literal


import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from config import NAME_PATTERN

TASK_DIR = Path(__file__).resolve().parent / "tasks"
REQUIRED_ACTIONS = {"wait", "task_complete"}
REQUIRED_PROMPTS = {"system", "decide", "turn", "silence"}


class TaskDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    participants: list[str]
    actions: dict[str, str]
    prompts: dict[str, str]


    @model_validator(mode="after")
    def check_required(self):
        if missing := REQUIRED_ACTIONS - self.actions.keys():
            raise ValueError(f"task {self.name} is missing actions: {sorted(missing)}")
        if missing := REQUIRED_PROMPTS - self.prompts.keys():
            raise ValueError(f"task {self.name} is missing prompts: {sorted(missing)}")
        return self


def make_turn_action(actions: dict[str,str]):
    """
    Build the TurnAction output schema from the task's actions
    """
    return create_model(
        "TurnAction",
        __doc__="What to do on this turn",
        current_action=(Literal[tuple(actions)], Field(description="the action to take")),
        message=(str, Field(default="", description=(
            "the that message to send, or empty string if waiting or task_complete"))),
    )



class Task:
    def __init__(self, name: str):
        if not NAME_PATTERN.match(name):
            raise ValueError(f"invalid task name '{name}'")
        self.dir = TASK_DIR / name
        self.defn = TaskDef(**yaml.safe_load((self.dir / "task.yaml").read_text()))
        self.env = Environment(
            loader=FileSystemLoader(self.dir),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True
        )
        self.TurnAction = make_turn_action(self.defn.actions)
        for template in self.defn.prompts.values():
            self.env.get_template(template)


    def render(self, prompt: str, **variables) -> str:
        template = self.env.get_template(self.defn.prompts[prompt])
        return template.render(task=self.defn, **variables).strip()

    
