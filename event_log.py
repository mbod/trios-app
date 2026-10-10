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

        self._state_path = self.dir / "active_trials.json"
        self.active = {}                     # room -> trial info
        if self._state_path.exists():
            self.active = json.loads(self._state_path.read_text())

    # --- trials ---------------------------------------------------------

    def active_trial(self, room):
        return self.active.get(room)

    def trial_exists(self, trial_id):
        return (self.dir / f"{trial_id}.jsonl").exists()

    def start_trial(self, room, trial_id, meta):
        with self._lock:
            self.active[room] = {"trial_id": trial_id, **meta}
            self._save_state()
            return self.record(room, "trial_start", payload=meta)

    def end_trial(self, room):
        with self._lock:
            event = self.record(room, "trial_end")
            self.active.pop(room, None)
            self._save_state()
            return event

    def _save_state(self):
        self._state_path.write_text(json.dumps(self.active, indent=2))

    # --- recording ------------------------------------------------------

    def _file_key(self, room):
        trial = self.active.get(room)
        if trial:
            return trial["trial_id"]
        return f"{room}_untrialed_{datetime.now():%Y%m%d}"

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
            trial = self.active.get(room) or {}
            event = {
                "seq": self._next_seq(key, path),
                "ts": datetime.now(timezone.utc).isoformat(),
                "t_mono": time.monotonic(),
                "server_run": self.run_stamp,
                "trial_id": trial.get("trial_id"),
                "room": room,
                "type": event_type,
                "sender": sender,
                "kind": kind,
                "payload": payload or {},
            }
            with open(path, "a") as f:
                f.write(json.dumps(event) + "\n")
            return event
