"""轻量文件缓存（任务书 10.4）。

- JSON 缓存：新闻检索结果、价格序列、PDF 抽取结果（按文档哈希+解析器版本）
- 二进制缓存：PDF 文件（按 SHA256）
- 所有缓存记录均携带 retrieved_at，is_cached 标记随响应返回，
  绝不把缓存数据伪装成实时数据。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mda.common.models import iso_now

_SAFE_KEY = re.compile(r"[^a-zA-Z0-9._-]")


def safe_key(key: str) -> str:
    """任意字符串 -> 文件系统安全键。"""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    readable = _SAFE_KEY.sub("_", key)[:48]
    return f"{readable}_{digest}"


class JsonFileCache:
    """按 key 存 JSON 的 TTL 缓存。"""

    def __init__(self, root: Path, ttl: timedelta | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl

    def _path(self, key: str) -> Path:
        return self.root / f"{safe_key(key)}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        stored = payload.get("retrieved_at")
        if self.ttl and stored:
            try:
                ts = datetime.fromisoformat(stored)
                if datetime.now(timezone.utc) - ts > self.ttl:
                    return None
            except ValueError:
                return None
        return payload

    def set(self, key: str, value: dict) -> None:
        payload = dict(value)
        payload["retrieved_at"] = iso_now()
        self._path(key).write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )


class BlobCache:
    """按 SHA256 存二进制文件（用于 PDF 下载缓存）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def has(self, sha256: str) -> bool:
        return (self.root / f"{sha256}.bin").exists()

    def read(self, sha256: str) -> bytes:
        return (self.root / f"{sha256}.bin").read_bytes()

    def write(self, sha256: str, data: bytes) -> None:
        (self.root / f"{sha256}.bin").write_bytes(data)

    @staticmethod
    def sha256_of(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()
