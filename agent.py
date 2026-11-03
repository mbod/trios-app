"""An LLM participant in a session.

Two parts:
  - handlers (register_handlers) record what happens in the room, then queue a trigger;
  - one loop (act_loop) takes at most one turn at a time, using the turn-taking
    strategy named in the agent's settings (see strategies.py).
"""

import asyncio
import hashlib
import logging
import random
import time
from dataclasses import dataclass

import socketio
from pydantic import BaseModel, Field

from config import AgentSettings
from llm import build_chat_model, structured
from tasks import Task

LOG = logging.getLogger(__name__)


# Model calling parameters
CALL_TIMEOUT_S = 60
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 1.0
RETRY_BUDGET_S = 20



class SpeakDecision(BaseModel):
    """
    Decide whether to speak next in the group chat
    """
    speak: bool = Field(description="true if you should send a message now")
    reason: str = Field(description="brief reason for the decision")



@dataclass
class Trigger:
    """
    What action or event prompted a turn?
    """
    kind: str             # "message" | "silence" | "round_start" | "status"
    sender: str | None = None
    text: str = ""


class Agent:
    def __init__(self, id: str, room: str, ws_url: str, socketio_path: str,
                 task: Task, settings: AgentSettings, seed: int | None = None):

        from strategies import STRATEGIES
        if settings.turn_taking not in STRATEGIES:
            raise ValueError(f"unknown turn_taking '{settings.turn_taking}'; "
                             f"choose from {sorted(STRATEGIES)}")

        self.strategy = STRATEGIES[settings.turn_taking]

        self.id = id
        self.room = room
        self.ws_url = ws_url
        self.socketio_path = socketio_path
        self.task = task
        self.settings = settings
        self.rng = random.Random(f"{seed}-{room}-{id}") if seed is not None else random.Random()

        chat_model = build_chat_model(settings)
        self.decider = structured(chat_model, SpeakDecision, settings.provider)
        self.actor = structured(chat_model, task.TurnAction, settings.provider)
        self.sio = socketio.AsyncClient()

        self.prompt = ""
        self.history = []
        self.last_seq = 0
        self.last_heard_at = time.monotonic()
        self.others_typing = set()

        # flags for activity in current round
        self.in_round = False
        self.done = False
        self.my_answer = None
        self.others_done = {}
        
        # Turn Taking
        self.triggers = asyncio.Queue()
        self.turn = None
        self.last_spoke_at = 0.0
        self.cooldown_seconds = 0.0
        self.running = True
        self._loop_task = None
        self._trace_tasks = set()

        self.register_handlers()
        LOG.info(f"Agent {id}: {settings.model}, {settings.turn_taking}")


    # ------- what happens in the room ---------------

    def register_handlers(self):
        @self.sio.on("round_start")
        async def on_round_start(data):
            self.start_round(data)


        @self.sio.on("round_end")
        async def on_round_end(data):
            self.in_round = False
            self._drain_triggers()
            self.history.append({"role": "user",
                                 "content": f"[Round {data['round']} has ended.]"})

        @self.sio.on("message")
        async def on_message(data):
            sender, text, seq = data.get("from"), data.get("message", ""), data.get("seq")
            if isinstance(seq, int):
                self.last_seq = max(self.last_seq, seq)

            if not sender or not text:
                return

            self.last_heard_at = time.monotonic()
            self.others_typing.discard(sender)
            if sender == self.id:
                return
            self.history.append({"role": "user", "content": f"{sender}: {text}"})

            if not self.in_round:
                return

            if self.settings.interrupt == "cancel" and self.turn and not self.turn.done():
                self.turn_cancel()

            self.triggers.put_nowait(Trigger("message", sender, text))

            
        @self.sio.on("typing")
        async def on_typing(data):
            if sender and sender != send.id:
                if data.get("state") == "start":
                    self.others_typing.add(sender)
                else:
                    self.others_typing.discard(sender)

        @self.sio.on("participant_status")
        async def on_status(data):
            who, done = data.get("participant"), bool(data.get("done"))
            if who == self.id:
                self.done = done
                if not done:
                    self.my_answer = None
                return
            if self.others_done.get(who) == done:
                return
            self.others_done[who] = done
            self.note(f"{who} has signalled they think the round is complete" if done
                     else f"{who} has withdrawn their done signal", trigger=True)

        @self.sio.on("round_warning")
        async def on_warning(data):
            self.note("Everyone is done, but the answers don't all match", trigger=True)

        @self.sio.on("status_error")
        async def on_status_error(data):
            self.my_answer = None
            self.note(f"Your done signal was not accepted: {data.get('error')}")
            self.trace(stage="status_error", outcome="rejected",
                       error=data.get('error'))

    
    def start_round(self, data):

        # get stimuli for participant
        image = self.task.material_path(data["stimuli"], data["round_id"], self.id).read_text()
        others = [p for p in data["participants"] if p != self.id]

        # build system prompt for participant in task
        self.prompt = self.task.render("system", participant=self.id, others=others,
                                       materials=image)

        if self.settings.memory == "none":      # start each round with clear memory
            self.history = []
        self.history.append({"role": "user",
                             "content": f"[Round {data['round']} of {data['of']} has started. You have a new image. It is in your instructions above]"})
        self.in_round = True
        self.done = False
        self.my_answer = None
        self.others_done = {p: False for p in others}
        self._drain_triggers()
        self._trace_system_prompt()
        self.triggers.put_nowait(Trigger("round_start"))


    def note(self, text: str, trigger: bool = False):
        """
        Add a notice to the agent's history, as a human would see it on screen
        """
        self.history.append({"role": "user", "content": f"[{text}]"})
        if trigger and self.in_round:
            self.triggers.put_nowait(Trigger("status", text=text))


    # --- taking turns -------------------------------

    async def act_loop(self):
        while self.running:

            try:
                trigger = await self.next_trigger()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.exception(f"{self.id} waiting for a trigger failed")
                self.trace(stage="loop", outcome="error",
                           error=f"{type(e).__name__}: {e}"[:500])
                await asyncio.sleep(1)
                continue
            
            if trigger is None or not self.in_round:
                continue

            self.turn = asyncio.create_task(self.strategy(self, trigger))
            try:
                await self.turn
            except asyncio.CancelledError:                  # the agent itself is stopping
                if asyncio.current_task().cancelling():
                    raise
                self.trace(stage="turn", outcome="cancelled", trigger=trigger.kind,
                           context_seq=self.last_seq)

            except Exception as e:
                LOG.exception(f"{self.id} turn failed")
                self.trace(stage="turn", outcome="error",
                           error=f"{type(e).__name__}: {e}"[:500])

            finally:
                self.turn = None
            

    async def next_trigger(self) -> Trigger | None:
        """
        The next thing to respond to. If several are waiting: a new round
        first, then a status change, otherwise the latest message.
        None means nothing to do this time; a silence trigger if the group
        has been quiet long enough.
        """
        try:
            first = await asyncio.wait_for(
                self.triggers.get(), timeout=self.rng.uniform(*self.settings.silence_check_every)
            )
        except TimeoutError:
            quiet_for = time.monotonic() - self.last_heard_at
            if (not self.in_round or self.cooling_down()
                    or quiet_for < self.settings.silence_wait):
                return None
            return Trigger("silence") if self.rng.random() < self.settings.silence_chance else None

        waiting = [first]
        while not self.triggers.empty():
            waiting.append(self.triggers.get_nowait())
        return self._pick(waiting)                


                

    def _drain_triggers(self):
        """
        Empty triggers queue
        """
        while not self.triggers.empty():
            self.triggers.get_nowait()


    def cooling_down(self) -> bool:
        """
        Is agent in cooling down period?
        """
        return time.monotonic() - self.last_spoke_at < self.cooldown_seconds

    def typing_note(self) -> str:
        typing = sorted(self.others_typing)
        return f"Currently typing: {', '.join(typing)}." if typing else "Nobody else is typing"

    def status_note(self) -> str:
        """
        Who has signalled done - as seen in indicators in chatroom UI
        """
        labels = self.task.defn.answer.options
        parts = []
        if self.done and self.my_answer:
            answer = ", ".join(labels.get(a, a) for a in self.my_answer)
            parts.append(f"You have signalled done, with the answer: {answer}")
        others = sorted(p for p, d in self.others_done.items() if d)
        parts.append(f"Also signalled done: {', '.join(others)}." if others
                     else "Nobody else has signalled done.")
        return "\n".join(parts)


    TRIGGER_PRIORITY = ("round_start", "status")
    
    def _pick(self, waiting: list[Trigger]) -> Trigger:
        """
        From serveral waiting triggers: a new round first, then a status
        change (both skip the decision)m otherwise the newest message
        """
        for kind in self.TRIGGER_PRIORITY:
            for t in reversed(waiting):
                if t.kind == kind:
                    return t
        return waiting[-1]
    
    
    # --- actions ------------------
    async def act(self, action, context_seq: int, cooldown):
        """
        Carry out the action the model chose
        """
        if action.current_action == "wait":
            return
        if action.message:
            await self.say(action.message, cooldown, context_seq)
        if action.current_action == "task_complete":
            same = self.done and sorted(action.answer or []) == sorted(self.my_answer or [])
            if not same:
                await self.signal_done(True, action.answer)
        elif action.current_action == "withdraw" and self.done:
            await self.signal_done(False)




    async def say(self, text: str, cooldown, context_seq: int | None = None):
        """
        Send message to chat
        """
        text = (text or "").strip()
        if text.startswith(f"{self.id}:"):
            text = text[len(self.id)+1:].strip()

        if not text or not self.in_round or not self.sio.connected:
            return
        self.history.append({"role": "assistant",
                             "content": f"{self.id}: {text}"})
        self.last_spoke_at = time.monotonic()
        self.cooldown_seconds = self.rng.uniform(*cooldown)

        message = {"from": self.id,
                   "room": self.room,
                   "message": text}

        if context_seq is not None:
            message["context_seq"] = context_seq

        await self.sio.emit("message", message)



    async def signal_done(self, done: bool, answer=None):
        """
        Signal (or withdraw) done. The server's participant_status confirms it
        """
        if done:
            self.my_answer = answer
        if self.sio.connected:
            await self.sio.emit("task_complete",
                                {"done": done, "answer": answer})
            

    # --- calling the model and tracing ---------------------------------------

    async def ask(self, runnable, stage: str, instruction: str):
        """Call a structured model with retries; trace every attempt."""

        base = {
            "stage": stage,
            "context_seq": self.last_seq,
            "history_len": len(self.history),
            "instruction": instruction,
        }
        messages = [
            {"role": "system", "content": self.prompt},
            *self.history,
            {"role": "user", "content": instruction},
        ]
        deadline = time.monotonic() + RETRY_BUDGET_S

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            started = time.monotonic()
            remaining = deadline - started
            if remaining <= 0:
                break
            elapsed_ms = lambda: int((time.monotonic() - started) * 1000)

            try:
                async with asyncio.timeout(min(CALL_TIMEOUT_S, remaining)):
                    result = await runnable.ainvoke(messages)
            except asyncio.CancelledError:
                self.trace(**base, outcome="cancelled", attempt=attempt, latency_ms=elapsed_ms())
                raise
            except TimeoutError:
                self.trace(**base, outcome="timeout", attempt=attempt, latency_ms=elapsed_ms())
                continue
            except Exception as e:
                error = f"{type(e).__name__}: {e}"[:500]
                self.trace(**base, outcome="llm_error", attempt=attempt,
                            latency_ms=elapsed_ms(), error=error)
                LOG.warning("%s: %s attempt %d failed: %s", self.id, stage, attempt, error)
                if not self._retryable(e):
                    return None
                delay = RETRY_BASE_DELAY_S * 2 ** (attempt - 1) * self.rng.uniform(0.5, 1.5)
                await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                continue

            raw = result["raw"]
            meta = getattr(raw, "response_metadata", None) or {}
            details = {
                "attempt": attempt,
                "latency_ms": elapsed_ms(),
                "usage": getattr(raw, "usage_metadata", None),
                "model_reported": meta.get("model_name") or meta.get("model"),
            }
            if result["parsing_error"]:
                self.trace(**base, **details, outcome="parse_error",
                            error=str(result["parsing_error"])[:500],
                            raw=str(getattr(raw, "content", ""))[:2000])
                return None

            parsed = result["parsed"]
            self.trace(**base, **details, outcome="ok", result=parsed.model_dump())
            return parsed

        return None

    @staticmethod
    def _retryable(e) -> bool:
        """Retry rate limits, server errors and connection problems; not bad requests."""
        status = getattr(e, "status_code", None) or getattr(e, "code", None)
        if not isinstance(status, int):
            return True
        return status in (408, 409, 429) or status >= 500


    def trace(self, **payload):
        """
        Send a trace event without waiting for it.
        Never blocks or raises
        """

        if not self.sio.connected:
            return

        task = asyncio.create_task(self._emit_trace(payload))
        self._trace_tasks.add(task)

        task.add_done_callback(self._trace_tasks.discard)

    async def _emit_trace(self, payload):
        try:
            await self.sio.emit("agent_trace", payload)
        except Exception:
            LOG.debug(f"{self.id}: trace emit failed", exc_info=True)
       

    def _trace_system_prompt(self):
        self.trace(stage="system_prompt", outcome="ok",
                    prompt_sha256=hashlib.sha256(self.prompt.encode()).hexdigest(),
                    prompt=self.prompt)

    def _loop_stopped(self, task):
        if not task.cancelled() and task.exception():
            LOG.error(f"{self.id}: turn loop stopped", exc_info=task.exception())
    

    # --- running --------------------------------------------------------------

    async def run(self, auth):
        try:
            await self.sio.connect(self.ws_url, socketio_path=self.socketio_path,
                                   auth=auth, transports=["websocket", "polling"])
            
            self._loop_task = asyncio.create_task(self.act_loop())
            self._loop_task.add_done_callback(self._loop_stopped)
            await self.sio.wait()

        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception(f"Agent {self.id} encountered an error")
        finally:
            self.running = False
            if self._loop_task:
                self._loop_task.cancel()
            if self.sio.connected:
                await self.sio.disconnect()
