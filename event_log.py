

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class EventLog:
    """
    Append-only JSONL log
    Creates one file per room per server run
    """

    def __init__(self, log_dir="logs"):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._seq={}
        self._lock = threading.Lock()


    def record(self, room, event_type, sender=None, kind=None, payload=None):
        with self._lock:
            seq = self._seq.get(room, 0) + 1
            self._seq[room]=seq
            event = {
                "seq": seq,
                "ts": datetime.now(timezone.utc).isoformat(),
                "t_mono": time.monotonic(),
                "room": room,
                "type": event_type,
                "sender": sender,
                "kind": kind,    # human or agent
                "payload": payload or {},
            }

            path = self.dir / f"{room}_{self.run_stamp}.jsonl"
            with open(path, "a") as f:
                f.write(json.dumps(event) + "\n")

        return event
