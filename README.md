
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
	  

### TODO - instructions for running with `gunicorn`


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
