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
import threading
from dotenv import load_dotenv
from flask_socketio import SocketIO, join_room, leave_room, emit
from event_log import EventLog
from functools import wraps
from flask import (
    Flask, request, render_template, session,
    jsonify, abort, send_from_directory,
    redirect, url_for, flash
    )
from flask import session as flask_session
from werkzeug.middleware.proxy_fix import ProxyFix 

from datetime import datetime
import logging
import requests

from pathlib import Path
from pydantic import ValidationError

from config import NAME_PATTERN, list_conditions
from tasks import TASK_DIR
from session import SESSIONS, Session

_ = load_dotenv()



ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

ROOM_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

PARTICIPANTS=['A','B','C']

STIMULUS_FILE_TYES = {".svg", ".png", ".jpg", ".txt"}

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ['FLASK_SECRET_KEY']
app.config['SESSION_COOKIE_SECURE'] = os.environ.get("SECURE_COOKIES") == "1"
app.config['SESSION_COOKIE_SAMESITE'] = "Lax"
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN')
AGENT_TOKEN = os.environ['AGENT_TOKEN']
AGENT_MANAGER_URL = os.environ.get("AGENT_MANAGER_URL", "http://127.0.0.1:5556")


START_LOCK = threading.Lock()

event_log = EventLog(os.environ.get('LOG_DIR', 'logs'))

for room in list(event_log.active):
    event_log.end_session(room, payload={"reason": "server_restart"})

