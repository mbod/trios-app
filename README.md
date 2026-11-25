
# Trio Chatroom App

Matthew Brook O'Donnell and Marlon Twyman

## Overview

This app is a part of a larger research project focused on communication patterns of three member groups (trios) collaborating to complete tasks. Trios can consist of:

1. All humans
2. All agents (LLMs)
3. Hybrid teams
   - 2 LLMs + 1 human
   - 2 humans + 1 LLM 

The app implements a web-based chatroom to facilitate communication between trio members as they work to complete tasks.



- [Poster Abstract](abstract.md) for [NSF AI-SDM Workshop on Human-AI Complementarity for Decision Making](https://www.cmu.edu/ai-sdm/research/human-ai-workshop/index.html) (Sept 24-25, 2026 @ CMU)



- [Some conversation logs](chatlogs)


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
	  

### Running with `gunicorn` (AWS Elastic Beanstalk)

The app deploys to the Elastic Beanstalk platform *Python 3.13 running on 64bit Amazon Linux 2023*. Beanstalk installs `requirements.txt` and starts the two processes listed in the [`Procfile`](Procfile):

- `web`: gunicorn serving `app:app` on `127.0.0.1:8000`. Beanstalk's nginx proxies to this port, including websocket upgrades.
- `agents`: `agent_manager.py` on `127.0.0.1:5556`. It connects agents directly to gunicorn on port 8000.

Constraints:

- **Use one worker and one instance.** Session state is held in the web process's memory, so gunicorn runs `--workers 1` (threads handle concurrency), and the environment must not scale past a single instance.
- **Session logs are stored at `/var/app/data/logs`**, outside the app directory, so that redeploys don't wipe them (see [`.platform/hooks/prebuild/01_log_dir.sh`](.platform/hooks/prebuild/01_log_dir.sh)). They do not survive instance replacement.
- **Redeploying ends any running sessions.** Check `/health` for `active_sessions` before deploying.
- The load balancer health check uses `/health` (see [`.ebextensions/01-health-check.config`](.ebextensions/01-health-check.config)).

Set these environment properties on the Beanstalk environment (see [`.env.example`](.env.example)): `FLASK_SECRET_KEY`, `AGENT_TOKEN`, `ADMIN_PASSWORD`, `ADMIN_TOKEN`, `SECURE_COOKIES=1` (when served over HTTPS), and the API keys for the model providers in use.

To run the same way locally (with a `.env` file), start each process in its own terminal:

```
gunicorn --workers 1 --threads 400 --bind 127.0.0.1:8000 --timeout 0 app:app
APP_URL=http://127.0.0.1:8000 python agent_manager.py
```


## TODO

### Admin dashboard

- create a 'room' (for a task) and set group config



#### Chatroom

- send (app) and receive (clients) TASK START signal
  - make sure all participants have joined 
  
- receive (app) and send (clients) TASK COMPLETE signal

- persistent log of chat



----

## Configuration

### Models

Testing with a range of openai models initially
TODO: generalize async handler to work with any LLM

OpenAI model ids for testing

- gpt-4o-2024-11-20
- gpt-5-nano
- gpt-4o-mini
- gpt-4.1-nano
- gpt-5.6-luna
- gpt-5.4-nano

- claude-haiku-4-5-20251001
