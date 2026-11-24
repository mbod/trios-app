

from pathlib import Path
from typing import Literal


import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, PrefixLoader, ChoiceLoader, TemplateNotFound
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from config import NAME_PATTERN

TASK_DIR = Path(__file__).resolve().parent / "tasks"
REQUIRED_ACTIONS = {"wait", "task_complete", "withdraw"}


DEFAULT_PROMPTS = {
    "system": "system.j2",
    "decide": "decide.j2",
    "turn": "turn.j2",
    "silence": "silence.j2",
    "describe": "describe.j2",
    "reflect": "reflect.j2"
}

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
    selection_visibility: Literal["private", "on_done", "live"] = "private"
    
    
class StimulusSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = ""
    framing: str = ""
    rounds: dict[str, list[str]]
    
    


class TaskDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    participants: list[str]
    actions: dict[str, str]
    prompts: dict[str, str] = {}
    materials_name: Literal["picture", "points table"] = "picture"
    materials: Literal["svg", "html"] = "html"
    answer: AnswerSpec
    rules: Rules = Field(default_factory=Rules)
    engine: Literal["choice"] = "choice"

    @model_validator(mode="after")
    def check_required(self):
        if missing := REQUIRED_ACTIONS - self.actions.keys():
            raise ValueError(f"task {self.name} is missing actions: {sorted(missing)}")

        return self


def make_turn_action(actions: dict[str,str], answer: "AnswerSpec"):
    """
    Build the TurnAction output schema from the task's actions and answer format
    """
    option_ids = tuple(answer.options)
    options_text = "; ".join(f"{k} = {label}" for k, label in answer.options.items())

    return create_model(
        "TurnAction",
        __doc__="What to do on this turn",
        current_action=(Literal[tuple(actions)], Field(description="the action to take")),
        message=(str, Field(default="", description=(
            "the chat message to send; it can accompany task_complete or withdraw"
            "Leave empty to send nothing"))),
        answer=(list[Literal[option_ids]] | None, Field(default=None, description=(
            f"only with task_complete: your group's answer, {answer.choose} different "
            f"options from: {options_text}"))),
    )



class Task:
    def __init__(self, name: str):
        if not NAME_PATTERN.match(name):
            raise ValueError(f"invalid task name '{name}'")
        self.dir = TASK_DIR / name
        self.defn = TaskDef(**yaml.safe_load((self.dir / "task.yaml").read_text()))
        shared = FileSystemLoader(TASK_DIR / "_shared")
        self.env = Environment(
            loader=ChoiceLoader([
                FileSystemLoader(self.dir),
                PrefixLoader({"shared": shared}),
                shared,
            ]),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True
        )
        self.TurnAction = make_turn_action(self.defn.actions, self.defn.answer)

        self.prompts = self.prompt_files()


    def render(self, prompt: str, **variables) -> str:
        if prompt not in self.prompts:
            raise ValueError(f"task {self.defn.name} has no '{prompt}' prompt")
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
        name = f"{participant}.{self.defn.materials}"
        if (self.defn.materials == "svg" and
                self.material_path(set_name, round_id, participant).with_suffix(".png").is_file()):
            name = f"{participant}.png"
        return f"/stimuli/{self.defn.name}/{set_name}/{round_id}/{name}"
        
        
    def score(self, answer: list[str] | None, correct: list[str]) -> dict:
        if not answer:
            return {"answered": False, "correct": False, "overlap": 0}
        return {
            "answered": True,
            "correct": set(answer) == set(correct),
            "overlap": len(set(answer) & set(correct))
        }
                                 

    def prompt_context(self, stimuli_name: str, participant: str) -> dict:
        """
        Variables for the instructions, the same for agents and human task page
        """
        return {
            "participant": participant,
            "participants": self.defn.participants,
            "others": [p for p in self.defn.participants if p!= participant],
            "framing": self.stimuli(stimuli_name).framing,
        }
            

    def prompt_files(self) -> dict[str, str]:
        """Prompt name -> template name, for the prompts this task has
        (in its own folder or in _shared)."""
        names = {**DEFAULT_PROMPTS, **self.defn.prompts}
        found = {}
        for prompt, template in names.items():
            try:
                self.env.get_template(template)
            except TemplateNotFound:
                if prompt in REQUIRED_PROMPTS or prompt in self.defn.prompts:
                    raise ValueError(f"task {self.defn.name}: prompt '{prompt}' "
                                     f"({template}) not found in the task or _shared")
                continue                      # optional and absent: fine
            found[prompt] = template
        return found
