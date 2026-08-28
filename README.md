
# Trio Chatroom App


## Overview




## Running App

* There are two components
 
  1. Chatroom app server
     - Flask app that serves the chatroom and manages websocket traffic
  2. Agent Manager
     - Manages LLM agent connections over websockets to the chatroom app
	 

### Setup 

1. Create and activate Python virtual environment
   - For example:
     ```
     python3.13 -m venv .venv
	 source .venv/bin/activate
	 pip install -r requirements.txt
	 
	 ```
	 
2. Start two server components (each in a separate terminal with venv activated)

   1. Chatroom
      ```
	  
	  python app.py
	  
	  ```
   2. Agent manager
      ```
	  
	  python agent_manager.py
	  
	  ```
	  

### TODO - instructions for running with `gunicorn`


## TODO

### Admin dashboard

- create a 'room' (for a task) and set group config



#### Chatroom

- send (app) and receive (clients) TASK START signal
  - make sure all participants have joined 
  
- receive (app) and send (clients) TASK COMPLETE signal

- persistent log of chat

