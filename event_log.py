import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class EventLog:
    """Append-only JSONL logs: one file per trial, plus an 'untrialed' file per room."""

    def __init__(self, log_dir="logs"):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._lock = threading.RLock()
        self._seq = {}                       # file key -> last sequence number

        self._state_path = self.dir / "active_sessios.json"
        self.active = {}                     # room -> {"session_id": ..., "round": ... }
        if self._state_path.exists():
            self.active = json.loads(self._state_path.read_text())

    # --- sessions and rounds ---------------------------------------------------------

    def active_session(self, room):
        return self.active.get(room)

    def session_exists(self, session_id):
        return (self.dir / f"{session_id}.jsonl").exists()

    def start_session(self, room, session_id, meta):
        with self._lock:
            self.active[room] = {"session_id": session_id, "round": None}
            self._save_state()
            return self.record(room, "session_start", payload=meta)

    def set_round(self, room, round_no):
        with self._lock:
            self.active[room]["round"] = round_no
            self._save_state()
        
    def end_session(self, room, payload=None):
        with self._lock:
            event = self.record(room, "session_end", payload=payload)
            self.active.pop(room, None)
            self._save_state()
            return event

    def _save_state(self):
        self._state_path.write_text(json.dumps(self.active, indent=2))

    # --- recording ------------------------------------------------------

    def _file_key(self, room):
        session = self.active.get(room)
        if session:
            return session["session_id"]
        return f"{room}_nosession_{datetime.now():%Y%m%d}"

    def _next_seq(self, key, path):
        if key not in self._seq:
            self._seq[key] = self._last_seq(path)
        self._seq[key] += 1
        return self._seq[key]

    @staticmethod
    def _last_seq(path):
        """Recover the last sequence number from an existing file (after a restart)."""
        if not path.exists():
            return 0
        last = 0
        with open(path) as f:
            for line in f:
                try:
                    last = json.loads(line)["seq"]
                except (ValueError, KeyError):
                    pass
        return last

    def record(self, room, event_type, sender=None, kind=None, payload=None):
        with self._lock:
            key = self._file_key(room)
            path = self.dir / f"{key}.jsonl"
            session = self.active.get(room) or {}
            event = {
                "seq": self._next_seq(key, path),
                "ts": datetime.now(timezone.utc).isoformat(),
                "t_mono": time.monotonic(),
                "server_run": self.run_stamp,
                "session_id": session.get("session_id"),
                "round": session.get("round"),
                "room": room,
                "type": event_type,
                "sender": sender,
                "kind": kind,
                "payload": payload or {},
            }
            with open(path, "a") as f:
                f.write(json.dumps(event) + "\n")
            return event
