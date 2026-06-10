"""Simple persistent cache with TTL and stale fallback support."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional


class CacheStore:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(
        self,
        key: str,
        max_age_seconds: Optional[int] = None,
        allow_stale: bool = False,
    ) -> tuple[dict[str, Any], bool] | None:
        path = self._path_for_key(key)
        if not path.exists():
            return None

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        created_at = payload.get("created_at", 0)
        age_seconds = time.time() - float(created_at)
        is_stale = False
        if max_age_seconds is not None and age_seconds > max_age_seconds:
            is_stale = True

        if is_stale and not allow_stale:
            return None
        return payload, is_stale

    def set(self, key: str, value: dict[str, Any]) -> None:
        path = self._path_for_key(key)
        payload = {
            "created_at": time.time(),
            "data": value,
        }
        temp_path = path.with_suffix(f".tmp.{uuid.uuid4().hex}")
        temp_path.write_text(json.dumps(payload), encoding="utf-8")
        temp_path.replace(path)

    def clear(self) -> None:
        for item in self.cache_dir.glob("*.json"):
            try:
                item.unlink()
            except OSError:
                continue

    def _path_for_key(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"
