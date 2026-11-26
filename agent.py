"""An LLM participant in a session.

Two parts:
  - handlers (register_handlers) record what happens in the room, then queue a trigger;
  - one loop (act_loop) takes at most one turn at a time, using the turn-taking
    strategy named in the agent's settings (see strategies.py).
"""

import asyncio
import base64
import hashlib
import logging
import random
import time
from dataclasses import dataclass

import socketio
from pydantic import BaseModel, Field

from config import AgentSettings
from llm import build_chat_model, structured, provider_slot
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



class SelfDescription(BaseModel):
    """
    Your description of your materials
    """
    description: str = Field(description="your description")
    

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
                 task: Task, settings: AgentSettings, seed: int | None = None, limits: dict | None = None):

        from strategies import STRATEGIES
        if settings.turn_taking not in STRATEGIES:
            raise ValueError(f"unknown turn_taking '{settings.turn_taking}'; "
                             f"choose from {sorted(STRATEGIES)}")

        self.strategy = STRATEGIES[settings.turn_taking]

        self.id = id
        self.room = room
        self.round_no = None
        self.ws_url = ws_url
        self.socketio_path = socketio_path
        self.task = task
        self.settings = settings
        self.rng = random.Random(f"{seed}-{room}-{id}") if seed is not None else random.Random()

        chat_model = build_chat_model(settings)
        self.decider = structured(chat_model, SpeakDecision, settings.provider)
        self.actor = structured(chat_model, task.TurnAction, settings.provider)

        self.describer = structured(chat_model, SelfDescription, settings.provider)
        self.description = ""
        
        self.sio = socketio.AsyncClient()

        self.prompt = ""
        self.history = []
        self.last_seq = 0
        self.last_heard_at = time.monotonic()
        self.others_typing = {}

        # flags for activity in current round
        self.round_epoch = 0
        self.round_deadline = None
        self.in_round = False
        self.done = False
        self.my_answer = None
        self.others_done = {}


        self.limits = limits or {}
        
        # Turn Taking
        self.triggers = asyncio.Queue()
        self.turn = None
        self.last_spoke_at = 0.0
        self.cooldown_seconds = 0.0
        self.running = True
        self._loop_task = None
        self._trace_tasks = set()

        # image and materials
        self.materials_message = None
        self.view = None
        self.image_file = None

        
        self.register_handlers()
        LOG.info(f"Agent {id}: {settings.model}, {settings.turn_taking}")


    # ------- what happens in the room ---------------

    def register_handlers(self):
        @self.sio.on("round_start")
        async def on_round_start(data):
            try:
                self.start_round(data)
            except Exception as e:
                LOG.exception(f"{self.id}: start_round failed")
                self.trace(stage="error", outcome="start_round_failed",
                           error=f"{type(e).__name__}: {e}"[:500])
                await self.sio.disconnect()

        @self.sio.on("round_end")
        async def on_round_end(data):
            self.view = None
            self.in_round = False
            self.round_epoch += 1
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
            self.others_typing.pop(sender, None)
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
            sender = data.get("from")
            if not sender or sender == self.id:
                return
            if data.get("state") == "start":
                self.others_typing[sender] = time.monotonic()
            else:
                self.others_typing.pop(sender, None)
                self.last_heard_at = time.monotonic()
            

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

        @self.sio.on("task_view")
        async def on_task_view(data):
            if data.get("participant") != self.id or not self.in_round:
                return

            first = self.view is None
            self.view = data

            if first:
                try:
                    self.load_view_materials()
                    self.render_prompt()
                    self._trace_system_prompt()
                    self.triggers.put_nowait(Trigger("round_start"))
                except Exception as e:
                    LOG.exception(f"{self.id}: loading view failed")
                    self.trace(stage="error", outcome="view_failed",
                               error=f"{type(e).__name__}: {e}"[:500])


        @self.sio.on("action_error")
        async def on_action_error(data):
            self.note(f"Your card placement was not accepted: {data.get('error')}")
            self.trace(stage="action_error", outcome="rejected", error=data.get("error"))

             
    def start_round(self, data):
        self.round_label = data["round"]
        self.view = None
        if self.task.defn.engine == "choice":
            path = self.task.material_path(data["stimuli"], data["round_id"], self.id)
            label = f"Your picture for round {data['round']}"
            if self.settings.perception == "image":
                png = path.with_suffix(".png").read_bytes()
                self.materials_message = {"role": "user", "content": [
                    {"type": "text", "text": f"{label}:"},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}
                self.image_file = {"file": path.with_suffix(".png").name,
                                   "sha256": hashlib.sha256(png).hexdigest()[:12]}
            else:
                self.materials_message = {"role": "user", "content": (
                    f"{label} (given as SVG code; the others see it as an image. Describe "
                    "what it shows as a person looking at it would see it, never the code, "
                    f"coordinates or colour values):\n\n{path.read_text()}")}
                self.image_file = {"file": path.name}
            image = ""
        else:                                 # matching task: materials provided from view of engine
            image = ""
            self.materials_message = None
            self.image_file = None
        
        others = [p for p in data["participants"] if p != self.id]

        # build system prompt for participant in task

        self.last_seq = max(self.last_seq, data.get("seq") or 0)
        
        self.self_description = ""
        self.stimuli_name = data["stimuli"]
        self.image = image

        if self.task.defn.engine == "choice":         # only compile and render prompts for choice tasks
            self.render_prompt()
            self._trace_system_prompt()

        if self.settings.memory == "none":      # start each round with clear memory
            self.history = []


        self.history.append({"role": "user",
                             "content": f"[Round {data['round']} of {data['of']} has started. Each of you has a new picture.]"})
        
        limit = data.get("time_limit")
        self.round_deadline = time.monotonic() + limit if limit else None

        self.round_epoch += 1
        self.in_round = True
        self.done = False
        self.my_answer = None
        self.others_done = {p: False for p in others}
        self._drain_triggers()

        if self.task.defn.engine == "choice":
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

            if trigger.kind == "round_start":
                await self.describe_materials()

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
            
    def render_prompt(self):
        v = self.view or {}
        self.prompt = self.task.render(
            "system", materials=self.image, self_description=self.self_description,
            max_words=self.settings.max_words, perception=self.settings.perception,
            role=v.get("role"), director=v.get("director"),
            grid=v.get("grid"), figures=v.get("figures"),
            **self.task.prompt_context(self.stimuli_name, self.id))


    def _figure_file(self, url: str, suffix: str):
        """
        Path for a figure URL like `/stimuli/<task>/<set>/<file>`
        """
        set_name, filename = url.split("/")[-2:]
        return (self.task.dir / "stimuli" / set_name / filename).with_suffix(suffix)

    def load_view_materials(self):
        """
        Matching: the director's target grid, or a matcher's labelled cards
        """
        v = self.view
        if v["role"] == "director":
            items = [(f"Slot {i}", url) for i, url in enumerate(v["target"], start=1)]
            heading = "Your target grid, slot by slot:"
        else:
            items = [(f"Card {c}", url) for c, url in v["cards"].items()]
            heading = "Your cards:"

        heading = f"Round {self.round_label}: " + heading
            
        if self.settings.perception == "image":
            content = [{"type": "text", "text": heading}]
            for label, url in items:
                png = self._figure_file(url, ".png").read_bytes()
                content += [{"type": "text", "text": f"{label}:"},
                            {"type": "image_url", "image_url": {
                                "url": "data:image/png;base64,"+base64.b64encode(png).decode()}}]
            self.materials_message = {"role": "user", "content": content}
            self.image = ""
        else:
            text = "\n\n".join(f"{label}:\n{self._figure_file(url, ".svg").read_text()}"
                                     for label, url in items)

            self.materials_message = {"role": "user", "content": (
                f"{heading} (given as SVG code; the others see pictures. Describe them as a "
                f"person would see them, never the code):\n\n{text}")}

        self.image = ""
        
    def board_note(self) -> str:
        """
        Matching: the matcher's grid as text, as they'd see it on screen
        """
        v = self.view
        if not v or v.get("role") != "matcher":
            return ""

        slots = "; ".join(f"slot {i}: {'card ' + c if c else 'empty'}"
                          for i, c in enumerate(v["slots"], start=1))

        return f"Your grid: {slots}.\nCards not placed: {', '.join(v['pool']) or 'none'}."
    

    async def describe_materials(self):
        """
        Look at the round's materials before joining the conversation
        """

        # temp guard for setting self_description and trying task that doesn't have it
        if "describe" not in self.task.prompts:
            return
        
        if self.settings.perception != "self_description":
            return

        round_at_start = self.round_no
        result = await self.ask(self.describer, "self_description",
                                self.task.render("describe"), use_history=False)

        if self.round_no != round_at_start:
            return

        self.self_description = result.description if result else ""
        self.render_prompt()
        self._trace_system_prompt()
        
                
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
            if (not self.in_round or self.cooling_down() or self.typing_now()
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


    TYPING_STALE_S = 30

    def typing_now(self) -> list[str]:
        """Others currently typing, ignoring indicators older than TYPING_STALE_S."""
        now = time.monotonic()
        return sorted(p for p, t in self.others_typing.items()
                      if now - t < self.TYPING_STALE_S)
    

    def typing_note(self) -> str:
        typing = self.typing_now()
        if not typing:
            return "nobody else is typing"
        return f"{' and '.join(typing)} {'is' if len(typing) == 1 else 'are'} typing."


    def status_note(self) -> str:
        """
        Who has signalled done - as seen in indicators in chatroom UI
        """
        labels = self.task.defn.answer.options if self.task.defn.answer else {}
        parts = []
        if self.done and self.my_answer:
            answer = ", ".join(labels.get(a, a) for a in self.my_answer)
            parts.append(f"You have signalled done, with the answer: {answer}")

        if self.round_deadline is not None:
            left = max(0, int(self.round_deadline - time.monotonic()))
            parts.append(f"Time left in this round: {left // 60}:{left % 60:02d}.")
            
        others = sorted(p for p, d in self.others_done.items() if d)
        parts.append(f"Also signalled done: {', '.join(others)}." if others
                     else "Nobody else has signalled done.")

        if (board := self.board_note()):
            parts.append(board)
            
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
    async def act(self, action, context_seq: int, cooldown, epoch=None, turn_id=None):
        """
        Carry out the action the model chose
        """

        for p in getattr(action, "places", None) or []:
            if not self.in_round or (epoch is not None and epoch != self.round_epoch):
                break
            await self.sio.emit("task_action", {"type": "place", "card": p.card,
                                                "slot": p.slot})
            self.trace(stage="action", outcome="sent", card=p.card, slot=p.slot, turn_id=turn_id)
        
        if action.current_action == "wait":
            return
        if action.message:
            sent = await self.say(action.message, cooldown, context_seq, epoch, turn_id)
            if not sent:
                return
        answer = getattr(action, "answer", None)
        if action.current_action == "task_complete":
            same = self.done and sorted(answer or []) == sorted(self.my_answer or [])
            if not same:
                await self.signal_done(True, answer)
        elif action.current_action == "withdraw" and self.done:
            await self.signal_done(False)




    async def say(self, text: str, cooldown, context_seq: int | None = None, epoch=None, turn_id=None) -> bool:
        """
        Send message to chat
        """
        text = (text or "").strip()
        if text.startswith(f"{self.id}:"):
            text = text[len(self.id)+1:].strip()

        if not text or not self.sio.connected:
            return False

        seq_before = self.last_seq
        typed_s = await self.simulate_typing(text)

        if not self.in_round or (epoch is not None and epoch != self.round_epoch):
            self.trace(stage="gate", outcome="stale_round", context_seq=context_seq, turn_id=turn_id)
            return False

        self.trace(stage="typing", outcome="sent", context_seq=context_seq,
                   typed_s=round(typed_s,1), heard_while_typing=self.last_seq - seq_before)
    
        
        self.history.append({"role": "assistant",
                             "content": f"{self.id}: {text}"})
        self.last_spoke_at = time.monotonic()
        self.cooldown_seconds = self.rng.uniform(*cooldown)

        message = {"from": self.id, "room": self.room,
                   "message": text,
                   "context_seq": context_seq, "turn_id": turn_id}

        await self.sio.emit("message", message)
        return True


    async def signal_done(self, done: bool, answer=None):
        """
        Signal (or withdraw) done. The server's participant_status confirms it
        """
        if done:
            self.my_answer = answer
        if self.sio.connected:
            await self.sio.emit("task_complete",
                                {"done": done, "answer": answer})
            self.trace(stage="signal", outcome="done" if done else "withdraw", answer=answer)

    # --- calling the model and tracing ---------------------------------------

    async def ask(self, runnable, stage: str, instruction: str,
                  use_history: bool = True, turn_id: str | None = None):
        """Call a structured model with retries; trace every attempt."""
        base = {"stage": stage, "context_seq": self.last_seq,
                "history_len": len(self.history), "instruction": instruction,
                "turn_id": turn_id}
        
        messages = [{"role": "system", "content": self.prompt},
                    *(self.history if use_history else []),
                    *([self.materials_message] if self.materials_message else []),
                    {"role": "user", "content": instruction}]

        deadline = time.monotonic() + RETRY_BUDGET_S
        slot = provider_slot(self.settings.provider, self.limits)
        ms = lambda since: int((time.monotonic() - since) * 1000)

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            # --- wait for a slot, make one call, release the slot ---
            queued = time.monotonic()
            async with slot:
                queue_ms = ms(queued)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.trace(**base, outcome="timeout", attempt=attempt, queue_ms=queue_ms)
                    return None
                started = time.monotonic()
                error = None
                try:
                    async with asyncio.timeout(min(CALL_TIMEOUT_S, remaining)):
                        result = await runnable.ainvoke(messages)
                except asyncio.CancelledError:
                    self.trace(**base, outcome="cancelled", attempt=attempt,
                               queue_ms=queue_ms, latency_ms=ms(started))
                    raise
                except TimeoutError:
                    error = "timeout"
                except Exception as e:
                    error = e
                latency_ms = ms(started)
            # --- slot released: retries and back-off happen without holding it ---

            timing = {"attempt": attempt, "queue_ms": queue_ms, "latency_ms": latency_ms}
            if error == "timeout":
                self.trace(**base, **timing, outcome="timeout")
                continue
            if error is not None:
                message = f"{type(error).__name__}: {error}"[:500]
                self.trace(**base, **timing, outcome="llm_error", error=message)
                LOG.warning("%s: %s attempt %d failed: %s", self.id, stage, attempt, message)
                if not self._retryable(error):
                    return None
                delay = RETRY_BASE_DELAY_S * 2 ** (attempt - 1) * self.rng.uniform(0.5, 1.5)
                await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                continue

            raw = result["raw"]
            meta = getattr(raw, "response_metadata", None) or {}
            details = {**timing, "usage": getattr(raw, "usage_metadata", None),
                       "model_reported": meta.get("model_name") or meta.get("model")}
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
                    prompt=self.prompt, image=self.image_file)

    def _loop_stopped(self, task):
        if not task.cancelled() and task.exception():
            LOG.error(f"{self.id}: turn loop stopped", exc_info=task.exception())


    async def _emit_typing(self, state: str):
        if self.sio.connected:
            await self.sio.emit("typing", {"from": self.id, "state": state})

    async def simulate_typing(self, text: str) -> float:
        """
        Show 'typing' for as long as a person would take to type text
        """
        wpm = self.settings.typing_wpm
        if not wpm:
            return 0.0

        words = len(text)/5       # using 5 chars average per 'word'
        seconds = words / self.rng.uniform(*wpm) * 60
        await self._emit_typing("start")
        try:
            await asyncio.sleep(seconds)
        finally:
            await self._emit_typing("stop")
        return seconds



    def render_decide(self, trigger) -> str:
        return self.task.render("decide",
                                participant=self.id,
                                last_sender=trigger.sender,
                                last_message=trigger.text,
                                typing_note=self.typing_note(),
                                status_note=self.status_note(),
                                role=(self.view or {}).get("role")
                                )

    def render_turn(self, silence: bool) -> str:
        return self.task.render("turn",
                                situation=self.task.render("silence") if silence else "",
                                typing_note=self.typing_note(),
                                status_note=self.status_note(),
                                max_words=self.settings.max_words,
                                role=(self.view or {}).get("role")
                                )



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
