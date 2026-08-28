# flask app for web socket chatroom 
# allow WS clients to join and 
# 1. listen to stream of messages
# 2. post messages
#
# stream of messages captured in sequence
#


from flask import Flask, request, render_template, session, url_for, jsonify
from flask_socketio import SocketIO, join_room, leave_room, send
import logging
from  multiprocessing import Queue, Process
from uuid import uuid4
from collections import defaultdict

import requests


from client_ws import run_agent


PARTICIPANTS=['A','B','C']

app = Flask(__name__)
app.config['SECRET_KEY'] = 'coll@bplan!'

socketio = SocketIO(app)

logger = logging.getLogger(__name__)


# TODO: move messages data structure
#       to a persistent db
messages = []

rooms = defaultdict(list)

# TODO: migrate to redis
connected_clients = {}

# create process queue and agent dictionary for agents
agent_queue = Queue()
agents = {}


AGENT_MANAGER_URL = "http://127.0.0.1:5556"



# --- app routes --------------------------------------------------

@app.route('/chatroom')
def chatroom():
    '''
    Chatroom view
    '''

    session.clear()
    session['room'] = 'test'
    
    room = session.get('room')

    if request.args.get('user_id', False):
        PARTICIPANTS = [ request.args.get('user_id') ]
    else:
        PARTICIPANTS = ['A','B','C']
    
    return render_template('chatroom.html',
                           room=room,
                           participants=PARTICIPANTS)



@app.route('/')
def create_room():

    return render_template('create_room.html')



"""
@app.route('/add_agent/<agent_id>/to/<room_id>')
def add_agent(agent_id, room_id):

    # check room_id to see if agent already in room
    if agent_id in rooms.get(room_id, []):
        return f"Agent {agent_id} already in Room {room_id}"
    
    # Generate a unique task ID
    task_id = str(uuid4())

    rooms[room_id].append(agent_id)
    agents[task_id] = {"status": "processing", "result": None}

    # Start a child process to agent
    process = Process(
        target=run_agent,
        args=(agent_id, room_id)  
    )
    process.start()
    process.join()
 
    return f"Agent {agent_id} started in Room {room_id} with TaskID {task_id}"
"""

def require_user():
    return {'user': True}

#@app.route(f"{prefix.rstrip('/')}/add_agent/<agent_id>/to/<room_id>")
@app.route("/add_agent/<agent_id>/to/<room_id>")
def add_agent(agent_id, room_id):
    auth_check = require_user()
    if not isinstance(auth_check, dict):
        return auth_check

    # Delegate spawning to the Agent Manager daemon
    try:
        r = requests.post(
            f"{AGENT_MANAGER_URL}/start",
            json={"agent_id": agent_id, "room_id": room_id},
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

                           


# ----- SOCKETIO handlers -----------------------------------------

@socketio.on('connect')
def handle_connect(auth):
    '''
    WS client connecting to room
    '''
    print('CONNECTION ATTEMPT', auth)
    logger.info(f'Connection request: {auth}')

    sid = request.sid
    
    if auth and auth.get('token') == app.config['SECRET_KEY']:
        connected_clients[sid] = {
            'client': sid,
            'room': auth.get('room')
        }

    else:
        if not session.get('room'):
            return False

        connected_clients[sid] = {
            'client': sid,
            'room': 'test'
        }
    
    logger.info(f'Connected clients: {connected_clients}')

    # join room
    join_room(connected_clients[sid]['room'])


@socketio.on('disconnect')
def handle_disconnect():
    '''
    WS client leaves a room
    '''
    room = session.get('room')
    leave_room(room)

@socketio.on('message')
def handle_message(payload):

    print('MESSAGE - ', payload, request.sid)

    client = connected_clients.get(request.sid)
    print(client)
    #if not client:
    #    return
    
    room = client['room'] # session.get('room')
    
    # message
    sender = payload['from']
    message = payload['message']    

    logger.info(f'Sending {payload} to {room}')
    send(payload, to=room)

@socketio.on('task_begin')
def handle_begin_task(payload):
    '''Signal that task has begin and participants can begin'''
    pass
    
@socketio.on('task_complete')
def handle_end_task(payload):
    '''Handle participant signal that they think task is complete'''
    pass

    
    
if __name__ == "__main__":

    socketio.run(app, port=5555, debug=True)
