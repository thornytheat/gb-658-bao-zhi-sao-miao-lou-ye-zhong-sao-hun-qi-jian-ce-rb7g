"""校验、规范化和三方合并的纯函数。

同步记录按字段保存，字段值限定为 JSON 标量。``None`` 一律视为"字段不存在"，
这样"删除"和"显式空值"不会产生两套语义。
"""

import json
from hashlib import sha256

from app.errors import DomainError

SCALAR_TYPES = (str, int, float, bool)

# 用于表达"字段不存在"，区别于显式的 None。
MISSING = object()


def canon(value) -> str:
    """字段值的稳定序列化，用于相等比较与摘要。"""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DomainError("BAD_FIELD_VALUE", "字段值必须是 JSON 标量") from exc


def same(left, right) -> bool:
    if left is MISSING or right is MISSING:
        return left is MISSING and right is MISSING
    return canon(left) == canon(right)


def _scalar(value, field: str):
    if value is None:
        return MISSING
    if isinstance(value, SCALAR_TYPES):
        canon(value)
        return value
    raise DomainError("BAD_FIELD_VALUE", f"字段 {field} 的值必须是标量")


def normalize_fields(raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DomainError("BAD_REVISION", "fields 必须是对象")
    fields = {}
    for field, value in raw.items():
        if not isinstance(field, str) or not field.strip():
            raise DomainError("BAD_FIELD_VALUE", "字段名不能为空")
        value = _scalar(value, field)
        if value is not MISSING:
            fields[field] = value
    return fields


def normalize_changes(raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DomainError("BAD_REVISION", "changes 必须是对象")
    changes = {}
    for field, change in raw.items():
        if not isinstance(field, str) or not field.strip():
            raise DomainError("BAD_FIELD_VALUE", "字段名不能为空")
        if not isinstance(change, dict) or "to" not in change:
            raise DomainError("BAD_REVISION", f"字段 {field} 的修订必须给出 to")
        unknown = set(change) - {"from", "to"}
        if unknown:
            raise DomainError("BAD_REVISION", f"字段 {field} 的修订含未知键")
        changes[field] = {
            key: _scalar(change[key], field) for key in ("from", "to") if key in change
        }
    return changes


def _text(raw: dict, key: str, *, required: bool = False):
    value = raw.get(key)
    if value is None:
        if required:
            raise DomainError("BAD_REVISION", f"缺少 {key}")
        return None
    if not isinstance(value, str) or not value.strip():
        raise DomainError("BAD_REVISION", f"{key} 必须是非空字符串")
    return value.strip()


def _version(raw: dict, key: str):
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainError("BAD_REVISION", f"{key} 必须是非负整数")
    return value


def normalize_revision(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise DomainError("BAD_REVISION", "修订必须是对象")
    has_fields = raw.get("fields") is not None
    has_changes = raw.get("changes") is not None
    if has_fields == has_changes:
        raise DomainError("BAD_REVISION", "fields 与 changes 必须且只能给出一个")

    return {
        "revision_id": _text(raw, "revision_id", required=True),
        "business_key": _text(raw, "business_key", required=True),
        "station_id": _text(raw, "station_id"),
        "parent_revision": _text(raw, "parent_revision"),
        "base_version": _version(raw, "base_version"),
        "expected_version": _version(raw, "expected_version"),
        "mode": "fields" if has_fields else "changes",
        "fields": normalize_fields(raw.get("fields")) if has_fields else None,
        "changes": normalize_changes(raw.get("changes")) if has_changes else None,
        "submitted_at": _text(raw, "submitted_at"),
    }


def changes_json(changes: dict | None):
    """把内部 changes 转为可序列化结构；MISSING 落为 null，读回时还原。"""

    if changes is None:
        return None
    return {
        field: {key: (None if value is MISSING else value) for key, value in change.items()}
        for field, change in changes.items()
    }


def payload_digest(spec: dict) -> str:
    body = {
        "revision_id": spec["revision_id"],
        "business_key": spec["business_key"],
        "station_id": spec["station_id"],
        "parent_revision": spec["parent_revision"],
        "base_version": spec["base_version"],
        "expected_version": spec["expected_version"],
        "mode": spec["mode"],
        "fields": spec["fields"],
        "changes": changes_json(spec["changes"]),
    }
    return sha256(canon(body).encode("utf-8")).hexdigest()


def apply_changes(base: dict, changes: dict) -> tuple[dict, list[str]]:
    """把离线修订应用到共同底稿。

    返回 (生效字段, from 与共同底稿不符的字段)。``from`` 只是站点自述，不能
    作为真值；不符时仍按共同底稿比较，差异留给三方合并和证据。
    """

    result = dict(base)
    mismatched = []
    for field, change in changes.items():
        if "from" in change:
            expected = MISSING if change["from"] is None else change["from"]
            current = result.get(field, MISSING)
            if not same(expected, current):
                mismatched.append(field)
        target = change["to"]
        if target is None or target is MISSING:
            result.pop(field, None)
        else:
            result[field] = target
    return result, mismatched


def effective_fields(spec: dict, base: dict) -> tuple[dict, list[str]]:
    if spec["mode"] == "fields":
        return dict(spec["fields"]), []
    return apply_changes(base, spec["changes"])


def three_way_merge(base: dict, ours: dict, theirs: dict) -> tuple[dict, dict]:
    """共同底稿三方合并。

    返回 (合并结果, {冲突字段: 三方取值})。只有双方都相对底稿改动且改得不同
    才产生冲突；双方改得一样视为达成一致。
    """

    merged = {}
    conflicts = {}
    for field in sorted(set(base) | set(ours) | set(theirs)):
        base_value = base.get(field, MISSING)
        our_value = ours.get(field, MISSING)
        their_value = theirs.get(field, MISSING)

        if same(our_value, their_value):
            chosen = our_value
        elif same(our_value, base_value):
            chosen = their_value
        elif same(their_value, base_value):
            chosen = our_value
        else:
            conflicts[field] = {
                "base": base_value,
                "ours": our_value,
                "theirs": their_value,
            }
            chosen = our_value
        if chosen is not MISSING:
            merged[field] = chosen
    return merged, conflicts


def protect_approved(
    merged: dict,
    base: dict,
    ours: dict,
    theirs: dict,
    approved_fields: list[str],
    approved_values: dict,
) -> tuple[dict, dict]:
    """中心已核准的字段不接受任何未裁定改动。

    旧底稿把已核准的期版页归属改回去时挂起，而不是让整单合并落账。
    """

    conflicts = {}
    result = dict(merged)
    for field in approved_fields:
        approved = approved_values.get(field, ours.get(field, MISSING))
        chosen = result.get(field, MISSING)
        if same(chosen, approved):
            continue
        conflicts[field] = {
            "base": base.get(field, MISSING),
            "ours": approved,
            "theirs": theirs.get(field, MISSING),
        }
        if approved is MISSING:
            result.pop(field, None)
        else:
            result[field] = approved
    return result, conflicts


def changes_from_json(data):
    if data is None:
        return None
    return {
        field: {key: (MISSING if value is None else value) for key, value in change.items()}
        for field, change in data.items()
    }
