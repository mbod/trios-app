# agent_manager.py

import asyncio
import logging
import os
import re
from pathlib import Path

from pydantic import ValidationError

from config import AgentSettings, load_condition
from tasks import Task

from aiohttp import web
from dotenv import load_dotenv

from agent import Client

_ = load_dotenv()


DEFAULT_CONDITION = os.environ.get("CONDITION", "pilot")
DEFAULT_TASK = os.environ.get("TASK", "common_lines")

AGENT_TOKEN = os.environ['AGENT_TOKEN']

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [AgentManager] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
LOG = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = (BASE_DIR / "static" / "images").resolve()
ID_PATTERN = re.compile(r"^[A-z0-9_-]{1,32}$")

def resolve_image(image_prefix: str, agent_id: str) -> Path | None:
    """
    Return the agent's image path if it exists else None
    """
    candidate = (IMAGES_DIR / f"{image_prefix}{agent_id}.svg").resolve()
    if candidate.is_relative_to(IMAGES_DIR) and candidate.is_file():
        return candidate

    return None

# Registry: (room_id, agent_id) -> {"job": asyncio.Task, "client": Client}
RUNNING_AGENTS = {}

BASE_WS_URL = "http://127.0.0.1:5555"
#SOCKETIO_PATH = "/services/trios-app/socket.io"
SOCKETIO_PATH = "/socket.io"


def _on_agent_done(key, job):
    """
    Runs when an agent ends a task for any reason to clean up
    """
    info = RUNNING_AGENTS.get(key)
    if info and info["job"] is job:
        RUNNING_AGENTS.pop(key)

    if job.cancelled():
        LOG.info(f"Agent {key} stopped (canncelled)")
    elif job.exception():
        LOG.error(f"Agent {key} crashed!", exc_info=job.exception())
    else:
        LOG.info(f"Agent {key} finished")
        




async def start_agent_handler(request):
    """Spawns an agent inside the async event loop as a background task."""
    data = await request.json()
    agent_id = data.get("agent_id", "")
    room_id = data.get("room_id", "")
    image_prefix = data.get("image_path") or "image"


    
    if not ID_PATTERN.match(agent_id) or not ID_PATTERN.match(room_id):
        return web.json_response(
            {"error": "agent_id and room_id must be 1-32 letters, digits, _ or - ONLY"},
            status=400)

    image_file = resolve_image(image_prefix, agent_id)
    if image_file is None:
        return web.json_response(
            {"error": f"image not found for prefix '{image_prefix}' and agent {agent_id}"},
            status=400)
        

    key = (room_id, agent_id)
    if key in RUNNING_AGENTS:
        return web.json_response({
            "status": "already_running",
            "message": f"Agent {agent_id} is already running in room {room_id}"
        })



    try:
        cond = load_condition(data.get("condition") or DEFAULT_CONDITION)
        settings = cond.agent(agent_id)

        if data.get("model"):
            settings = AgentSettings(**{**settings.model_dump(),
                                        "model": data["model"]})

        task = Task(data.get("task") or DEFAULT_TASK)
        
        client = Client(
            id=agent_id, room=room_id, image_file=str(image_file),
            ws_url=BASE_WS_URL, socketio_path=SOCKETIO_PATH,
            task=task, settings=settings, seed=cond.seed
        )
    except (FileNotFoundError, ValueError, ValidationError) as e:
        return web.json_response({
            "error": f"config: {e}"}, status=400)
    
    auth = {
        "room": room_id,
        "agent_id": agent_id,
        "token": AGENT_TOKEN
    }

    # Spawn directly in the existing asyncio event loop
    job = asyncio.create_task(client.run(auth),
                               name=f"agent-{room_id}-{agent_id}")
    RUNNING_AGENTS[key] = {"job": job, "client": client,
                           "condition": cond.name, 
                           "seed": cond.seed
                           }

    job.add_done_callback(lambda t: _on_agent_done(key, t))
    
    LOG.info(f"Started agent {agent_id} in Room {room_id} with IMAGE {image_file.name}")
    
    return web.json_response({
        "status": "started",
        "agent_id": agent_id,
        "room_id": room_id
    })


async def config_handler(request):
    """
    Settings for every agent running in a room, for the session log
    """
    room_id = request.query.get("room_id")
    agents = {}
    for (room, agent_id), info in RUNNING_AGENTS.items():
        if room == room_id:
            client = info["client"]
            agents[agent_id] = {
                "condition": info["condition"],
                "seed": info["seed"],
                "task": client.task.defn.name,
                "settings": client.settings.model_dump()
            }

    return web.json_response({"agents": agents})
                


async def stop_agent_handler(request):
    """Gracefully cancels a running agent task."""
    data = await request.json()
    key = (data.get("room_id"), data.get("agent_id"))

    info = RUNNING_AGENTS.get(key)
    if not info:
        return web.json_response(
            {"error": "Agent not found or already stopped"},
            status=404)
    
    job = info["job"]
    job.cancel()

    await asyncio.wait({job}, timeout=5)

    return web.json_response(
        {"status": "stopped",
         "agent_id": key[1],
         "room_id": key[0]})


async def list_agents_handler(request):
    """Returns all currently active agents."""
    active = [
        {"room_id": r, "agent_id": a, "done": info["job"].done()}
        for (r, a), info in RUNNING_AGENTS.items()
    ]
    return web.json_response({"active_agents": active})


async def on_shutdown(app):
    jobs = [info["job"] for info in RUNNING_AGENTS.values()]
    for job in jobs:
        job.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def create_app():
    app = web.Application()
    app.router.add_post("/start", start_agent_handler)
    app.router.add_post("/stop", stop_agent_handler)
    app.router.add_get("/list", list_agents_handler)
    app.router.add_get("/config", config_handler)
    app.on_shutdown.append(on_shutdown)
    return app


if __name__ == "__main__":
    app = create_app()
    LOG.info("Starting Agent Manager service on http://127.0.0.1:5556...")

    def log_unhandled(loop, context):
        LOG.error("Unhandled error in background task: %s",
                  context.get("message"), exc_info=context.get("exception"))

    async def install_handler(app):
        asyncio.get_running_loop().set_exception_handler(log_unhandled)

    
    web.run_app(app, host="127.0.0.1", port=5556)
