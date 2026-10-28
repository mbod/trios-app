# flask app for web socket chatroom 
# allow WS clients to join and 
# 1. listen to stream of messages
# 2. post messages
#
# stream of messages captured in sequence
#

import os
import re
import hmac
from dotenv import load_dotenv
from flask_socketio import SocketIO, join_room, leave_room, emit
from event_log import EventLog

from flask import Flask, request, render_template, session, jsonify, abort, send_from_directory
from datetime import datetime
import logging
import requests

from pathlib import Path
from pydantic import ValidationError

from config import NAME_PATTERN
from tasks import TASK_DIR
from session import SESSIONS, Session

_ = load_dotenv()

ROOM_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

PARTICIPANTS=['A','B','C']

STIMULUS_FILE_TYES = {".svg", ".png", ".jpg", ".txt"}

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ['FLASK_SECRET_KEY']

ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN')
AGENT_TOKEN = os.environ['AGENT_TOKEN']


event_log = EventLog(os.environ.get('LOG_DIR', 'logs'))

for room in list(event_log.active):
    event_log.end_session(room, payload={"reason": "server_restart"})

logging.basicConfig(level=logging.INFO,
                    format="[AppServer]%(asctime)s [%(levelname)s] %(message)s")
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


def require_admin():
    """
    Request most carry the admin token
    """
    token = request.args.get("token") or request.headers.get("X-Admin-Token","")
    if not ADMIN_TOKEN or not hmac.compare_digest(token, ADMIN_TOKEN):
        return jsonify({"error": "admin token required"}), 403
    return None


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
    
    
    return render_template('chatroom.html',
                           room=room_id,
                           participants=participants,
                          )



@app.route('/')
def create_room():

    return render_template('create_room.html')




@app.route("/start_session/<room_id>")
def start_session(room_id):
    if (denied := require_admin()):
        return denied
    args = request.args
    if not ROOM_PATTERN.match(room_id):
        return jsonify({"error": "invalid room id"}), 400
    if room_id in SESSIONS or event_log.active_session(room_id):
        return jsonify({"error": f"a session is already running in room {room_id}"}), 409

    session_id = args.get("session_id") or f"{room_id}-{datetime.now():%Y%m%d-%H%M%S}"
    if not ID_PATTERN.match(session_id):
        return jsonify({"error": "invalid session id"}), 400
    if event_log.session_exists(session_id):
        return jsonify({"error": f"session {session_id} already has a log file"}), 409

    try:
        overrides = {}
        if "rounds" in args:
            overrides["rounds"] = int(args["rounds"])
        if "time_limit" in args:
            overrides["time_limit"] = None if args["time_limit"] == "none" else float(args["time_limit"])
        meta = {k: args[k] for k in ("group_id", "notes") if args.get(k)}
        session = Session(socketio, event_log, connected_clients, room_id, session_id,
                          args.get("condition", ""), args.get("task", ""),
                          args.get("stimuli", ""), overrides, meta)
    except (ValueError, ValidationError, FileNotFoundError) as e:
        return jsonify({"error": str(e)}), 400

    if (missing := session.missing_humans()):
        return jsonify({"error": f"waiting for human participants: {missing}"}), 409

    SESSIONS[room_id] = session
    session.start()
    return jsonify({"session_id": session_id, "round_order": session.order})



@app.route("/end_round/<room_id>")
def end_round(room_id):
    if (denied := require_admin()):
        return denied
    if not (session := SESSIONS.get(room_id)):
        return jsonify({"error": f"no session running in room {room_id}"}), 404
    session.request_end_round()
    return jsonify({"status": "ending round"})


@app.route("/end_session/<room_id>")
def end_session(room_id):
    if (denied := require_admin()):
        return denied
    if not (session := SESSIONS.get(room_id)):
        return jsonify({"error": f"no session running in room {room_id}"}), 404
    session.request_end_session()
    return jsonify({"status": "ending session"})






#@app.route(f"{prefix.rstrip('/')}/add_agent/<agent_id>/to/<room_id>")
@app.route("/add_agent/<agent_id>/to/<room_id>")
def add_agent(agent_id, room_id):
    if (denied := require_admin()):
        return denied    

    if not isinstance(auth_check, dict):
        return auth_check


    # Delegate spawning to the Agent Manager daemon
    try:
        r = requests.post(
            f"{AGENT_MANAGER_URL}/start",
            json={"agent_id": agent_id,
                  "room_id": room_id,
                  "stimuli": request.args.get("stimuli"),
                  "round": request.args.get("round"),
                  "condition": request.args.get("condition"),
                  "task": request.args.get("task"),
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

    if (denied := require_admin()):
        return denied
    
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
        

# serve stimuli
@app.route('/stimuli/<task>/<stim_set>/<round_id>/<filename>')
def stimulus_file(task, stim_set, round_id, filename):
    if not all(NAME_PATTERN.match(part) for part in (task, stim_set, round_id)):
        abort(404)

    if Path(filename).suffix.lower() not in STIMULUS_FILE_TYES:
        abort(404)

    return send_from_directory(TASK_DIR / task / "stimuli" / stim_set / round_id, filename)
    

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


    
    logger.info(f'Connected clients: {connected_clients}')

    # join room
    join_room(room)

    event_log.record(room, 'join',
                     sender=identity['id'],
                     kind=identity['kind'])
                     
    connected_clients[sid] = identity

    if (session := SESSIONS.get(room)):
        emit('session_state', session.public_state())
    
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
    

    if (session := SESSIONS.get(client['room'])):
        session.on_message()
    
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


"""
@socketio.on('task_complete')
def handle_end_task(payload):
    '''Handle participant signal that they think task is complete'''    
    client = participant(request.sid)
    if not client or not (session := SESSIONS.get(client['room'])):
        return
    payload = payload or {}
    error = session.on_status(client['id'], client['kind'],
                              done=payload.get('done', True),
                              answer=payload.get('answer'))
    if error:
        emit('status_error', {'error': error})                                   """           



@socketio.on('task_complete')
def handle_task_complete(payload):
    logger.info("task_complete from sid=%s payload=%s", request.sid, payload)
    client = participant(request.sid)
    if not client:
        logger.warning("task_complete: sender is not a participant: %s",
                       connected_clients.get(request.sid))
        return
    session = SESSIONS.get(client['room'])
    if not session:
        logger.warning("task_complete: no session in room %s; running: %s",
                       client['room'], list(SESSIONS))
        return
    payload = payload or {}
    error = session.on_status(client['id'], client['kind'],
                              done=payload.get('done', True),
                              answer=payload.get('answer'))
    logger.info("task_complete: on_status returned %r", error)
    if error:
        emit('status_error', {'error': error})
        

        
# --- agent traces are ws events but NOT broadcast
@socketio.on('agent_trace')
def handle_agent_trace(payload):
    client = connected_clients.get(request.sid)
    if not client or client['kind'] != 'agent':
        return

    event_log.record(client['room'], 'agent_trace',
                     sender=client['id'], kind='agent',
                     payload=payload or {})



def participant(sid):
    """The connected client if it may send events (human or agent); None for observers."""
    client = connected_clients.get(sid)
    if client and client['kind'] in ('human', 'agent'):
        return client
    return None    

    
if __name__ == "__main__":


    # NOTE for deployment
    # turn logging off socketio = SocketIO(app)
    
    socketio.run(app, port=5555, debug=True, use_reloader=False)
