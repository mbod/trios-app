"""Start the chat server and the agent manager together.

    python run.py

Ctrl+C stops both. If either one stops on its own, the other is stopped too.
"""
import subprocess
import sys
import time

processes = [
    subprocess.Popen([sys.executable, "app.py"]),
    subprocess.Popen([sys.executable, "agent_manager.py"]),
]

try:
    while all(p.poll() is None for p in processes):
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
finally:
    for p in processes:
        if p.poll() is None:
            p.terminate()
    for p in processes:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
