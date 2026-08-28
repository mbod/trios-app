# agent_manager.py
import asyncio
import logging
import os
from aiohttp import web
from client_ws import Client, SK

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [AgentManager] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
LOG = logging.getLogger(__name__)

# Registry: (room_id, agent_id) -> {"task": asyncio.Task, "client": Client}
RUNNING_AGENTS = {}

BASE_WS_URL = "http://127.0.0.1:5555"
#SOCKETIO_PATH = "/services/trios-app/socket.io"
SOCKETIO_PATH = "/socket.io"

async def start_agent_handler(request):
    """Spawns an agent inside the async event loop as a background task."""
    data = await request.json()
    agent_id = data.get("agent_id")
    room_id = data.get("room_id")

    if not agent_id or not room_id:
        return web.json_response({"error": "agent_id and room_id required"}, status=400)

    key = (room_id, agent_id)
    if key in RUNNING_AGENTS and not RUNNING_AGENTS[key]["task"].done():
        return web.json_response({
            "status": "already_running",
            "message": f"Agent {agent_id} is already running in room {room_id}"
        })

    client = Client(
        id=agent_id,
        room=room_id,
        image_file=f"image{agent_id}_nocomments.svg",
        ws_url=BASE_WS_URL,
        socketio_path=SOCKETIO_PATH
    )

    auth = {
        "room": room_id,
        "sid": agent_id,
        "token": SK
    }

    # Spawn directly in the existing asyncio event loop
    task = asyncio.create_task(client.run(auth))
    RUNNING_AGENTS[key] = {"task": task, "client": client}

    LOG.info(f"Spawned agent task for Agent {agent_id} in Room {room_id}")
    return web.json_response({
        "status": "started",
        "agent_id": agent_id,
        "room_id": room_id
    })


async def stop_agent_handler(request):
    """Gracefully cancels a running agent task."""
    data = await request.json()
    agent_id = data.get("agent_id")
    room_id = data.get("room_id")

    key = (room_id, agent_id)
    agent_info = RUNNING_AGENTS.pop(key, None)

    if not agent_info:
        return web.json_response({"error": "Agent not found or already stopped"}, status=404)

    task = agent_info["task"]
    client = agent_info["client"]

    # Disconnect websocket and cancel async task
    try:
        if client.sio.connected:
            await client.sio.disconnect()
    except Exception as e:
        LOG.warning(f"Error disconnecting client {agent_id}: {e}")

    task.cancel()
    LOG.info(f"Stopped agent {agent_id} in Room {room_id}")
    return web.json_response({"status": "stopped", "agent_id": agent_id, "room_id": room_id})


async def list_agents_handler(request):
    """Returns all currently active agents."""
    active = [
        {"room_id": r, "agent_id": a, "done": info["task"].done()}
        for (r, a), info in RUNNING_AGENTS.items()
    ]
    return web.json_response({"active_agents": active})


def create_app():
    app = web.Application()
    app.router.add_post("/start", start_agent_handler)
    app.router.add_post("/stop", stop_agent_handler)
    app.router.add_get("/list", list_agents_handler)
    return app


if __name__ == "__main__":
    app = create_app()
    LOG.info("Starting Agent Manager service on http://127.0.0.1:5556...")
    web.run_app(app, host="127.0.0.1", port=5556)
