import json
import threading
from datetime import datetime, timezone
from pathlib import Path


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UsageStatsStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def defaults(self):
        return {
            "last_cleaned_at": None,
            "last_printed_at": None,
            "prints_since_cleaning": 0,
            "total_prints": 0,
            "total_cleanings": 0,
        }

    def load(self):
        with self._lock:
            return self._load_locked()

    def save(self, payload):
        with self._lock:
            data = self._normalize(payload)
            self._write_locked(data)
            return dict(data)

    def record_print_success(self):
        with self._lock:
            data = self._load_locked()
            data["last_printed_at"] = _utc_now_iso()
            data["total_prints"] += 1
            data["prints_since_cleaning"] += 1
            self._write_locked(data)
            return dict(data)

    def record_cleaning_success(self):
        with self._lock:
            data = self._load_locked()
            data["last_cleaned_at"] = _utc_now_iso()
            data["prints_since_cleaning"] = 0
            data["total_cleanings"] += 1
            self._write_locked(data)
            return dict(data)

    def _load_locked(self):
        if not self.path.exists():
            data = self.defaults()
            self._write_locked(data)
            return data
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        data = self._normalize(payload)
        if data != payload:
            self._write_locked(data)
        return data

    def _write_locked(self, payload):
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _normalize(self, payload):
        defaults = self.defaults()
        raw = payload if isinstance(payload, dict) else {}
        normalized = dict(defaults)
        normalized["last_cleaned_at"] = raw.get("last_cleaned_at") or None
        normalized["last_printed_at"] = raw.get("last_printed_at") or None
        normalized["prints_since_cleaning"] = max(0, int(raw.get("prints_since_cleaning", 0) or 0))
        normalized["total_prints"] = max(0, int(raw.get("total_prints", 0) or 0))
        normalized["total_cleanings"] = max(0, int(raw.get("total_cleanings", 0) or 0))
        return normalized
