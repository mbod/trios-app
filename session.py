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

import logging
import random
import threading
import time
from importlib import metadata

import requests

from config import load_condition
from tasks import Rules, Task


LOG = logging.getLogger(__name__)

AGENT_MANAGER_URL = "http://127.0.0.1:5556"
CHECK_EVERY_S = 0.5      # how often to check whether to end running round
AGENT_CONNECT_TIMEOUT_S = 15  # how long to wait for agents to join
KEY_PACKAGES = [
    "flask", "flask-socketio", "pydantic", "langchain",
    "langchain-openai", "langchain-anthropic", "langchain-google-genai",
    "langchain-ollama"
]

SESSIONS = {}


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
        self.rules = Rules(**{**self.task.defn.rules.model_dump(), **overrides})
        if set(self.condition.participants) != set(self.task.defn.participants):
            raise ValueError(f"condition {self.condition.name} has participants "
                             f"{sorted(self.condition.participants)}; task {task_name} "
                             f"needs {sorted(self.task.defn.participants)}")
        self.order = self._plan_rounds()

        
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
        if self.rules.order == "shuffle":
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
            "packages": {name: _version(name) for name in KEY_PACKAGES}
        })

        self.socketio.emit("session_start",
                           {"session_id": self.id,
                            "rounds": len(self.order)},
                           to=self.room)

        self.socketio.start_background_task(self._run)

        return event


    # --- running --------------------------

    def _run(self):
        reason = "completed"
        try:
            for number, round_id in enumerate(self.order, start=1):
                if self.end_round_requested:
                    break
                self._start_agents(round_id)
                self._run_round(number, round_id)
                self._stop_agents()
                if number < len(self.order) and not self.end_session_requested:
                    self.socketio.sleep(self.rules.pause_between_rounds)
            if self.end_round_requested:
                reason = "experimenter"

        except Exception as e:
            LOG.exception(f"session {self.id} failed")
            self.log.record(self.room, "session_error", payload={"error": f"{type(e).__name__}: {e}"})
            reason = "error"

        finally:
            self._stop_agents()
            self.log.end_session(self.room, payload={"reason": reason})
            self.socketio.emit("session_end", {"session_id": self.id,
                                               "reason": reason},
                               to=self.room)
            SESSIONS.pop(self.room, None)

            
    def _run_round(self, number: int, round_id: str):
        with self.lock:
            self.round_no = number
            self.status = {p: {"done": False, "answer": None} for p in self.condition.participants}
            self.messages_this_round = 0
            self.end_round_requested = None

        materials = {p: self.task.material_url(self.stimuli_name, round_id, p)
                     for p in self.condition.participants}

        self.log.set_round(self.room, number)
        self.log.record(self.room, "round_start",
                        payload={"round": number, "round_id": round_id,
                                 "materials": materials })

        self.socketio.emit("round_start", {
            "round": number, "of": len(self.order), "materials": materials,
            "time_limit": self.rules.time_limit,
            "clear_chat": self.rules.clear_chat_between_rounds
            },
            to=self.room)

        started = time.monotonic()
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
        if not all(s["done"] for s in self.status.values()):
            return False

        if self.rules.require_agreement:
            return len({frozenset(s["answer"] or []) for s in self.status.values()}) == 1

        return True


    def _time_to_warn(self, elapsed: float) -> bool:
        r = self.rules
        return (r.time_limit is not None and r.warning_before_end is not None
                and elapsed >= r.time_limit - r.warning_before_end)


    def _end_round(self, number: int, round_id: str, reason: str, elapsed: float):
        correct = self.stimuli.rounds[round_id]
        with self.lock:
            results = {p: {**s, **self.task.score(s["answer"], correct)}
                       for p, s in self.status.items()}
            answers = [s["answer"] for s in self.status.values()]
            agreed = all(answers) and len({rozenset(a) for a in answers}) == 1
            messages = self.messages_this_round
            self.round_no = None

        self.log.record(self.room, "round_end", payload={
            "round": number, "round_id": round_id, "reason": reason,
            "duration_s": round(elapsed, 1), "messages": messages,
            "correct_answer": correct, "results": results, "agreed": agreed
            })

        self.log.set_round(self.room, None)
        self.socketio.emit("round_end", {"round": number, "reason": reason},
                           to=self.room)


    # --- agents -----------------------

    def _start_agents(self, round_id: str):
        for agent_id in self.condition.agent_ids:
            r = requests.post(f"{AGENT_MANAGER_URL}/start", timeout=5, json={
                "agent_id": agent_id, "room_id": self.room,
                "condition": self.condition.name, "task": self.task.defn.name,
                "stimuli": self.stimuli_name, "round": round_id
            })
            r.raise_for_status()

        deadline = time.monotonic() + AGENT_CONNECT_TIMEOUT_S

        while time.monotonic() < deadline:
            present = {c["id"] for c in list(self.clients.values())
                       if c["room"] == self.room and c["kind"] == "agent"}

            if set(self.condition.agent_ids) <= present:
                return

        return RuntimeError(f"agents did not connect within {AGENT_CONNECT_TIMEOUT_S}s")
    

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

            if done:
                if answer is not None:
                    if (error := self._check_answer(answer)):
                        return error

                    elif self.rules.require_answer and kind == "human":
                        return "please choose an answer first"

            self.status[participant] == {"done": bool(done), "answer": answer if done else None}
            disagree = (self.rules.require_agreement
                        and all(s["done"] for s in self.status.values())
                        and not self._all_done())


        self.log.record(self.room, "participant_status",
                        sender=participant, kind=kind,
                        payload={"done": bool(done), "answer": answer if done else None})

        self.socketio.emit("participant_status", {"participant": participant, "done": bool(done)},
                           to=self.room)

        if disagree:
            self.log.record(self.room, "answers_differ")
            self.socketio.emit("answers_differ", {},
                               to=self.room)

        return None


    def _check_answer(self, answer) -> str | None:
        spec = self.task.defn.answer
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
