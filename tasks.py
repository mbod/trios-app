

from pathlib import Path
from typing import Literal, Any


import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, PrefixLoader, ChoiceLoader, TemplateNotFound
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from config import NAME_PATTERN
import maze

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

# common rules for any task that can be set in tasks/<task_name>/task.yaml
COMMON_RULES = {"order", "rounds", "pause_between_rounds", "clear_chat_between_rounds",
                "time_limit", "message_limit", "warning_before_end", "require_answer" }

# specific rules depending on the kind of engine used for task
ENGINE_RULES = {
    "choice":   COMMON_RULES | {"require_agreement", "selection_visibility"},
    "matching": COMMON_RULES | {"grid", "director", "trial_end" },
    "maze":     COMMON_RULES | {"decide", "proposals", "on_wall", "lives", "extra_moves",
                                "wall_reveal"}
}


# --- util functions


def unused_rules(engine: str, keys) -> list[str]:
    return sorted(set(keys) - ENGINE_RULES[engine])

# ----

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

    grid: tuple[int, int] = (3,3)
    director: str = "A"
    trial_end: Literal["all_done", "first_done"] = "all_done"

    proposals: Literal["single", "multiple"] = "single"
    
    # maze settings
    decide: Literal["single", "majority", "all"] = "single"
    on_wall: Literal["stay", "restart"] = "stay"
    lives: int | None = 3
    extra_moves: int = 4
    wall_reveal: Literal["none", "all"] = "none"
    
class StimulusSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = ""
    framing: str = ""
    rounds: dict[str, list[str]] = {}
    figures: list[str] = []
    mazes: dict[str, dict] = {}

class Placement(BaseModel):
    card: str = Field(description="the letter of one of your cards")
    slot: int | None = Field(description="the slot number to put it in, or null to take it back")

class TaskDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    participants: list[str]
    actions: dict[str, str]
    prompts: dict[str, str] = {}
    materials_name: Literal["picture", "points table"] = "picture"
    materials: Literal["svg", "html"] = "html"
    answer: AnswerSpec | None = None
    rules: Rules = Field(default_factory=Rules)
    engine: Literal["choice", "matching", "maze"] = "choice"
    variants: dict[str, dict[str, Any]] = {}

    
    @model_validator(mode="after")
    def check_required(self):
        if missing := REQUIRED_ACTIONS - self.actions.keys():
            raise ValueError(f"task {self.name} is missing actions: {sorted(missing)}")

        if self.engine == "choice" and self.answer is None:
            raise ValueError(f"task {self.name} needs an 'answer' section")

        if bad := unused_rules(self.engine, self.rules.model_fields_set):
            raise ValueError(f"task {self.name}: rules {bad} don't apply to the "
                             f"{self.engine} engine")
        for vname, vrules in self.variants.items():
            if not NAME_PATTERN.match(vname):
                raise ValueError(f"task {self.name}: invalid variant name '{vname}'")
            if bad := unused_rules(self.engine, vrules):
                raise ValueError(f"task {self.name}, variant {vname}: rules {bad} don't "
                                 f"apply to the {self.engine} engine")
            Rules(**{**self.rules.model_dump(), **vrules})      # type-check the values
        
        return self


def make_turn_action(actions: dict[str,str], answer: "AnswerSpec | None", placing: bool = False):
    """
    Build the TurnAction output schema from the task's actions and answer format
    """

    fields = {
        "current_action": (Literal[tuple(actions)], Field(description="the action to take")),
        "message": (str, Field(default="", description=(
            "the chat message to send; it can accompany task_complete or withdraw. "
            "Leave empty to send nothing"))),
    }

    

    if answer is not None:
        option_ids = tuple(answer.options)
        options_text = "; ".join(f"{k} = {label}" for k, label in answer.options.items())
        fields["answer"] = (list[Literal[option_ids]] | None, Field(default=None, description=(
            f"only with task_complete: your group's answer, {answer.choose} different "
            f"options from: {options_text}")))


    if placing:
        fields["places"] = (list[Placement], Field(default_factory=list, description=(
            "matchers only: cards to place in your grid this turn, in order"
            "Leave empty to place nothing")))
        
    return create_model(
        "TurnAction",
        __doc__="What to do on this turn",
        **fields
    )


def list_tasks() -> list[str]:
    return sorted(p.parent.name for p in TASK_DIR.glob("*/task.yaml")
                  if NAME_PATTERN.match(p.parent.name))


def task_catalogue() -> dict:
    """
    Each task's variants and stimulus sets, for the admin start form
    """
    out = {}
    for name in list_tasks():
        try:
            data = yaml.safe_load((TASK_DIR / name / "task.yaml").read_text()) or {}
            data.setdefault("name", name)
            defn = TaskDef(**data)
        except Exception as e:
            out[name] = {"error": str(e)[:300], "variants": {}, "stimuli": []}
            continue

        stimuli = sorted(p.parent.name for p in (TASK_DIR / name / "stimuli").glob("*/stimuli.yaml"))
        out[name] = {"variants": defn.variants, "stimuli": stimuli}
    return out
                         
                                  



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
        self.TurnAction = make_turn_action(self.defn.actions, self.defn.answer,
                                           placing=self.defn.engine == "matching")

        self.prompts = self.prompt_files()


    def render(self, prompt: str, **variables) -> str:
        if prompt not in self.prompts:
            raise ValueError(f"task {self.defn.name} has no '{prompt}' prompt")
        template = self.env.get_template(self.prompts[prompt])
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

            
            # if task uses maze engine get that maze definition is valid
            if self.defn.engine == "maze":
                for mid, spec in stim.mazes.items():
                    if not NAME_PATTERN.match(mid):
                        raise ValueError(f"{set_name}: invalid maze name '{mid}'")
                    try:
                        maze.check(spec, self.defn.participants)
                    except ValueError as e:
                        raise ValueError(f"{set_name}, {mid}: {e}") from None
                return stim


            
            # if task uses a matching engine check correct number of figures
            if self.defn.engine == "matching":
                if len(stim.figures) < 2 or len(set(stim.figures)) != len(stim.figures):
                    raise ValueError(f"{set_name}: 'figures' must list at least 2 different figures")
                
                # check that listed figures exist
                for fig in stim.figures:
                    path = folder / f"{fig}.{self.defn.materials}"
                    if not NAME_PATTERN.match(fig) or not (path.is_file() or path.with_suffix(".png").is_file()):
                        raise ValueError(f"{set_name}: bad name or missing file for figure '{fig}'")

                return stim
                    
                    
            
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
