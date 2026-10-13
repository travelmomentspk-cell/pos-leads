"""Small JSON files that remember what was already seen, so the same business is never exported twice."""
import json
from pathlib import Path


class State:
    def __init__(self, folder):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.seen_path = self.dir / "seen.json"
        self.rot_path = self.dir / "rotation.json"
        self.seen = set(self._load(self.seen_path, []))
        self.rotation = self._load(self.rot_path, {"next_index": 0})
        self.backlog_path = self.dir / "backlog.json"
        self.backlog = self._load(self.backlog_path, [])

    @staticmethod
    def _load(path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default

    @staticmethod
    def keys_for(place_id, phone, website_domain):
        keys = []
        if place_id:
            keys.append(f"place:{place_id}")
        if phone:
            keys.append(f"phone:{phone}")
        if website_domain:
            keys.append(f"domain:{website_domain}")
        return keys

    def is_seen(self, keys):
        return any(k in self.seen for k in keys)

    def mark(self, keys):
        self.seen.update(keys)

    def save(self):
        self.seen_path.write_text(json.dumps(sorted(self.seen)), encoding="utf-8")
        self.rot_path.write_text(json.dumps(self.rotation), encoding="utf-8")
        self.backlog_path.write_text(json.dumps(self.backlog, ensure_ascii=False), encoding="utf-8")
