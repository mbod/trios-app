import asyncio
import random
import time
import json
import socketio
import logging


# TODO: Using openai API Async
#       Update to use langchain to allow multiple
#       LLMs
#       Can use asyncio and `.ainvoke` function
#       See 
from openai import AsyncOpenAI


from dotenv import load_dotenv

_ = load_dotenv()

#MODEL_NAME = "gpt-4o-2024-11-20"
MODEL_NAME = "gpt-5-nano"
#MODEL_NAME = "gpt-4o-mini"
MODEL_NAME = "gpt-4.1-nano"
MODEL_NAME = "gpt-5.6-luna"


ROOM = "test"
SK = "coll@bplan!"

# number of differences between the three images
# this could vary for different rounds
DIFFERENCE_CNT = 11

LOG = logging.getLogger(__name__)


class Client:
    SYSTEM_PROMPT = """
    You are a member of a group of 3 people working on a task together.
    You are Participant {part_id}.

    Each of you has a similar image. Your task is to identify the differences
    between your versions. There are {difference_cnt} differences between the three images.
    The task for the group is to identify and agree upon those {difference_cnt} differences.
    Once your team has identified all the differences finish the task.
    
    Behave like an engaged member of a small group collaborating on a task:
    - Do not respond to every message.
    - Speak when you have useful new information.
    - Answer direct questions.
    - Avoid repeating yourself.
    - Let others speak.
    - If you just spoke, wait before speaking again.
    - Help keep the task on track 

    IMAGE:
    {image}
    """

    def __init__(self, id: str,
                 room: str,
                 image_file: str,
                 ws_url: str,
                 socketio_path: str):
        self.id = id
        self.room = room
        self.ws_url = ws_url
        self.socketio_path = socketio_path


        # load the specific image file for instance participant
        self.image = open(f"static/images/{image_file}").read()
        self.prompt = self.SYSTEM_PROMPT.format(
            part_id=self.id,
            difference_cnt=DIFFERENCE_CNT,
            image=self.image
        )

        self.history = []
        self.sio = socketio.AsyncClient()
        self.LLM = AsyncOpenAI()

        # keep track of async call awaiting response
        # that may need to be canceled if new information arrives
        self.pending_response_task = None

        # time to track time since participant last spoke
        self.last_spoke_at = 0

        # each participant has a static but random
        # cooldown period after speaking
        self.cooldown_seconds = random.uniform(6, 14)

        # timestamp of last message sent by participant
        self.last_heard_at = time.monotonic()
        self.silence_task = None
        self.found_differences = []

        # Track background tasks for handling
        # silence and pending response
        self.silence_task = None
        self.pending_response_task = None

        
        self.register_handlers()

    def register_handlers(self):
        @self.sio.event
        async def connect():
            LOG.info(f"Client {self.id} connected")

            self.silence_task = asyncio.create_task(self.silence_monitor())
            
            await self.sio.emit("join", {
                "id": self.id,
                "room": self.room,
                "kind": "agent"
            })

        @self.sio.event
        async def disconnect():
            LOG.info(f"Client {self.id} disconnected")
            self._cancel_all_subtasks()
            
        @self.sio.on("message")
        async def on_message(payload):
            sender = payload.get("from")
            text = payload.get("message", "")

            if not sender or not text:
                return

            self.last_heard_at = time.monotonic()
            
            self.history.append({
                "role": "user",
                "content": f"{sender}: {text}"
            })

            if sender == self.id:
                return

            LOG.info(f"{self.id} heard {sender}: {text}")

            # Cancel previous pending response because the conversational context changed.
            if self.pending_response_task and not self.pending_response_task.done():
                self.pending_response_task.cancel()

            self.pending_response_task = asyncio.create_task(
                self.maybe_respond_later(sender, text)
            )

    async def silence_monitor(self):
        while True:
            await asyncio.sleep(random.uniform(4, 8))
    
            silence_for = time.monotonic() - self.last_heard_at
            since_spoke = time.monotonic() - self.last_spoke_at
    
            if silence_for < 6:
                continue
    
            if since_spoke < self.cooldown_seconds:
                continue
    
            # Avoid everyone breaking silence at once.
            probability = 0.25
            if self.id == "A":
                probability = 0.45
    
            if random.random() < probability:
                await self.break_silence()


    async def break_silence(self):
        prompt = """
        The group has gone quiet.
    
        Continue the task naturally. Do one of these:
        - ask about a new concrete feature in your image
        - summarize one difference already found and move to another feature
        - mention a new visual detail that has not been discussed yet
    
        Do not repeat the bus color discussion unless necessary.
        Keep it brief and conversational.
    
        Output valid JSON only:
        {
          "message": "your chat message"
        }
        """
    
        messages = [
            {"role": "system", "content": self.prompt},
            *self.history[:],
            {"role": "user", "content": prompt}
        ]
    
        response = await self.LLM.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            response_format={"type": "json_object"},
        )
    
        resp = json.loads(response.choices[0].message.content)
        message_text = resp["message"].strip()
    
        if not message_text:
            return

        if not self.sio.connected:
            return
        
            
        self.history.append({
            "role": "assistant",
            "content": message_text
        })
    
        self.last_spoke_at = time.monotonic()
        self.cooldown_seconds = random.uniform(6, 14)


        await self.sio.emit("message", {
            "from": self.id,
            "room": self.room,
            "message": message_text
        })    
    
        
    async def maybe_respond_later(self, sender: str, text: str):
        try:
            # Human-ish latency.
            await asyncio.sleep(random.uniform(1.0, 3.0))

            # Cooldown: do not jump back in immediately after speaking.
            seconds_since_spoke = time.monotonic() - self.last_spoke_at
            if seconds_since_spoke < self.cooldown_seconds:
                LOG.info(f"{self.id} staying quiet: cooldown")
                return

            decision = await self.decide_whether_to_speak(sender, text)
            LOG.info(f"{self.id} speak decision:", decision)

            if decision.get("speak"):
                await self.respond()

        except asyncio.CancelledError:
            LOG.info(f"{self.id} reconsidering because a newer message arrived")

    async def decide_whether_to_speak(self, sender: str, text: str) -> dict:
        decision_prompt = f"""
        Decide whether Participant {self.id} should speak next.

        Last speaker: {sender}
        Last message: {text}

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

        messages = [
            {"role": "system", "content": self.prompt},
            *self.history[:],
            {"role": "user", "content": decision_prompt}
        ]

        response = await self.LLM.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            response_format={"type": "json_object"},
        )

        return json.loads(response.choices[0].message.content)

    async def respond(self):
        LOG.info(f"{self.id} responding")

        resp = await self.take_turn()

        message_text = resp.get("message", "").strip()
        action = resp.get("current_action", "").lower()

        if not message_text:
            return

        if "wait" in action:
            LOG.info(f"{self.id} chose to wait")
            return

        if not self.sio.connected:
            return
        

        self.history.append({
            "role": "assistant",
            "content": f"{self.id}: {message_text}"
        })

        self.last_spoke_at = time.monotonic()
        self.cooldown_seconds = random.uniform(2, 8)

        await self.sio.emit("message", {
            "from": self.id,
            "room": self.room,
            "message": message_text
        })

    async def take_turn(self) -> dict:
        turn_prompt = """
        Decide what to say next in the group discussion.

        You may:
        - describe a feature in your image
        - ask another participant about a feature
        - suggest a possible difference
        - summarize what the group has established
        - wait silently if you have nothing useful to add

        Keep the message concise and natural.

        Output valid JSON only:
        {
          "current_action": "say | ask | suggest_difference | summarize | wait",
          "message": "the chat message to send, or empty string if waiting"
        }
        """

        messages = [
            {"role": "system", "content": self.prompt},
            *self.history[:],
            {"role": "user", "content": turn_prompt}
        ]

        response = await self.LLM.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            response_format={"type": "json_object"},
        )

        resp = json.loads(response.choices[0].message.content)
        LOG.info(f"{self.id} turn:", resp)

        return resp

    async def run(self, auth):


        try:
            await self.sio.connect(self.ws_url,
                                   socketio_path=self.socketio_path,
                                   auth=auth,
                                   transports=['websocket','polling'])

            await self.sio.emit("message", {
                "from": self.id,
                "room": self.room,
                "message": "Hi, I’m here!"
            })

            await self.sio.wait()

        except asyncio.CancelledError:
            LOG.info(f"Agent {self.id} received cancellation request.")
        except Exception as e:
            LOG.error(f"Agent {self.id} encountered an error: {e}")
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
            
    

def run_agent(agent_id, room_id):
    client = Client(
        agent_id,
        room_id,
        f"image{agent_id}_nocomments.svg",
        "ws://localhost:5555"
    )

    auth = {
        "room": ROOM,
        "sid": agent_id,
        "token": SK
    }

    asyncio.run(client.run(auth))
