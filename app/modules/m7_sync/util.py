"""同步批次的公共工具：稳定 JSON、时间戳和业务异常。"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import TIMEZONE
from app.errors import DomainError

ZONE = ZoneInfo(TIMEZONE)


def now_iso() -> str:
    return datetime.now(ZONE).isoformat(timespec="seconds")


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text):
    return json.loads(text) if text is not None else None


def bad(code: str, message: str, status: int = 422) -> DomainError:
    return DomainError(code, message, status)
