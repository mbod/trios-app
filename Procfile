web: env LOG_DIR=/var/app/data/logs gunicorn --workers 1 --threads 400 --bind 127.0.0.1:8000 --timeout 0 app:app
agents: env APP_URL=http://127.0.0.1:8000 python agent_manager.py
