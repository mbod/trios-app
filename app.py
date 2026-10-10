# flask app for web socket chatroom 
# allow WS clients to join and 
# 1. listen to stream of messages
# 2. post messages
#
# stream of messages captured in sequence
#

import os
import re
from dotenv import load_dotenv
from flask_socketio import SocketIO, join_room, leave_room, emit
from event_log import EventLog

from flask import Flask, request, render_template, session, jsonify
from datetime import datetime
import logging
import requests



from config import load_experiment, snapshot

_ = load_dotenv()

ROOM_PATTERN = re.compile(r"^[A-z0-9_-]{1,32}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SESSION_META_FIELDS = ("session_id", "task", "imagepath", "condition", "notes")

PARTICIPANTS=['A','B','C']

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ['FLASK_SECRET_KEY']
AGENT_TOKEN = os.environ['AGENT_TOKEN']
event_log = EventLog(os.environ.get('LOG_DIR', 'logs'))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


socketio = SocketIO(app, logger=True, engineio_logger=True)




# TODO: move messages data structure
#       to a persistent db
messages = []


# TODO: migrate to redis
connected_clients = {}

# create process queue and agent dictionary for agents
# agent_queue = Queue()
# agents = {}


AGENT_MANAGER_URL = "http://127.0.0.1:5556"



# --- app routes --------------------------------------------------

@app.route('/chatroom', defaults={'room_id': 'test'})
@app.route('/chatroom/<room_id>')
def chatroom(room_id):
    '''
    Chatroom view
    '''
    if not ROOM_PATTERN.match(room_id):
        return "Invalid room_id", 400
    
    session.clear()
    session['room'] = room_id

    user_id = request.args.get('user_id')

    participants = [user_id] if user_id else ['A','B','C']
    
    
    imagepath = request.args.get('imagepath', 'image')
        
    return render_template('chatroom.html',
                           room=room_id,
                           participants=participants,
                           imagepath = imagepath
                           )



@app.route('/')
def create_room():

    return render_template('create_room.html')



def require_user():
    return {'user': True}


@app.route("/start_session/<room_id>")
def start_trial(room_id):
    auth_check = require_user()
    if not isinstance(auth_check, dict):
        return auth_check

    if not ROOM_PATTERN.match(room_id):
        return jsonify({"error": "invalid room id"}), 400

    if event_log.active_session(room_id):
        return jsonify({"error": f"room {room_id} already has an active trial",
                        "active": event_log.active_session(room_id)}), 409

    session_id = request.args.get("session_id") or f"{room_id}-{datetime.now():%Y%m%d-%H%M%S}"
    if not ID_PATTERN.match(session_id):
        return jsonify({"error": "invalid session id"}), 400
    if event_log.session_exists(session_id):
        return jsonify({"error": f"session {session_id} already has a log file"}), 409

    meta = {k: request.args[k] for k in SESSION_META_FIELDS if request.args.get(k)}
    meta["roster"] = [{"id": c["id"], "kind": c["kind"]}
                      for c in connected_clients.values() if c["room"] == room_id]

    try:
        r = requests.get(f"{AGENT_MANAGER_URL}/config",
                         params={"room_id": room_id}, timeout=3)
        meta["agent_configs"] = r.json().get("agents", {})
    except requests.exceptions.ConnectionError:
        meta["agent_configs"] = {}

    event = event_log.start_session(room_id, session_id, meta)
    socketio.emit("session_start", { "session_id": session_id, **meta}, to=room_id)

    return jsonify(event)


@app.route("/end_session/<room_id>")
def end_trial(room_id):
    auth_check = require_user()
    if not isinstance(auth_check, dict):
        return auth_check

    if not event_log.active_session(room_id):
        return jsonify({"error": f"no active session in room {room_id}"}), 404

    event = event_log.end_session(room_id)
    socketio.emit("session_end", {"session_id": event["session_id"]}, to=room_id)
    return jsonify(event)


@app.route("/start_round/<room_id>/<int:round_no>")
def start_round(room_id, round_no):
    if not event_log.active_session(room_id):
        return jsonify({"error": f"no active session in room {room_id}"}), 404

    try:
        event_log.set_round(room_id, round_no)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    
    event = event_log.record(room_id, "round_start", payload={"round": round_no})
    socketio.emit("round_start", {"round": round_no, "seq": event["seq"]}, to=room_id)
    return jsonify(event)


@app.route("/end_round/<room_id>")
def end_round(room_id):
    session = event_log.active_session(room_id)
    if not session or session["round"] is None:
        return jsonify({"error": f"no active round in room {room_id}"}), 404

    event = event_log.record(room_id, "round_end",
                             payload={"round": session["round"],
                                      "reason": "experimenter"})
    event_log.set_round(room_id, None)
    socketio.emit("round_end", {"round": event["round"],
                                "reason": "experimenter"}, to=room_id)
    return jsonify(event)



#@app.route(f"{prefix.rstrip('/')}/add_agent/<agent_id>/to/<room_id>")
@app.route("/add_agent/<agent_id>/to/<room_id>")
def add_agent(agent_id, room_id):
    auth_check = require_user()
    if not isinstance(auth_check, dict):
        return auth_check



    imagepath = request.args.get('imagepath', 'image')

    
    # Delegate spawning to the Agent Manager daemon
    try:
        r = requests.post(
            f"{AGENT_MANAGER_URL}/start",
            json={"agent_id": agent_id,
                  "room_id": room_id,
                  "image_path": imagepath,
                  "experiment": request.args.get("experiment"),
                  "model": request.args.get("model")
                  },
            timeout=3
        )
        return jsonify(r.json()), r.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Agent manager service is not running on port 5556"}), 503


#@app.route(f"{prefix.rstrip('/')}/stop_agent/<agent_id>/from/<room_id>")
@app.route("/stop_agent/<agent_id>/from/<room_id>")
def stop_agent(agent_id, room_id):
    auth_check = require_user()
    if not isinstance(auth_check, dict):
        return auth_check

    try:
        r = requests.post(
            f"{AGENT_MANAGER_URL}/stop",
            json={"agent_id": agent_id, "room_id": room_id},
            timeout=3
        )
        return jsonify(r.json()), r.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Agent manager service is not running on port 5556"}), 503

@app.route("/list_agents")
def list_agents():
    auth_check = require_user()
    if not isinstance(auth_check, dict):
        return auth_check

    try:
        r = requests.get(
            f"{AGENT_MANAGER_URL}/list",
            timeout=3
        )
        return jsonify(r.json()), r.status_code
    except request.exceptions.ConnectionError:
        return jsonify({"error": "Agent manager service is not running on port 5556"}), 503
        


# ----- SOCKETIO handlers -----------------------------------------

@socketio.on('connect')
def handle_connect(auth):
    '''
    WS client connecting to room
    '''

    auth = auth or {}
    sid = request.sid
    

    logger.info(f'Connection request: {sid} {list(auth)}')

    
    if auth.get('token') == AGENT_TOKEN:
        identity = { 'id': auth.get('agent_id') or auth.get('sid'),
                     'room': auth.get('room'),
                     'kind': 'agent'
                    }

        if not identity['id']:
            return False

    else:
        user_id = auth.get('user_id')
        identity = {'id': user_id or f'observer-{sid[:6]}',
                    'room': auth.get('room') or session.get('room'),
                    'kind': 'human' if user_id else 'observer'}

    room = identity["room"]

    if not room or not ROOM_PATTERN.match(room):
        logger.warning(f"Rejected sid={sid}: invalid room {room}")
        return False

    connected_clients[sid] = identity
    
    logger.info(f'Connected clients: {connected_clients}')

    # join room
    join_room(room)

    event_log.record(room, 'join',
                     sender=identity['id'],
                     kind=identity['kind'])
                     


@socketio.on('disconnect')
def handle_disconnect(reason=None):
    '''
    WS client leaves a room
    '''
    client = connected_clients.pop(request.sid, None)
    if client:
        leave_room(client['room'])
        event_log.record(client['room'], 'leave',
                         sender=client['id'],
                         kind=client['kind'])
    

@socketio.on('message')
def handle_message(payload):

    logger.info('MESSAGE - ', payload, request.sid)

    client = connected_clients.get(request.sid)

    if not client:
        return

    payload = payload or {}
    text = str(payload.get('message','')).strip()

    if not text:
        return


    logged = {'message': text}
    context_seq = payload.get('context_seq')

    if isinstance(context_seq, int):
        logged['context_seq'] = context_seq

    
    # message
    sender = client['id'] or payload['from'] 
    event = event_log.record(client['room'], 'message',
                             sender=sender,
                             kind=client['kind'],
                             payload=logged)
    
    
    emit('message', { 'from': sender, 'message': text,
                      'seq': event['seq'], 'ts': event['ts']},
         to=client['room'])

@socketio.on('typing')
def handle_type(payload):
    '''
    event for user typing or agent simulated typing
    '''
    client = connected_clients.get(request.sid)

    if not client:
        return

    payload = payload or {}
    state = payload.get('state')      # start | stop
    if state not in ('start', 'stop'):
        return

    sender = payload['from']

    event_log.record(client['room'], 'typing',
                     sender=sender, kind=client['kind'],
                     payload={'state': state})

    emit('typing', {'from': sender, 'state': state},
         to=client['room'], include_self=False)
                                            

    
    
@socketio.on('task_begin')
def handle_begin_task(payload):
    '''Signal that task has begin and participants can begin'''
    pass
    
@socketio.on('task_complete')
def handle_end_task(payload):
    '''Handle participant signal that they think task is complete'''

    logger.info(f'TASK COMPLETE: {payload} - request_id: {request.sid}')

    client = connected_clients.get(request.sid)

    if not client:
        return

    payload = payload or {}
    sender = client['id']  # removed - payload['from'] 
    
    logger.info(f'Client {sender} has sent a TASK COMPLETE signal')

    event = event_log.record(client['room'], 'task_complete',
                             sender=sender, kind=client['kind'])
    
    emit('task_complete', {'from': sender, 'seq': event['seq'],
                           'ts': event['ts']},
         to=client['room'])
                                                        

# --- agent traces are ws events but NOT broadcast
@socketio.on('agent_trace')
def handle_agent_trace(payload):
    client = connected_clients.get(request.sid)
    if not client or client['kind'] != 'agent':
        return

    event_log.record(client['room'], 'agent_trace',
                     sender=client['id'], kind='agent',
                     payload=payload or {})

    

    
if __name__ == "__main__":


    # NOTE for deployment
    # turn logging off socketio = SocketIO(app)
    
    socketio.run(app, port=5555, debug=True)
