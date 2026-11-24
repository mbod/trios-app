"""
Running a session:
   - rounds
   - limits
   - done signals
   - answers
   - scores

One session per room

/start_session creates it and then runs in the background and records everything to event log

"""

import os
import logging
import random
import threading
import time
from importlib import metadata
import subprocess
from pathlib import Path
import requests

import hashlib

from engine import ENGINES
from config import load_condition
from tasks import Rules, Task


LOG = logging.getLogger(__name__)

AGENT_MANAGER_URL = os.environ.get("AGENT_MANAGER_URL", "http://127.0.0.1:5556")
CHECK_EVERY_S = 0.5      # how often to check whether to end running round
AGENT_CONNECT_TIMEOUT_S = 15  # how long to wait for agents to join
KEY_PACKAGES = [
    "flask", "flask-socketio", "pydantic", "langchain",
    "langchain-openai", "langchain-anthropic", "langchain-google-genai",
    "langchain-ollama"
]



SESSIONS = {}

TASK_DIR = Path(__file__).resolve().parent / "tasks"


class Session:

    def __init__(self, socketio, event_log, clients, room, session_id,
                 condition_name, task_name, stimuli_name, overrides, meta):

        self.socketio = socketio
        self.log = event_log
        self.clients = clients
        self.room = room
        self.id = session_id
        self.meta = meta

        self.condition = load_condition(condition_name)
        self.task = Task(task_name)
        self.stimuli_name = stimuli_name
        self.stimuli = self.task.stimuli(stimuli_name)

        self.rules = Rules(**{**self.task.defn.rules.model_dump(),
                              **self.condition.rules,
                              **overrides})


        self.engine = ENGINES[self.task.defn.engine](self.task, stimuli_name, self.stimuli, self.rules)
        seed = self.condition.seed
        self.order = self.engine.plan_rounds(
            random.Random(f"{seed}-{self.id}" if seed is not None else None))
            
        
        if set(self.condition.participants) != set(self.task.defn.participants):
            raise ValueError(f"condition {self.condition.name} has participants "
                             f"{sorted(self.condition.participants)}; task {task_name} "
                             f"needs {sorted(self.task.defn.participants)}")
    

        
        # state of current round
        self.lock = threading.Lock()
        self.round_no = None
        self.status = {}
        self.messages_this_round = 0
        self.end_round_requested = None
        self.end_session_requested = False

    # --- setting up -----------

    def _plan_rounds(self) -> list[str]:
        order = list(self.stimuli.rounds)
        if self.rules.order == "shuffled":
            seed = self.condition.seed
            random.Random(f"{seed}-{self.id}" if seed is not None else None).shuffle(order)

        if self.rules.rounds is not None:
            if self.rules.rounds > len(order):
                raise ValueError(f"{self.rules.rounds} rounds requested; "
                                 f"{self.stimuli_name} has {len(order)}")

            order = order[:self.rules.rounds]

        return order

    def missing_humans(self) -> list[str]:
        present = {c["id"] for c in list(self.clients.values())
                   if c["room"] == self.room and c["kind"] == "human"}

        return [h for h in self.condition.human_ids if h not in present]


    def start(self):
        event = self.log.start_session(self.room, self.id, {
            **self.meta,
            "condition": self.condition.model_dump(),
            "task": self.task.defn.name,
            "stimuli": self.stimuli_name,
            "rules": self.rules.model_dump(),
            "round_order": self.order,
            "packages": {name: _version(name) for name in KEY_PACKAGES},
            "templates": {
                name: {"file": str(Path(t.filename).relative_to(TASK_DIR)),
                       "sha": hashlib.sha256(Path(t.filename).read_bytes()).hexdigest()[:12]}
                       for name, t in ((n, self.task.env.get_template(f))
                                       for n, f in self.task.prompt_files().items())
            },
            "code": code_version()
        })

        self.socketio.emit("session_start",
                           {"session_id": self.id,
                            "rounds": len(self.order),
                            "answer_format": self.task.defn.answer.model_dump() if self.task.defn.answer else None,
                            "participants": list(self.condition.participants),
                            "code": code_version()
                           }, to=self.room)

        self.socketio.start_background_task(self._run)

        return event


    # --- running --------------------------

    def _run(self):
        reason = "completed"
        try:
            self._start_agents()
            for number, round_id in enumerate(self.order, start=1):
                if self.end_session_requested:
                    break
                self._run_round(number, round_id)
                if number < len(self.order) and not self.end_session_requested:
                    self.socketio.sleep(self.rules.pause_between_rounds)
            if self.end_session_requested:
                reason = "experimenter"

        except Exception as e:
            LOG.exception(f"session {self.id} failed")
            self.log.record(self.room, "session_error", payload={"error": f"{type(e).__name__}: {e}"})
            reason = "error"

        finally:
            self._stop_agents()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and any(
                    c["room"] == self.room and c["kind"] == "agent"
                    for c in list(self.clients.values())):
                self.socketio.sleep(0.2)

            
                
            self.log.end_session(self.room, payload={"reason": reason})
            self.socketio.emit("session_end", {"session_id": self.id,
                                               "reason": reason},
                               to=self.room)
            SESSIONS.pop(self.room, None)

            
    def _run_round(self, number: int, round_id: str):

        seed = self.condition.seed
        rng = random.Random(f"{seed}-{self.id}-{number}" if seed is not None else None)
        self.engine.setup_round(number, round_id, list(self.condition.participants), rng)
        materials = self.engine.materials()
        payload = {"round": number, "round_id": round_id, "materials": materials,
                   **self.engine.setup_log()}
        

        
        with self.lock:
            self.round_no = number
            self.status = {p: {"done": False, "answer": None} for p in self.condition.participants}
            self.selection = {p: [] for p in self.condition.participants}
            self.messages_this_round = 0
            self.end_round_requested = None
            self.current_materials = materials
            self.round_started_at = time.monotonic()
            self.calls = {}

        self.log.set_round(self.room, number)
        event = self.log.record(self.room, "round_start",
                        payload={"round": number, "round_id": round_id,
                                 "materials": materials })

        self.socketio.emit("round_start", {
            "round": number, "of": len(self.order), "round_id": round_id,
            "stimuli": self.stimuli_name, "materials": materials,
            "participants": list(self.condition.participants),
            "time_limit": self.rules.time_limit,
            "clear_chat": self.rules.clear_chat_between_rounds,
            "seq": event["seq"]
            },
            to=self.room)

        for p in self.condition.participants:
            self._send_view(p, self.engine.view(p))

        started =  self.round_started_at
        warned = False
        while True:
            self.socketio.sleep(CHECK_EVERY_S)
            elapsed = time.monotonic() - started
            reason = self._end_reason(elapsed)

            if reason:       # if round has ended
                break

            if not warned and self._time_to_warn(elapsed):
                warned = True
                seconds_left = round(self.rules.time_limit - elapsed)
                self.log.record(self.room, "round_warning",
                                payload={"seconds_left": seconds_left})
                self.socketio.emit("round_warning",
                                   {"seconds_left": seconds_left}, to=self.room)

        self._end_round(number, round_id, reason, elapsed)


    def _end_reason(self, elapsed: float) -> str | None:
        with self.lock:
            if self.end_round_requested:
                return self.end_round_requested

            if self._all_done():
                return "all_done"

            if self.rules.time_limit is not None and elapsed >= self.rules.time_limit:
                return "time_limit"

            if (self.rules.message_limit is not None
                and self.messages_this_round >= self.rules.message_limit):
                return "message_limit"

        return None


    def _all_done(self) -> bool:
            return self.engine.round_complete(self.status)
        


    def _time_to_warn(self, elapsed: float) -> bool:
        r = self.rules
        return (r.time_limit is not None and r.warning_before_end is not None
                and elapsed >= r.time_limit - r.warning_before_end)


    def _end_round(self, number: int, round_id: str, reason: str, elapsed: float):
        correct = self.engine.correct()
        with self.lock:
            scores = self.engine.score(self.status)
            results = {p: {**s, "selection": self.selection[p], **scores[p]}
                          for p, s in self.status.items()}

            answers = [s["answer"] for s in self.status.values()]
            agreed = all(answers) and len({frozenset(a) for a in answers}) == 1
            messages = self.messages_this_round

            calls = {a: dict(c) for a, c in self.calls.items()}
            
            self.round_no = None

        self.log.record(self.room, "round_end", payload={
            "round": number, "round_id": round_id, "reason": reason,
            "duration_s": round(elapsed, 1), "messages": messages,
            "correct_answer": correct, "results": results, "agreed": agreed,
            "calls": calls
            })

        self.log.set_round(self.room, None)
        self.socketio.emit("round_end", {"round": number, "reason": reason},
                           to=self.room)

    def round_open(self) -> bool:
        with self.lock:
            return self.round_no is not None


    def on_action(self, participant: str, kind: str, action) -> str | None:
        """
        A participant's task action (e.g. placing a card)
        Returns error message for the participant or None
        """
        with self.lock:
            if self.round_no is None:
                return "no round in progress"
            if participant not in self.status:
                return "not a participant in this session"
            if not isinstance(action, dict):
                return "invalid action"
            try:
                result = self.engine.apply(participant, action)
            except ValueError as e:
                return str(e)
            withdrew = self.status[participant]["done"]
            if withdrew:
                self.status[participant] = {"done": False, "answer": None}
            view = self.engine.view(participant)

        self.log.record(self.room, "task_action",
                        sender=participant,
                        kind=kind,
                        payload=result)

        if withdrew:
            self.log.record(self.room, "participant_status",
                            sender=participant,
                            kind=kind,
                            payload={"done": False, "answer": None, "cause": "board_change"})
            self.socketio.emit("participant_status",
                               {"participant": participant, "done": False}, to=self.room)

        self._send_view(participant, view)
        return None


    def _send_view(self, participant: str, view: dict):
        """
        A participant's private view: to them and to observers only
        """
        if not view:
            return

        for sid, c in list(self.clients.items()):
            if c["room"] == self.room and (c["id"] == participant or c["kind"] == "observer"):
                self.socketio.emit("task_view", view, to=sid)
                
                                                
            
    # --- agents -----------------------

    def _start_agents(self):
        for agent_id in self.condition.agent_ids:
            r = requests.post(f"{AGENT_MANAGER_URL}/start", timeout=5, json={
                "agent_id": agent_id, "room_id": self.room,
                "condition": self.condition.name, "task": self.task.defn.name,
            })
            r.raise_for_status()

        deadline = time.monotonic() + AGENT_CONNECT_TIMEOUT_S

        while time.monotonic() < deadline:
            present = {c["id"] for c in list(self.clients.values())
                       if c["room"] == self.room and c["kind"] == "agent"}

            if set(self.condition.agent_ids) <= present:
                return
            self.socketio.sleep(0.2)
        raise RuntimeError(f"agents did not connect within {AGENT_CONNECT_TIMEOUT_S}s")
    

    def _stop_agents(self):
        for agent_id in self.condition.agent_ids:
            try:
                requests.post(f"{AGENT_MANAGER_URL}/stop", timeout=10,
                              json={"agent_id": agent_id, "room_id": self.room})

            except requests.RequestException:
                pass


    # ---- called by the socket handlers and admin routers

    def on_message(self):
        with self.lock:
            if self.round_no is not None:
                self.messages_this_round += 1


                
    def on_status(self, participant: str, kind: str, done: bool, answer) -> str | None:
        """
        Participant signals done (with an answer) or withdraws
        Returns an error message for the participant or None
        """
        with self.lock:
            if self.round_no is None:
                return "no round in progress"
            if participant not in self.status:
                return "not a participant in this session"

            if done and answer is None:
                answer = self.selection[participant] or None
            
            if done:
                if answer is None:
                    if self.rules.require_answer:
                        return "please choose an answer first"
    
                elif (error := self._check_answer(answer)):
                    return error

                else:
                    answer = sorted(answer)

            new = {"done": bool(done), "answer": answer if done else None}
            if self.status[participant] == new:
                return None
            self.status[participant] = new

            disagree = (self.rules.require_agreement
                        and all(s["done"] for s in self.status.values())
                        and not self._all_done())

        self.log.record(self.room, "participant_status",
                        sender=participant, kind=kind,
                        payload={"done": bool(done), "answer": answer if done else None})

        status = {"participant": participant, "done": new["done"]}
        if self.rules.selection_visibility != "private":
            status["answer"] = new["answer"]
        
        self.socketio.emit("participant_status", status, to=self.room)

        if disagree:
            self.log.record(self.room, "answers_differ")
            self.socketio.emit("answers_differ", {},
                               to=self.room)

        return None


    def on_selection(self, participant: str, kind: str, selection) -> str | None:
        """A participant changes their current selection (a partial answer is fine).
        Returns an error message for the participant, or None."""
        spec = self.task.defn.answer
        if spec is None:
            return "this task has no answer option"
        
        with self.lock:
            if self.round_no is None:
                return "no round in progress"
            if participant not in self.selection:
                return "not a participant in this session"
            if (not isinstance(selection, list) or len(set(selection)) > spec.choose
                    or not set(selection) <= spec.options.keys()):
                return f"choose up to {spec.choose} different options"
            selection = sorted(set(selection))
            if selection == self.selection[participant]:
                return None                           # nothing changed
            self.selection[participant] = selection
            st = self.status[participant]
            withdrew = st["done"] and st["answer"] != selection
            if withdrew:                              # changing your mind undoes "done"
                self.status[participant] = {"done": False, "answer": None}

        self.log.record(self.room, "selection", sender=participant, kind=kind,
                        payload={"selection": selection})
        self._emit_selection(participant, selection)
        if withdrew:
            self.log.record(self.room, "participant_status", sender=participant,
                            kind=kind, payload={"done": False, "answer": None,
                                                "cause": "selection_changed"})
            self.socketio.emit("participant_status",
                               {"participant": participant, "done": False}, to=self.room)
        return None


            
    
    def _emit_selection(self, participant: str, selection: list):
        """Send a selection change to whoever may see it: everyone with 'live',
        otherwise only the participant themselves and observers."""
        payload = {"participant": participant, "selection": selection}
        if self.rules.selection_visibility == "live":
            self.socketio.emit("selection", payload, to=self.room)
            return
        for sid, c in list(self.clients.items()):
            if c["room"] == self.room and (c["id"] == participant or c["kind"] == "observer"):
                self.socketio.emit("selection", payload, to=sid)

    

    def public_state(self, participant: str | None = None) -> dict:
        """
        What a page needs when it connects
        (doesn't include answers)
        """
        with self.lock:
            state = {
                "session_id": self.id,
                "rounds": len(self.order),
                "answer_format": self.task.defn.answer.model_dump() if self.task.defn.answer else None,
                "participants": list(self.condition.participants),
                "round": self.round_no,
                "status": {p: s["done"] for p, s in self.status.items()}
            }
            if self.round_no is not None:
                state["materials"] = self.current_materials
                state["seconds_left"] = (
                    None if self.rules.time_limit is None else
                    max(0, round(self.rules.time_limit - (time.monotonic() - self.round_started_at))))

            if participant:
                state["view"] = self.engine.view(participant)
                
        return state
     
    

    def _check_answer(self, answer) -> str | None:
        spec = self.task.defn.answer
        if spec is None:
            return "this task has no answer option"

        if (not isinstance(answer, list) or len(set(answer)) != spec.choose
            or not set(answer) <= spec.options.keys()):
            return f"choose {spec.choose} different options"
        return None

    def request_end_round(self, reason: str = "experimenter"):
        with self.lock:
            self.end_round_requested = reason


    def request_end_session(self):
        with self.lock:
            self.end_session_requested = True
            self.end_round_requested = "experimenter"


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None

def code_version() -> dict:
    """The git commit the server is running: : from the image (Docker) or from git (development)."""

    if commit := os.environ.get("GIT_COMMIT"):
        return {"commit": commit, "uncommitted_changes": []}
                
    
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()
    try:
        changed = git("status", "--porcelain").splitlines()
        return {"commit": git("rev-parse", "HEAD"),
                "uncommitted_changes": changed}
    
    except FileNotFoundError:
        return {"commit": None, "uncommitted_changes": None}
