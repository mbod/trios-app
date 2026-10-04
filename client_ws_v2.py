

import asyncio
import random
import time
import json
import socketio
import logging
from pathlib import Path

# TODO: Using openai API Async
#       Update to use langchain to allow multiple
#       LLMs
#       Can use asyncio and `.ainvoke` function
#       See 
from openai import AsyncOpenAI
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


DEFAULT_MODEL = "gpt-6-luna"
MODEL_DICT = {
    'A': 'gpt-6-luna',
    'B': 'gpt-6-luna',
    'C': 'gpt-6-luna'
}



class Client:
    SYSTEM_PROMPT = """
    You are a member of a group of 3 people working on a task together.
    You are Participant {part_id}.

    Each of you has an image with 9 items arranged in a 3x3 grid.

    1 2 3
    4 5 6
    7 8 9
    
    You all have the same 9 items but arranged differently.
    However, there are two sequences of 3 items that are ordered
    in the same way same across all three images.

    Examples could be:
    1. horizontal (e.g. row 1, items 1 2 3 are the same)
    2. vertical (e.g. col 2, items 2 5 8 are the same)
    3. diagonal (e.g. items 1 5 9 are the same)

    Your task is to discuss together and describe the items in your
    grid to each other so that you can agree upon the two shared
    sequences.

    Once you have agreed that you have identified the two shared
    sequences the task is complete and you should stop.
    
    
    For instance:

    ImgA    ImgB     ImgC
    -----   -----    -----
    P Q R   P Q R    P Q R
    Z S X   M S N    N S Z
    M N T   X Z T    M X T

    Each of you has one of the three images to complete the task
    you would go around and describe your images until you agreed
    in the above example:
    1. top row = P Q R
    2. TL-BR diagonal = P S T
    are the same across your three images.
    
    Behave like an engaged member of a small group collaborating on a task:
    - Do not respond to every message.
    - Speak when you have useful new information.
    - Answer direct questions.
    - Avoid repeating yourself.
    - Let others speak.
    - If you just spoke, wait before speaking again.
    - Help keep the task on track
    - When you all agree the task is complete you should stop

    IMAGE:
    {image}
    """

    SILENCE_NOTE = """
        The group has gone quiet. If you have something useful to add, continue 
        the task naturally: check whether the task has been completed, ask about 
        or describe an item not yet discussed, or summarize what the group has 
        established so far.
    """

    TURN_PROMPT = """
    Decide what to say next in the group discussion.

    You may:
    - describe a feature/item in your image
    - ask another participant about a feature/item
    - suggest a possible difference or similarity
    - summarize what the group has established
    - wait silently if you have nothing useful to add
    - send a task_complete message indicating that you think group has completed the task

    Keep the message concise and natural.

    Output valid JSON only:
    {
      "current_action": "say | ask | suggest_difference | summarize | wait | task_complete",
      "message": "the chat message to send, or empty string if waiting or task_complete"
    }
    """

    INITIAL_GREETING = "Hi! I'm here ready to work on the task"
    

    def __init__(self, id: str,
                 room: str,
                 image_file: str,
                 ws_url: str,
                 socketio_path: str):

        
        self.id = id
        self.room = room
        self.ws_url = ws_url
        self.socketio_path = socketio_path

        self.model = MODEL_DICT.get(id, DEFAULT_MODEL)
        # load the specific image file for instance participant
        self.image = Path(image_file).read_text()

        self.prompt = self.SYSTEM_PROMPT.format(
            part_id=self.id,
            image=self.image
        )

        self.history = []
        self.sio = socketio.AsyncClient()
        self.LLM = AsyncOpenAI()

        # flag to indicate whether agent has
        # sent completion message and should stop working
        self.task_completed = False
        self.others_typing = set()


        # Turn taking state
        self.last_spoke_at = 0.0
        self.last_heard_at = time.monotonic()
        self.cooldown_seconds = random.uniform(6,14)
        self.speak_lock = asyncio.Lock()


        # Background tasks
        self.silence_task = None
        self.pending_response_task = None

        LOG.info(f"Agent {self.id} using model {self.model}, image {Path(image_file).name}")
        self.register_handlers()



    # --- helper functions

    async def _ask_json(self, instruction: str) -> dict | None:
        """
        Send chat history and instruction to the LLM;
        return parsed JSON or None
        """

        messages = [
            {"role": "system", "content": self.prompt},
            *self.history,
            {"role": "user", "content": instruction}
        ]

        try:
            response = await self.LLM.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"}
                )

            return json.loads(response.choices[0].message.content)
        except:
            LOG.exception(f"{self.id}: LLM call failed -- {messages}")
            return None


    def _typing_note(self) -> str:
        typing = sorted(self.others_typing)
        if typing:
            return f"Currently typing: {', '.join(typing)}."
        return "Nobody else is typing."

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
            if sender and sender != self.id and not self.task_completed:
                self.history.append({
                    "role": "user",
                     "content": f"[{sender} has signalled they think the task is complete]"
                })
            


    # --------- action functions

    
    async def say(self, text: str, cooldown_range=(2, 8)):
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
        self.cooldown_seconds = random.uniform(*cooldown_range)

        await self.sio.emit("message",
                            {"from": self.id, "room": self.room,
                             "message": text })
        


                
    async def silence_monitor(self):
        """
        Handles how long to stay silent while monitoring activity in chatroom
        """
        while not self.task_completed:
            await asyncio.sleep(random.uniform(4, 8))

            try:
                now = time.monotonic()
                if now - self.last_heard_at < 6:
                    continue
                if now - self.last_spoke_at < self.cooldown_seconds:
                    continue
                if self.speak_lock.locked():
                    continue
                if self.pending_response_task and not self.pending_response_task.done():
                    continue


                # TODO - revisit this - giving different values to different
                #        speakers for some randomness but unclear if it will
                #        impact or skew speaker A over trials
                #        was added to avoid breaking silence at same time but not sure if needed still
                probability = 0.45 if self.id == "A" else 0.25

                if random.random() < probability:
                    await self.respond(situation=self.SILENCE_NOTE, cooldown_range=(6,14))
            except Exception:
                LOG.exception(f"{self.id}: silence monitor error")
                                       
            
    
        
        
    async def maybe_respond_later(self, sender: str, text: str):
        """
        Add latency to agent responses and check last spoke and cooldown periods
        to provide more human-like response timing
        """
        try:
            # Human-ish latency.
            await asyncio.sleep(random.uniform(1.0, 3.0))

            # check to see if cooldown period greater than last spoke at and keep quiet if so
            if time.monotonic() - self.last_spoke_at < self.cooldown_seconds:
                LOG.info(f"{self.id} staying quiet: cooldown")
                return
            
            decision = await self.decide_whether_to_speak(sender, text)
            LOG.info(f"{self.id} speak decision: {decision}")

            if decision and decision.get("speak"):
                await self.respond()

        except asyncio.CancelledError:
            LOG.info(f"{self.id} reconsidering because a newer message arrived")
            raise

        

    async def decide_whether_to_speak(self, sender: str, text: str) -> dict | None:
        decision_prompt = f"""
        Decide whether Participant {self.id} should speak next.

        Last speaker: {sender}
        Last message: {text}
        {self._typing_note()}
        
        You are simulating a natural human chatroom participant.

        Speak only if one of these is true:
        - You were directly asked a question.
        - You have new useful information about your image.
        - You need to clarify a possible difference.
        - The group seems stuck or confused.

        Do NOT speak if:
        - You would only agree.
        - You would repeat something you already said.
        - The latest message is better answered by someone else.
        - You recently spoke and should let others talk.

        Output valid JSON only:
        {{
          "speak": true or false,
          "reason": "brief reason"
        }}
        """

        return await self._ask_json(decision_prompt)
        


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
        prompt = "\n".join(p for p in (situation, self._typing_note(), self.TURN_PROMPT) if p)

        resp = await self._ask_json(prompt)

        if not resp:
            return

        action = str(resp.get("current_action","")).lower()
        LOG.info(f"{self.id} turn: {resp}")

        # take turn action
        if "task_complete" in action:
            await self.signal_complete()
        elif "wait" in action:
            LOG.info(f"{self.id} chose to wait")
        else:
            await self.say(resp.get("message", ""), cooldown_range)
            

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

            await self.say(self.INITIAL_GREETING)
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
            
    

