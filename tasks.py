

from pathlib import Path
from typing import Literal


import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from config import NAME_PATTERN

TASK_DIR = Path(__file__).resolve().parent / "tasks"
REQUIRED_ACTIONS = {"wait", "task_complete"}
REQUIRED_PROMPTS = {"system", "decide", "turn", "silence"}


class AnswerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choose: int = Field(ge=1)
    options: dict[str, str]

class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: Literal["fixed", "shuffled"] = "fixed"
    rounds: int | None = None
    pause_between_rounds: float = 10
    clear_chat_between_rounds: bool = False
    time_limit: float | None = 600
    message_limit: int | None = None
    warning_before_end: float | None = 60
    require_answer: bool = True
    require_agreement: bool = False

    
class StimulusSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = ""
    rounds: dict[str, list[str]]
    
    


class TaskDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    participants: list[str]
    actions: dict[str, str]
    prompts: dict[str, str]
    materials: str = "svg"
    answer: AnswerSpec
    rules: Rules = Field(default_factory=Rules)
    

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
            "the chat message to send, or empty string if waiting or task_complete"))),
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

    

    def stimuli(self, set_name: str) -> StimulusSet:
        """
        Load a stimulus set and check it for completeness
        """
        if not NAME_PATTERN.match(set_name):
            raise ValueError(f"invalid stimulus set name '{set_name}'")
        folder = self.dir / "stimuli" / set_name
        path = folder / "stimuli.yaml"
        if not path.is_file():
            raise ValueError(f"no stimulus set at {folder}")

        with open(path) as f:
            stim = StimulusSet(**(yaml.safe_load(f) or {}))

        spec = self.defn.answer
        for round_id, answer in stim.rounds.items():
            if not NAME_PATTERN.match(round_id):
                raise ValueError(f"{set_name}: invalid round name '{round_id}'")

            if len(set(answer)) != spec.choose or not set(answer) <= spec.options.keys():
                raise ValueError(f"{set_name}, round {round_id}: the answer must be "
                                 f"{spec.choose} different options from {sorted(spec.options)}")

            for p in self.defn.participants:
                if not self.material_path(set_name, round_id, p).is_file():
                    raise ValueError(f"{set_name}, round {round_id}: missing file for {p}"
                                     f"({self.material_path(set_name, round_id, p)})")

        return stim


    def material_path(self, set_name: str, round_id: str, participant: str) -> Path:
        return self.dir / "stimuli" / set_name / round_id / f"{participant}.{self.defn.materials}"


    def material_url(self, set_name: str, round_id: str, participant: str) -> str:
        return f"/stimuli/{self.defn.name}/{set_name}/{round_id}/{participant}.{self.defn.materials}"

    def score(self, answer: list[str] | None, correct: list[str]) -> dict:
        if not answer:
            return {"answered": False, "correct": False, "overlap": 0}
        return {
            "answered": True,
            "correct": set(answer) == set(correct),
            "overlap": len(set(answer) & set(correct))
        }
                                 