logging.basicConfig(level=logging.INFO,
                    format="[AppServer]%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


socketio = SocketIO(app, async_mode="threading")

logger.info("Socket.IO async mode %s", socketio.async_mode)



# TODO: move messages data structure
#       to a persistent db
messages = []


# TODO: migrate to redis
connected_clients = {}

# create process queue and agent dictionary for agents
# agent_queue = Queue()
# agents = {}




# --- app routes --------------------------------------------------

@app.route('/health')
def health():
    return jsonify({"ok": True, "active_sessions": sorted(SESSIONS)})


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

    is_experimenter = bool(flask_session.get('admin')) and not request.args.get('user_id')
    
    return render_template('chatroom.html',
                           room=room_id,
                           participants=participants,
                           experimenter=is_experimenter
                          )




def create_session(room_id: str, params) -> tuple[dict, int]:
    """
    Validate and start a session. Used by /start_session and admin form
    """
    if not ROOM_PATTERN.match(room_id):
        return {"error": "invalid room id"}, 400
    with START_LOCK:
        if room_id in SESSIONS or event_log.active_session(room_id):
            return {"error": f"a session is already running in room {room_id}"}, 409

        session_id = params.get("session_id") or f"{room_id}-{datetime.now():%Y%m%d-%H%M%S}"
        if not ID_PATTERN.match(session_id):
            return {"error": "invalid session id"}, 400
        if event_log.session_exists(session_id):
            return {"error": f"session {session_id} already has a log file"}, 409

        try:
            overrides = {}
            if params.get("rounds"):
                overrides["rounds"] = int(params["rounds"])
            if params.get("time_limit"):
                overrides["time_limit"] = None if params["time_limit"]== "none" else float(params["time_limit"])
            meta = {k : params[k] for k in ("group_id", "notes") if params.get(k)}
            session = Session(socketio, event_log, connected_clients, room_id, session_id,
                              params.get("condition",""),
                              params.get("task",""),
                              params.get("stimuli", ""),
                              overrides, meta)
        except (ValueError, ValidationError, FileNotFoundError) as e:
            return {"error": str(e)}, 400

        if (missing := session.missing_humans()):
            return {"error": f"waiting for human participants: {missing}"}, 409


        SESSIONS[room_id] = session

    session.start()
    return {"session_id": session_id, "round_order": session.order}, 200





@app.route("/start_session/<room_id>")
def start_session(room_id):
    if (denied := require_admin()):
        return denied
    result, status = create_session(room_id, request.args)
    return jsonify(result), status



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
@app.route("/stimuli/<task>/<path:subpath>")
def stimulus_file(task, subpath):
    print(task, subpath)
    if not NAME_PATTERN.match(task) or Path(subpath).suffix not in (".svg", ".png"):
        abort(404)
    return send_from_directory(TASK_DIR / task / "stimuli", subpath)


# ----- ADMIN routes and functions -------


def require_admin():
    """
    Request most carry the admin token
    """

    if flask_session.get("admin"):
        return None
    
    token = request.args.get("token") or request.headers.get("X-Admin-Token","")
    if not ADMIN_TOKEN or not hmac.compare_digest(token, ADMIN_TOKEN):
        return jsonify({"error": "admin token required"}), 403
    return None


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not flask_session.get('admin'):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if not ADMIN_PASSWORD:
        abort(503, "Admin is diasbled: ADMIN_PASSWORD not set")
    error = None
    if request.method == "POST":
        given = request.form.get("password","").encode()
        if hmac.compare_digest(given, ADMIN_PASSWORD.encode()):
            flask_session['admin'] = True
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith("/admin") else url_for("admin_sessions"))
        error = "Wrong password"
    return render_template("admin/login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    flask_session.pop("admin", None)
    return redirect(url_for("admin_login"))


@app.route('/admin')
@admin_required
def admin_sessions():
    return render_template('admin/active_sessions.html',
                           sessions = list(SESSIONS.values()))


@app.route("/admin/start", methods=["GET", "POST"])
@admin_required
def admin_start():
    error = None
    if request.method == "POST":
        result, status = create_session(request.form.get("room_id", ""), request.form)
        if status == 200:
            flash(f"Started {result['session_id']}")
            return redirect(url_for("admin_sessions"))
        error = result["error"]
    return render_template("admin/start.html", error=error, form=request.form,
                           conditions=list_conditions())


@app.post("/admin/end_round/<room_id>")
@admin_required
def admin_end_round(room_id):
    if (s := SESSIONS.get(room_id)):
        s.request_end_round()
        flash(f"Ending round in {room_id}")
    return redirect(url_for("admin_sessions"))


@app.post("/admin/end_session/<room_id>")
@admin_required
def admin_end_session(room_id):
    if (s := SESSIONS.get(room_id)):
        s.request_end_session()
        flash(f"Ending session in {room_id}")
    return redirect(url_for("admin_sessions"))



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
        if user_id:
            indentity = {'id': user_id, 'room': auth.get('room'), 'kind': 'human'}
        elif flask_session.get('admin'):
            identity = {'id': 'Experimenter', 'room': auth.get('room'), 'kind': 'experimenter'}
        else:
            identity = {'id': f'observer-{sid[:6]}', 'room': auth.get('room'), 'kind': 'observer'}

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
        emit('session_state', session.public_state(identity['id']))
    
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
    client = connected_clients.get(request.sid)
    if not client or client['kind'] == 'observer':
        return
    payload = payload or {}
    text = str(payload.get('message', '')).strip()
    if not text:
        return

    session = SESSIONS.get(client['room'])
    if session and not session.round_open():
        event_log.record(client['room'], "message_rejected", sender=client['id'],
                         kind=client['kind'], payload={"message": text, "reason": "between_rounds"})
        emit("message_rejected", {"error": "The next round hasn't started yet"})
        return

    lines = text.splitlines()
    commands = [l.strip() for l in lines if l.strip().startswith('/')]
    chat = "\n".join(l for l in lines if not l.strip().startswith('/')).strip()

    if chat:
        logged = {'message': chat, 'turn_id': payload.get('turn_id')}
        if isinstance(payload.get('context_seq'), int):
            logged['context_seq'] = payload['context_seq']
        event = event_log.record(client['room'], 'message', sender=client['id'],
                                 kind=client['kind'], payload=logged)
        if session and client['kind'] != 'experimenter':
            session.on_message()
        emit('message', {'from': client['id'], 'message': chat,
                         'seq': event['seq'], 'ts': event['ts']}, to=client['room'])

    if commands and session and client['kind'] in ('human', 'agent'):
        for command in commands:
            session.on_command(client['id'], client['kind'], command)



        
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

    sender = client['id']

    event_log.record(client['room'], 'typing',
                     sender=sender, kind=client['kind'],
                     payload={'state': state})

    emit('typing', {'from': sender, 'state': state},
         to=client['room'], include_self=False)
                                            

@socketio.on('selection')
def handle_selection(data):
    client = participant(request.sid)
    session = SESSIONS.get(client["room"]) if client else None
    if not session:
        return
    error = session.on_selection(client["id"], client["kind"],
                                 (data or {}).get("selection"))
    if error:
        emit("selection_error", {"error": error})
    
    
@socketio.on('task_begin')
def handle_begin_task(payload):
    '''Signal that task has begin and participants can begin'''
    pass



@socketio.on("task_action")
def handle_task_action(data):
    client = participant(request.sid)
    session = SESSIONS.get(client["room"]) if client else None
    if not session:
        return
    error = session.on_action(client["id"], client["kind"], data or {})
    if error:
        emit("action_error", {"error": error})
        


@socketio.on("task_complete")
def handle_task_complete(data):
    client = participant(request.sid)
    session = SESSIONS.get(client["room"]) if client else None
    if not session:
        return
    data = data or {}
    error = session.on_status(client["id"], client["kind"],
                              bool(data.get("done")), data.get("answer"))
    if error:
        emit("status_error", {"error": error})
        
        
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
