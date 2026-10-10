

import asyncio
import random
import time
import json
import socketio
import logging
import hashlib
from pathlib import Path

from config import AgentConfig, ModelSpec
from tasks import Task
from llm import build_chat_model, structured
from schemas import SpeakDecision


from dotenv import load_dotenv

_ = load_dotenv()


LOG = logging.getLogger(__name__)

# Testing with a range of openai models initially
# TODO: generalize async handler to work with any LLM

# OpenAI model ids for testing
#
# gpt-4o-2024-11-20
# gpt-5-nano
# gpt-4o-mini
# gpt-4.1-nano
# gpt-5.6-luna
# gpt-5.4-nano

# claude-haiku-4-5-20251001


"""
MODELS = {
    "gpt":     ModelSpec(provider="openai", model="gpt-6-luna"), 
    "claude":  ModelSpec(provider="anthropic", model="claude-haiku-5-5"),
    "gemini":  ModelSpec(provider="google_genai", model="gemini-3.5-flash-lite",
                         structured_output="json_schema"),
    "local":   ModelSpec(provider="openai_compatible", model="llama3.2:latest",
                         base_url="http://localhost:11434/v1")
}
"""

DEFAULT_MODEL = "gpt"

"""
AGENT_MODELS = {
    "A" : "gpt",
    "B" : "claude",
    "C" : "gemini"
}
"""


class Client:
    INITIAL_GREETING = "Hi! I'm here ready to work on the task"
    

    def __init__(self, id: str,
                 room: str,
                 image_file: str,
                 ws_url: str,
                 socketio_path: str,
                 task: Task, cfg: AgentConfig,
                 model_spec: ModelSpec, seed: int | None = None
                 ):

        
        self.id = id
        self.room = room
        self.ws_url = ws_url
        self.socketio_path = socketio_path

        self.task = task
        self.cfg = cfg

        self.model_spec = model_spec
        self._check_supported()

        self.rng = random.Random(f"{seed}-{room}-{id}") if seed is not None else random.Random()


        self.image = Path(image_file).read_text()
        others = [part for part in task.defn.participants if part != id]  # ids of other participants (? NEEDED)

        # create system prompt from prompt template with participant specific materials
        self.prompt = task.render("system", participant=id, others=others, materials=self.image)
        
        chat_model = build_chat_model(self.model_spec)
        self.decider = structured(chat_model, SpeakDecision, self.model_spec)
        self.actor = structured(chat_model, task.TurnAction, self.model_spec)

        
        
        
        self.history = []
        self.sio = socketio.AsyncClient()


        # flag to indicate whether agent has
        # sent completion message and should stop working
        self.task_completed = False
        self.others_typing = set()


        # Turn taking state
        self.last_spoke_at = 0.0
        self.last_heard_at = time.monotonic()
        self.cooldown_seconds = self.rng.uniform(*cfg.timing.cooldown_after_silence_s)
        self.speak_lock = asyncio.Lock()
        self.last_seq = 0    # last server seq this agent has seen
        self._trace_tasks = set()

        # Background tasks
        self.silence_task = None
        self.pending_response_task = None

        LOG.info(f"Agent {self.id} using model {self.model_spec.model} from {self.model_spec.provider}, image {Path(image_file).name}")
        self.register_handlers()


    def _check_supported(self):
        """
        Check settings passed in initialization and refuse if not supported
        """
        unsupported = []
        if self.cfg.turn.mode != "decide_then_act":
            unsupported.append(f"turn.mode={self.cfg.turn.mode}")

        if self.cfg.turn.interrupt != "cancel":
            unsupported.append(f"turn.interrupt={self.cfg.turn.interrupt}")

        if self.cfg.perception != "raw":
            unsupported.append(f"perception={self.cfg.perception}")

        if unsupported:
            raise ValueError(f"agent {self.id}: not implemented yet: {', '.join(unsupported)}")
        

    # --- helper functions

    async def _ask(self, runnable, stage: str, instruction: str):
        """Call a structured model with retries; trace every attempt."""
        retry = self.cfg.retry
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
        deadline = time.monotonic() + retry.budget_s

        for attempt in range(1, retry.attempts + 1):
            started = time.monotonic()
            remaining = deadline - started
            if remaining <= 0:
                break
            elapsed_ms = lambda: int((time.monotonic() - started) * 1000)

            try:
                async with asyncio.timeout(min(self.model_spec.timeout, remaining)):
                    result = await runnable.ainvoke(messages)
            except asyncio.CancelledError:
                self._trace(**base, outcome="cancelled", attempt=attempt, latency_ms=elapsed_ms())
                raise
            except TimeoutError:
                self._trace(**base, outcome="timeout", attempt=attempt, latency_ms=elapsed_ms())
                continue
            except Exception as e:
                error = f"{type(e).__name__}: {e}"[:500]
                self._trace(**base, outcome="llm_error", attempt=attempt,
                            latency_ms=elapsed_ms(), error=error)
                LOG.warning("%s: %s attempt %d failed: %s", self.id, stage, attempt, error)
                if not self._retryable(e):
                    return None
                delay = retry.base_delay_s * 2 ** (attempt - 1) * self.rng.uniform(0.5, 1.5)
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
                self._trace(**base, **details, outcome="parse_error",
                            error=str(result["parsing_error"])[:500],
                            raw=str(getattr(raw, "content", ""))[:2000])
                return None

            parsed = result["parsed"]
            self._trace(**base, **details, outcome="ok", result=parsed.model_dump())
            return parsed

        return None

    @staticmethod
    def _retryable(e) -> bool:
        """Retry rate limits, server errors and connection problems; not bad requests."""
        status = getattr(e, "status_code", None) or getattr(e, "code", None)
        if not isinstance(status, int):
            return True
        return status in (408, 409, 429) or status >= 500

    

    def _typing_note(self) -> str:
        typing = sorted(self.others_typing)
        if typing:
            return f"Currently typing: {', '.join(typing)}."
        return "Nobody else is typing."


    def _trace(self, **payload):
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
        self._trace(stage="system_prompt", outcome="ok",
                    prompt_sha256=hashlib(self.prompt.encode()).hexdigest(),
                    prompt=self.prompt)
            
    # ----------

        

    def register_handlers(self):
        @self.sio.event
        async def connect():
            LOG.info(f"Agent {self.id} connected")

            if self.silence_task is None or self.silence_task.done():
                self.silence_task = asyncio.create_task(self.silence_monitor())
            
            # TODO - should there be a join indicator other than the canned 'hi' message currently?
            # await self.sio.emit("join", {
            #    "id": self.id,
            #    "room": self.room,
            #    "kind": "agent"
            #})


            @self.sio.on("session_start")
            async def on_session_start(payload):
                self._trace_system_prompt()

            @self.sio.on("round_start")
            async def on_round_start(payload):
                self._trace_system_prompt()
            
        @self.sio.event
        async def disconnect():
            LOG.info(f"Agent {self.id} disconnected")
            
        @self.sio.on("message")
        async def on_message(payload):
            sender = payload.get("from")
            text = payload.get("message", "")

            if self.task_completed or not sender or not text:
                return

            self.last_heard_at = time.monotonic()
            self.others_typing.discard(sender)

            seq = payload.get("seq")
            if isinstance(seq, int):
                self.last_seq = max(self.last_seq, seq)
            
            if sender == self.id:
                return               # don't add echo to history
            
            self.history.append({
                "role": "user",
                "content": f"{sender}: {text}"
            })

            LOG.info(f"{self.id} heard {sender}: {text}")

            # Cancel previous pending response because the conversational context changed.
            if self.pending_response_task and not self.pending_response_task.done():
                self.pending_response_task.cancel()

            self.pending_response_task = asyncio.create_task(
                self.maybe_respond_later(sender, text)
            )

        @self.sio.on("typing")
        async def on_typing(payload):
            sender = payload.get("from")
            if not sender or sender == self.id:
                return
            if payload.get("state") == "start":
                self.others_typing.add(sender)
            else:
                self.others_typing.discard(sender)

        @self.sio.on("task_complete")
        async def on_task_complete(payload):
            sender = payload.get("from")

            seq = payload.get("seq")
            if isinstance(seq, int):
                self.last_seq = max(self.last_seq, seq)

            
            if sender and sender != self.id and not self.task_completed:
                self.history.append({
                    "role": "user",
                     "content": f"[{sender} has signalled they think the task is complete]"
                })

            
            


    # --------- action functions

    
    async def say(self, text: str, cooldown_range, context_seq: int | None = None):
        """
        This is where an agent sends a chat message to chatroom
        """

        text = (text or "").strip()
        prefix = f"{self.id}:"

        if text.startswith(prefix):
            text = text[len(prefix):].strip()

        if not text or self.task_completed or not self.sio.connected:
            return

        self.history.append({"role": "assistant", "content": f"{self.id}: {text}"})
        self.last_spoke_at = time.monotonic()
        self.cooldown_seconds = self.rng.uniform(*cooldown_range)


        message = {"from": self.id, "room": self.room, "message": text}
        if context_seq is not None:
            message["context_seq"] = context_seq
        
        await self.sio.emit("message", message)

        


                
    async def silence_monitor(self):
        """
        Handles how long to stay silent while monitoring activity in chatroom
        """


        silence = self.cfg.silence
        while not self.task_completed:
            await asyncio.sleep(self.rng.uniform(*silence.check_every_s))

            try:
                now = time.monotonic()
                if now - self.last_heard_at < silence.threshold_s:
                    continue


                if now - self.last_spoke_at < self.cooldown_seconds:
                    continue
                if self.speak_lock.locked():
                    continue
                if self.pending_response_task and not self.pending_response_task.done():
                    continue

                if self.rng.random() < silence.probability:
                    await self.respond(situation=self.task.render('silence'),
                                       cooldown_range=self.cfg.timing.cooldown_after_silence_s)

            except Exception:
                LOG.exception(f"{self.id}: silence monitor error")
                                       
            
    
        
        
    async def maybe_respond_later(self, sender: str, text: str):
        """
        Add latency to agent responses and check last spoke and cooldown periods
        to provide more human-like response timing
        """
        try:
            # Human-ish latency.
            await asyncio.sleep(self.rng.uniform(*self.cfg.timing.think_s))

            # check to see if cooldown period greater than last spoke at and keep quiet if so
            if time.monotonic() - self.last_spoke_at < self.cooldown_seconds:
                LOG.info(f"{self.id} staying quiet: cooldown")
                self._trace(stage="gate", outcome="cooldown",
                            context_seq = self.last_seq)
                return
            
            decision = await self.decide_whether_to_speak(sender, text)
            LOG.info(f"{self.id} speak decision: {decision}")

            if decision and decision.speak:
                await self.respond(cooldown_range=self.cfg.timing.cooldown_after_reply_s)

        except asyncio.CancelledError:
            LOG.info(f"{self.id} reconsidering because a newer message arrived")
            raise

        

    async def decide_whether_to_speak(self, sender: str, text: str) -> dict | None:
        prompt = self.task.render("decide", participant=self.id,
                                  last_sender=sender, last_message=text,
                                  typing_note=self._typing_note())
        
        return await self._ask(self.decider, "decide", prompt)


    async def respond(self, situation: str = "", cooldown_range=(2,8)):
        """
        Take a turn (just one)
        - ask the LLM what do
        - do this

        Params:
            situation: optional str giving situational context for prompt
            cooldown_range: default 2-8 secs
        

        """

        async with self.speak_lock:
            if self.task_completed:
                return

            if time.monotonic() - self.last_spoke_at < self.cooldown_seconds:
                return

        # construct turn taking prompt    
        prompt = self.task.render("turn", situation=situation, typing_note=self._typing_note(),
                                  max_words=self.cfg.turn.max_words)
        

        stage = "silence_turn" if situation else "turn"
        context_seq = self.last_seq
        
        
        resp = await self._ask(self.actor, stage, prompt)

        if not resp:
            return

        LOG.info(f"{self.id} turn: {resp}")

        # take turn action
        if resp.current_action == "task_complete":
            await self.signal_complete()
        elif resp.current_action == "wait":
            LOG.info(f"{self.id} chose to wait")
        else:
            await self.say(resp.message, cooldown_range, context_seq=context_seq)
            

    async def signal_complete(self):
        self.task_completed = True
        LOG.info(f"{self.id} signalled task complete")

        if self.sio.connected:
            await self.sio.emit("task_complete", {"from": self.id, "room": self.room })
                                

        
    async def run(self, auth):


        try:
            await self.sio.connect(self.ws_url,
                                   socketio_path=self.socketio_path,
                                   auth=auth,
                                   transports=['websocket','polling'])

            await self.say(self.INITIAL_GREETING,
                           self.cfg.timing.cooldown_after_reply_s,
                           context_seq=self.last_seq
                           )
            await self.sio.wait()

        except asyncio.CancelledError:
            LOG.info(f"Agent {self.id} received cancellation request.")
            raise
        except Exception:
            LOG.exception(f"Agent {self.id} encountered an error")
        finally:
            self._cancel_all_subtasks()
            if self.sio.connected:
                await self.sio.disconnect()
            

    def _cancel_all_subtasks(self):
        """Cancels all background loops and pending LLM response tasks."""
        if self.silence_task and not self.silence_task.done():
            self.silence_task.cancel()
        if self.pending_response_task and not self.pending_response_task.done():
            self.pending_response_task.cancel()
            
    

