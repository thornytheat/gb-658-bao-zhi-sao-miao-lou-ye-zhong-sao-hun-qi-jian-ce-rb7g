"""站点离线修订同步：接收、三方合并、冲突挂起、裁定入账与回读。

不变量：
- 记录版本号只增不减；同一版本快照不可变，重放不会改写历史。
- 中心已核准字段不会被未裁定修订覆盖，旧底稿不能把期版页归属盖回去。
- 修订号按站点全局唯一；同号同内容视为重试，同号异内容拒绝。
- 批次按 (station_id, payload_sha256) 幂等，重试复用同一 batch_id。
- 合并中断留下的 STAGED/REJECTED 修订可以从 revision_id 重新处理。
"""

from hashlib import sha256

from app.modules.m2_issue.catalog import split_key
from app.reasons import REASONS
from app.store import read, transaction

from .intake import prepare_manifest, quarantine_key
from .model import (
    MISSING,
    changes_from_json,
    changes_json,
    canon,
    effective_fields,
    normalize_fields,
    normalize_revision,
    payload_digest,
    protect_approved,
    same,
    three_way_merge,
)
from .util import bad, dumps, loads, now_iso

# 可被重新处理的未入账状态。CONFLICT 需要先裁定，恢复时只报告不改动。
PENDING_STATUSES = ("STAGED", "REJECTED", "BASE_MISMATCH")
RECOVERABLE_STATUSES = PENDING_STATUSES + ("CONFLICT",)
BOOKED_STATUSES = ("APPLIED", "UNCHANGED")

REVISION_STATUS = {
    "APPLIED": "已入账",
    "UNCHANGED": "无改动",
    "STAGED": "待前序修订",
    "CONFLICT": "冲突挂起",
    "REJECTED": "拒绝合并",
    "BASE_MISMATCH": "底稿不符",
    "STALE_VERSION": "过期版本",
}

BATCH_STATUS = {
    "RECEIVED": "已接收",
    "COMPLETED": "已入账",
    "HELD": "存在挂起",
    "PARTIAL": "部分入账",
}


def reason(code: str) -> str:
    return REASONS.get(code, code)


def _row(db, sql: str, params: tuple = ()):
    return db.execute(sql, params).fetchone()


def _load_record(db, business_key: str):
    return _row(db, "SELECT * FROM sync_records WHERE business_key = ?", (business_key,))


def _record_fields(row) -> dict:
    return loads(row["fields_json"]) or {}


def _approved_fields(row) -> list[str]:
    return loads(row["approved_fields_json"]) or []


def _approved_values(row) -> dict:
    return loads(row["approved_values_json"]) or {}


def _snapshot(db, business_key: str, version) -> dict | None:
    if version is None:
        return None
    row = _row(
        db,
        "SELECT fields_json FROM sync_record_versions WHERE business_key = ? AND version = ?",
        (business_key, version),
    )
    return loads(row["fields_json"]) if row else None



def _write_version(db, business_key: str, fields: dict, version: int, revision_id, now: str):
    approved = _approved_fields(_load_record(db, business_key))
    db.execute(
        "INSERT OR IGNORE INTO sync_record_versions (business_key, version, fields_json, revision_id, approved_fields_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (business_key, version, dumps(fields), revision_id, dumps(approved), now),
    )


def _update_item(db, batch_id, revision_id, status, code=None, message=None, now=None):
    if not batch_id:
        return
    db.execute(
        "UPDATE sync_batch_items SET status = ?, reason_code = ?, message = ?, updated_at = ? WHERE batch_id = ? AND revision_id = ?",
        (status, code, message, now or now_iso(), batch_id, revision_id),
    )


def _commit(
    db,
    business_key: str,
    fields: dict,
    record_version: int,
    revision_id,
    station_id: str,
    now: str,
) -> int:
    """把一次合并结果落成新版本，返回新版本号。"""

    new_version = record_version + 1
    exists = _load_record(db, business_key) is not None
    if not exists:
        db.execute(
            "INSERT INTO sync_records (business_key, station_id, fields_json, version, head_revision, approved_fields_json, approved_values_json, approved_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, '[]', '{}', NULL, ?, ?)",
            (business_key, station_id, dumps(fields), new_version, revision_id, now, now),
        )
    _write_version(db, business_key, fields, new_version, revision_id, now)
    db.execute(
        "UPDATE sync_records SET fields_json = ?, version = ?, head_revision = ?, updated_at = ? WHERE business_key = ?",
        (dumps(fields), new_version, revision_id, now, business_key),
    )
    return new_version


def _full_revision_id(station_id: str, client_id: str) -> str:
    return f"{station_id}|{client_id}"


def _find_parent(db, station_id: str, parent_ref: str):
    """前序修订可以按站点本地号引用，也可以按完整修订号引用。"""

    candidates = [_full_revision_id(station_id, parent_ref)]
    if "|" in parent_ref:
        candidates.append(parent_ref)
    for candidate in candidates:
        row = _row(db, "SELECT * FROM sync_revisions WHERE revision_id = ?", (candidate,))
        if row is not None:
            return row
    return None


def _conflict_entries(field_conflicts: dict, approved_conflicts: dict) -> dict:
    entries = {}
    for field, values in field_conflicts.items():
        entries[field] = ("FIELD_CONFLICT", values)
    for field, values in approved_conflicts.items():
        entries[field] = ("APPROVED_OVERRIDE", values)
    return entries


def _hold_conflicts(db, revision_id, business_key, entries: dict, record_version: int, now: str):
    if not entries:
        return []
    existing = {
        row["field"]
        for row in db.execute(
            "SELECT field FROM sync_conflicts WHERE revision_id = ? AND status = 'OPEN'",
            (revision_id,),
        )
    }
    held = []
    for field in sorted(entries):
        code, values = entries[field]
        held.append(field)
        if field in existing:
            continue
        db.execute(
            """
            INSERT INTO sync_conflicts (
                conflict_id, revision_id, business_key, field, reason_code,
                base_present, base_value_json, ours_present, ours_value_json,
                theirs_present, theirs_value_json, record_version_at_hold,
                status, evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
            """,
            (
                f"{revision_id}::{field}",
                revision_id,
                business_key,
                field,
                code,
                int(values["base"] is not MISSING),
                canon(values["base"]) if values["base"] is not MISSING else None,
                int(values["ours"] is not MISSING),
                canon(values["ours"]) if values["ours"] is not MISSING else None,
                int(values["theirs"] is not MISSING),
                canon(values["theirs"]) if values["theirs"] is not MISSING else None,
                record_version,
                dumps({"reason_code": code}),
                now,
            ),
        )
    return held


def _insert_revision(db, spec, revision_id, digest, row_values, now):
    db.execute(
        """
        INSERT INTO sync_revisions (
            revision_id, business_key, station_id, batch_id, parent_revision,
            base_version, expected_version, base_fields_json, mode, fields_json,
            changes_json, payload_sha256, effective_json, status, reason_code,
            record_version_before, record_version_after, evidence_json,
            submitted_at, created_at, updated_at, booked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            spec["business_key"],
            spec["station_id"],
            spec["batch_id"],
            spec["parent_revision"],
            spec["base_version"],
            spec["expected_version"],
            row_values.get("base_fields_json"),
            spec["mode"],
            dumps(spec["fields"]) if spec["fields"] is not None else None,
            dumps(changes_json(spec["changes"])) if spec["changes"] is not None else None,
            digest,
            row_values.get("effective_json"),
            row_values["status"],
            row_values.get("reason_code"),
            row_values["record_version_before"],
            row_values["record_version_after"],
            dumps(row_values.get("evidence") or {}),
            spec["submitted_at"],
            now,
            now,
            now if row_values["status"] in BOOKED_STATUSES else None,
        ),
    )
    if spec["batch_id"]:
        db.execute(
            "INSERT OR REPLACE INTO sync_batch_items (batch_id, revision_id, business_key, status, reason_code, message, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                spec["batch_id"],
                revision_id,
                spec["business_key"],
                row_values["status"],
                row_values.get("reason_code"),
                row_values.get("message"),
                now,
            ),
        )


def _reject(db, spec, revision_id, digest, code, message, now, status=None) -> dict:
    """记录不进入合并的修订，保留原始提报与原因。"""

    record = _load_record(db, spec["business_key"])
    current = int(record["version"]) if record else 0
    status = status or ("STAGED" if code == "MISSING_PARENT" else "REJECTED")
    evidence = {
        "message": message,
        "parent_revision": spec["parent_revision"],
        "requested_base_version": spec["base_version"],
        "expected_version": spec["expected_version"],
        "station_id": spec["station_id"],
    }
    _insert_revision(
        db,
        spec,
        revision_id,
        digest,
        {
            "status": status,
            "reason_code": code,
            "record_version_before": current,
            "record_version_after": current,
            "evidence": evidence,
            "message": message,
        },
        now,
    )
    return _result(db, revision_id)


def _apply(db, spec, revision_id, digest, now) -> dict:
    """在已打开的事务内处理一条修订。"""

    business_key = spec["business_key"]
    record = _load_record(db, business_key)
    record_version = int(record["version"]) if record else 0
    head_fields = _record_fields(record) if record else {}
    approved_fields = _approved_fields(record) if record else []
    approved_values = _approved_values(record) if record else {}

    # 中心复核与同步并发：提报明确期望的版本不是当前版本即拒绝，防止过期覆盖。
    if spec["expected_version"] is not None and spec["expected_version"] != record_version:
        return _reject(
            db,
            spec,
            revision_id,
            digest,
            "STALE_VERSION",
            f"期望版本 {spec['expected_version']} 与当前入账版本 {record_version} 不一致",
            now,
        )

    requested_base = spec["base_version"]
    parent = None
    if spec["parent_revision"] is not None:
        parent = _find_parent(db, spec["station_id"], spec["parent_revision"])
        if parent is None:
            return _reject(
                db,
                spec,
                revision_id,
                digest,
                "MISSING_PARENT",
                f"缺少前序修订 {spec['parent_revision']}，暂存待补",
                now,
            )
        if parent["business_key"] != business_key:
            return _reject(
                db,
                spec,
                revision_id,
                digest,
                "PARENT_DIVERGED",
                "前序修订属于其他记录，分叉拒绝自动合并",
                now,
                status="REJECTED",
            )
        if parent["status"] not in BOOKED_STATUSES:
            return _reject(
                db,
                spec,
                revision_id,
                digest,
                "MISSING_PARENT",
                f"前序修订状态为 {REVISION_STATUS.get(parent['status'], parent['status'])}，暂存待其入账",
                now,
                status="STAGED",
            )
        if requested_base is None:
            requested_base = parent["record_version_after"]

    if requested_base is not None and requested_base > record_version:
        return _reject(
            db,
            spec,
            revision_id,
            digest,
            "BASE_MISMATCH",
            f"底稿版本 {requested_base} 高于当前入账版本 {record_version}",
            now,
        )

    base_fields = _snapshot(db, business_key, requested_base)
    if base_fields is None:
        # 没有对应快照（例如中心复核初次建记录）时，以当前入账值作底稿。
        base_fields = head_fields if requested_base in (None, record_version) else {}

    theirs, mismatched = effective_fields(spec, base_fields)
    merged, field_conflicts = three_way_merge(base_fields, head_fields, theirs)
    merged, approved_conflicts = protect_approved(
        merged, base_fields, head_fields, theirs, approved_fields, approved_values
    )
    entries = _conflict_entries(field_conflicts, approved_conflicts)

    evidence = {
        "requested_base_version": spec["base_version"],
        "effective_base_version": requested_base,
        "record_version": record_version,
        "parent_revision": spec["parent_revision"],
        "submitted_at": spec["submitted_at"],
        "from_mismatch": mismatched,
        "conflict_fields": sorted(entries),
        "approved_fields": approved_fields,
    }

    if entries:
        # 先按共同底稿把互不冲突的字段入账；分歧字段保留中心当前值并挂起裁定。
        new_version = record_version
        if not same(merged, head_fields):
            new_version = _commit(db, business_key, merged, record_version, revision_id, spec["station_id"], now)
        conflict_fields = _hold_conflicts(
            db, revision_id, business_key, entries, new_version, now
        )
        _insert_revision(
            db,
            spec,
            revision_id,
            digest,
            {
                "status": "CONFLICT",
                "reason_code": entries[conflict_fields[0]][0],
                "record_version_before": record_version,
                "record_version_after": new_version,
                "base_fields_json": dumps(base_fields),
                "effective_json": dumps(merged),
                "evidence": {**evidence, "held_fields": conflict_fields},
            },
            now,
        )
        return _result(db, revision_id)

    if same(merged, head_fields):
        _insert_revision(
            db,
            spec,
            revision_id,
            digest,
            {
                "status": "UNCHANGED",
                "record_version_before": record_version,
                "record_version_after": record_version,
                "base_fields_json": dumps(base_fields),
                "effective_json": dumps(merged),
                "evidence": evidence,
            },
            now,
        )
        return _result(db, revision_id)

    new_version = _commit(db, business_key, merged, record_version, revision_id, spec["station_id"], now)
    _insert_revision(
        db,
        spec,
        revision_id,
        digest,
        {
            "status": "APPLIED",
            "record_version_before": record_version,
            "record_version_after": new_version,
            "base_fields_json": dumps(base_fields),
            "effective_json": dumps(merged),
            "evidence": evidence,
        },
        now,
    )
    return _result(db, revision_id)


def _result(db, revision_id: str) -> dict:
    row = _row(db, "SELECT * FROM sync_revisions WHERE revision_id = ?", (revision_id,))
    if row is None:
        raise bad("REVISION_NOT_FOUND", "修订不存在", 404)
    return _result_for_row(db, row)


def _result_for_row(db, row) -> dict:
    conflicts = [
        _conflict_dict(item)
        for item in db.execute(
            "SELECT * FROM sync_conflicts WHERE revision_id = ? ORDER BY field",
            (row["revision_id"],),
        )
    ]
    evidence = loads(row["evidence_json"]) or {}
    return {
        "revision_id": row["revision_id"],
        "client_revision_id": row["revision_id"].split("|", 1)[-1],
        "business_key": row["business_key"],
        "station_id": row["station_id"],
        "batch_id": row["batch_id"],
        "status": row["status"],
        "status_label": REVISION_STATUS.get(row["status"], row["status"]),
        "reason_code": row["reason_code"],
        "reason": reason(row["reason_code"]) if row["reason_code"] else None,
        "record_version_before": row["record_version_before"],
        "record_version_after": row["record_version_after"],
        "evidence": evidence,
        "conflicts": conflicts,
        "deduplicated": False,
    }


def _conflict_dict(row) -> dict:
    return {
        "conflict_id": row["conflict_id"],
        "revision_id": row["revision_id"],
        "field": row["field"],
        "business_key": row["business_key"],
        "reason_code": row["reason_code"],
        "reason": reason(row["reason_code"]),
        "status": row["status"],
        "base": loads(row["base_value_json"]) if row["base_present"] else None,
        "base_present": bool(row["base_present"]),
        "ours": loads(row["ours_value_json"]) if row["ours_present"] else None,
        "ours_present": bool(row["ours_present"]),
        "theirs": loads(row["theirs_value_json"]) if row["theirs_present"] else None,
        "theirs_present": bool(row["theirs_present"]),
        "record_version_at_hold": row["record_version_at_hold"],
        "resolution_value": loads(row["resolution_value_json"])
        if row["resolution_value_json"] is not None
        else None,
        "resolution_source": row["resolution_source"],
        "resolved_by": row["resolved_by"],
        "resolved_at": row["resolved_at"],
        "evidence": loads(row["evidence_json"]) or {},
    }


# ---------------------------------------------------------------------------
# 接收
# ---------------------------------------------------------------------------


def _validate_batch(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise bad("BAD_BATCH", "批次必须是对象")
    station_id = payload.get("station_id")
    if not isinstance(station_id, str) or not station_id.strip():
        raise bad("BAD_BATCH", "缺少 station_id")
    revisions = payload.get("revisions") or []
    if not isinstance(revisions, list):
        raise bad("BAD_BATCH", "revisions 必须是数组")
    scans = payload.get("scans")
    if scans is not None and not isinstance(scans, (str, list)):
        raise bad("BAD_BATCH", "scans 必须是 CSV 文本或行对象列表")
    quarantine = payload.get("quarantine") or []
    if not isinstance(quarantine, list):
        raise bad("BAD_BATCH", "quarantine 必须是数组")
    return {
        "station_id": station_id.strip(),
        "batch_ref": payload.get("batch_ref"),
        "scans": scans,
        "scans_given": "scans" in payload,
        "quarantine": quarantine,
        "revisions": revisions,
    }


def receive(payload: dict, batch_id: str | None = None) -> dict:
    """接收站点批次；幂等键是 (station_id, payload 摘要)。"""

    batch = _validate_batch(payload)
    body = {
        "station_id": batch["station_id"],
        "batch_ref": batch["batch_ref"],
        "scans": batch["scans"],
        "quarantine": batch["quarantine"],
        "revisions": batch["revisions"],
    }
    payload_text = dumps(body)
    digest = sha256(payload_text.encode("utf-8")).hexdigest()
    batch_id = batch_id or f"{batch['station_id']}|{digest[:16]}"

    prepared = prepare_manifest(batch["scans"]) if batch["scans_given"] else {
        "input": 0,
        "accepted": [],
        "quarantine": [],
        "tail_complete": True,
    }

    now = now_iso()
    with transaction() as db:
        existing = _row(db, "SELECT * FROM sync_batches WHERE batch_id = ?", (batch_id,))
        if existing is not None:
            if existing["payload_sha256"] != digest:
                raise bad("BATCH_CONFLICT", "同一 batch_id 的内容不一致", 409)
            summary = loads(existing["summary_json"]) or {}
            summary["deduplicated"] = True
            summary["status_label"] = BATCH_STATUS.get(summary.get("status"), summary.get("status"))
            # 逐条结果以库中当前状态为准，重试时如实标出已处理过的修订。
            summary["revisions"] = [
                {**_result_for_row(db, row), "deduplicated": True}
                for row in db.execute(
                    "SELECT * FROM sync_revisions WHERE batch_id = ? ORDER BY seq", (batch_id,)
                )
            ]
            summary["quarantine"] = [
                {
                    "line_no": row["line_no"],
                    "reason_code": row["reason_code"],
                    "raw_value": row["raw_value"],
                }
                for row in db.execute(
                    "SELECT * FROM sync_scan_quarantine WHERE batch_id = ? ORDER BY line_no",
                    (batch_id,),
                )
            ]
            return summary

        db.execute(
            "INSERT INTO sync_batches (batch_id, station_id, payload_sha256, payload_json, status, summary_json, created_at, updated_at) VALUES (?, ?, ?, ?, 'RECEIVED', '{}', ?, ?)",
            (batch_id, batch["station_id"], digest, payload_text, now, now),
        )

        _store_quarantine(db, batch["station_id"], batch_id, batch["quarantine"], prepared["quarantine"], now)
        received = _store_scans(db, batch["station_id"], batch_id, prepared["accepted"], now)

        results = _process_revisions(db, batch["revisions"], batch["station_id"], batch_id)

        summary = _summarise(
            db, batch_id, batch["station_id"], digest, prepared, received, results
        )
        db.execute(
            "UPDATE sync_batches SET status = ?, summary_json = ?, updated_at = ? WHERE batch_id = ?",
            (summary["status"], dumps(summary), now_iso(), batch_id),
        )
        summary["deduplicated"] = False
        return summary


def _store_quarantine(db, station_id, batch_id, declared, prepared, now):
    for entry in list(declared) + list(prepared):
        entry = entry if isinstance(entry, dict) else {"raw_value": canon(entry)}
        db.execute(
            "INSERT OR IGNORE INTO sync_scan_quarantine (quarantine_id, station_id, batch_id, line_no, reason_code, raw_value, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                quarantine_key(station_id, entry),
                station_id,
                batch_id,
                entry.get("line_no"),
                entry.get("reason_code") or "BAD_FORMAT",
                entry.get("raw_value") or "",
                entry.get("message"),
                now,
            ),
        )


def _store_scans(db, station_id, batch_id, scans, now) -> int:
    received = 0
    for scan in scans:
        cur = db.execute(
            "INSERT OR IGNORE INTO sync_scans (station_id, sha256, business_key, file_name, fields_json, batch_id, line_no, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                station_id,
                scan["sha256"],
                scan["business_key"],
                scan["file_name"],
                dumps(scan["fields"]),
                batch_id,
                scan["line_no"],
                now,
            ),
        )
        received += cur.rowcount
    return received


def _process_revisions(db, raw_revisions, station_id, batch_id) -> list[dict]:
    """按批次内顺序处理修订，前序未入账的留待下一轮，避免顺序影响结果。"""

    specs = []
    results: dict[str, dict] = {}
    for index, raw in enumerate(raw_revisions):
        spec = normalize_revision(raw)
        spec["station_id"] = (spec["station_id"] or station_id).strip()
        if not spec["station_id"]:
            raise bad("BAD_REVISION", "缺少 station_id")
        spec["batch_id"] = batch_id
        spec["_index"] = index
        specs.append(spec)

    for spec in specs:
        revision_id = _full_revision_id(spec["station_id"], spec["revision_id"])
        digest = payload_digest(spec)
        existing = _row(db, "SELECT * FROM sync_revisions WHERE revision_id = ?", (revision_id,))
        if existing is not None:
            if existing["payload_sha256"] != digest:
                raise bad(
                    "REVISION_CONFLICT",
                    f"修订号 {spec['revision_id']} 已登记且内容不一致",
                    409,
                )
            result = _result_for_row(db, existing)
            result["deduplicated"] = True
            results[revision_id] = result
            continue
        clash = _row(
            db,
            "SELECT revision_id FROM sync_revisions WHERE revision_id = ? AND business_key != ?",
            (revision_id, spec["business_key"]),
        )
        if clash is not None:
            raise bad(
                "REVISION_REUSED", f"修订号 {spec['revision_id']} 已被其他记录使用", 409
            )

    pending = [
        spec
        for spec in specs
        if _full_revision_id(spec["station_id"], spec["revision_id"]) not in results
    ]
    batch_ids = {
        (spec["station_id"], spec["revision_id"]) for spec in specs
    }
    while pending:
        deferred = []
        progressed = False
        for spec in pending:
            if _parent_pending_in_batch(db, spec, batch_ids, results):
                deferred.append(spec)
                continue
            revision_id = _full_revision_id(spec["station_id"], spec["revision_id"])
            results[revision_id] = _apply(db, spec, revision_id, payload_digest(spec), now_iso())
            progressed = True
        if not deferred:
            break
        if not progressed:
            for spec in deferred:
                revision_id = _full_revision_id(spec["station_id"], spec["revision_id"])
                results[revision_id] = _reject(
                    db,
                    spec,
                    revision_id,
                    payload_digest(spec),
                    "MISSING_PARENT",
                    f"前序修订 {spec['parent_revision']} 未入账，暂存待补",
                    now_iso(),
                )
            break
        pending = deferred

    ordered = sorted(
        results.values(),
        key=lambda item: (specs_index(specs, item["revision_id"]), item["revision_id"]),
    )
    return ordered


def specs_index(specs: list[dict], revision_id: str) -> int:
    for spec in specs:
        if _full_revision_id(spec["station_id"], spec["revision_id"]) == revision_id:
            return spec["_index"]
    return len(specs)


def _parent_pending_in_batch(db, spec, batch_ids, results) -> bool:
    """前序修订在本批次内但尚未处理时，先把当前修订让后，避免批次顺序影响结果。"""

    if spec["parent_revision"] is None:
        return False
    parent = _find_parent(db, spec["station_id"], spec["parent_revision"])
    if parent is not None:
        return False
    if "|" in spec["parent_revision"]:
        parent_key = (spec["station_id"], spec["parent_revision"].split("|", 1)[-1])
    else:
        parent_key = (spec["station_id"], spec["parent_revision"])
    if parent_key not in batch_ids:
        return False
    return _full_revision_id(*parent_key) not in results


def _summarise(db, batch_id, station_id, digest, prepared, received, results) -> dict:
    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    if all(item["status"] in BOOKED_STATUSES for item in results):
        status = "COMPLETED"
    elif counts.get("CONFLICT"):
        status = "HELD"
    else:
        status = "PARTIAL"

    scans_total = _row(
        db, "SELECT COUNT(*) AS n FROM sync_scans WHERE station_id = ?", (station_id,)
    )["n"]
    quarantine_total = _row(
        db, "SELECT COUNT(*) AS n FROM sync_scan_quarantine WHERE station_id = ?", (station_id,)
    )["n"]

    return {
        "batch_id": batch_id,
        "station_id": station_id,
        "payload_sha256": digest,
        "status": status,
        "status_label": BATCH_STATUS.get(status, status),
        "deduplicated": False,
        "scans": {
            "input": prepared["input"],
            "accepted": len(prepared["accepted"]),
            "quarantined": len(prepared["quarantine"]),
            "newly_received": received,
            "station_total": scans_total,
            "station_quarantined": quarantine_total,
            "tail_complete": prepared["tail_complete"],
        },
        "quarantine": [
            {
                "line_no": entry.get("line_no"),
                "reason_code": entry.get("reason_code"),
                "raw_value": entry.get("raw_value"),
            }
            for entry in prepared["quarantine"]
        ],
        "revisions": results,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# 冲突裁定
# ---------------------------------------------------------------------------


def resolve(conflict_id: str, payload: dict | None = None) -> dict:
    """裁定一个挂起冲突；只改该字段，其余字段的合并结果保持不变。"""

    payload = payload or {}
    choice = payload.get("value_choice")
    if choice not in ("ours", "theirs", "value"):
        raise bad("BAD_RESOLUTION", "value_choice 必须是 ours、theirs 或 value")
    if choice == "value" and "value" not in payload:
        raise bad("BAD_RESOLUTION", "value_choice=value 时必须给出 value")
    operator = payload.get("operator")
    allow_stale = bool(payload.get("allow_stale_base"))

    now = now_iso()
    with transaction() as db:
        conflict = _row(db, "SELECT * FROM sync_conflicts WHERE conflict_id = ?", (conflict_id,))
        if conflict is None:
            raise bad("CONFLICT_NOT_FOUND", "冲突不存在", 404)
        if conflict["status"] != "OPEN":
            raise bad("CONFLICT_CLOSED", "冲突已裁定", 409)

        revision = _row(
            db, "SELECT * FROM sync_revisions WHERE revision_id = ?", (conflict["revision_id"],)
        )
        record = _load_record(db, conflict["business_key"])
        record_version = int(record["version"]) if record else 0

        if not allow_stale and conflict["record_version_at_hold"] != record_version:
            raise bad(
                "STALE_RESOLUTION",
                f"记录已从版本 {conflict['record_version_at_hold']} 前进到 {record_version}，请重新核对",
                409,
            )

        if choice == "ours":
            value = loads(conflict["ours_value_json"]) if conflict["ours_present"] else MISSING
            source = "ours"
        elif choice == "theirs":
            value = loads(conflict["theirs_value_json"]) if conflict["theirs_present"] else MISSING
            source = "theirs"
        else:
            value = payload["value"]
            source = payload.get("resolution_source") or "manual"
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise bad("BAD_RESOLUTION", "裁定值必须是标量")
            if value is None:
                value = MISSING

        db.execute(
            "UPDATE sync_conflicts SET status = 'RESOLVED', resolution_value_json = ?, resolution_source = ?, resolved_by = ?, resolved_at = ?, evidence_json = ? WHERE conflict_id = ?",
            (
                None if value is MISSING else canon(value),
                source,
                operator,
                now,
                dumps(
                    {
                        **(loads(conflict["evidence_json"]) or {}),
                        "value_choice": choice,
                        "operator": operator,
                    }
                ),
                conflict_id,
            ),
        )

        open_count = _row(
            db,
            "SELECT COUNT(*) AS n FROM sync_conflicts WHERE revision_id = ? AND status = 'OPEN'",
            (revision["revision_id"],),
        )["n"]
        result = _conflict_dict(
            _row(db, "SELECT * FROM sync_conflicts WHERE conflict_id = ?", (conflict_id,))
        )
        if open_count:
            return {
                "conflict": result,
                "revision": _result_for_row(db, revision),
                "booked": False,
                "record_version": record_version,
            }

        booked = _book_resolved(db, revision, record, now, operator)
        if revision["batch_id"]:
            _refresh_batch(db, revision["batch_id"], now)
        return {
            "conflict": result,
            "revision": _result(db, revision["revision_id"]),
            "booked": booked["applied"],
            "record_version": booked["version"],
        }


def _book_resolved(db, revision, record, now, operator) -> dict:
    business_key = revision["business_key"]
    record_version = int(record["version"]) if record else 0
    current_fields = _record_fields(record) if record else {}
    merged = dict(current_fields)  # 裁定只补写分歧字段，其余以当前入账值为准
    decided = {}

    for row in db.execute(
        "SELECT * FROM sync_conflicts WHERE revision_id = ? ORDER BY field",
        (revision["revision_id"],),
    ):
        field = row["field"]
        if row["resolution_value_json"] is None:
            merged.pop(field, None)
        else:
            merged[field] = loads(row["resolution_value_json"])
        decided[field] = row["resolution_source"]

    # 若裁定明确改写了已核准字段，核准基准随裁定更新；否则后续修订会被旧核准值误拦。
    if record is not None and any(
        source != "ours" and field in _approved_fields(record) for field, source in decided.items()
    ):
        approved_values = _approved_values(record)
        for field, source in decided.items():
            if source == "ours" or field not in _approved_fields(record):
                continue
            if field in merged:
                approved_values[field] = merged[field]
            else:
                approved_values.pop(field, None)
        db.execute(
            "UPDATE sync_records SET approved_values_json = ?, updated_at = ? WHERE business_key = ?",
            (dumps(approved_values), now, business_key),
        )

    if same(merged, current_fields):
        # 分歧字段裁定为现状；若同单其他字段已在接收时入账，保留 APPLIED 语义。
        already_booked = int(revision["record_version_after"]) > int(revision["record_version_before"])
        status = "APPLIED" if already_booked else "UNCHANGED"
        db.execute(
            "UPDATE sync_revisions SET status = ?, reason_code = NULL, booked_at = ? WHERE revision_id = ?",
            (status, now, revision["revision_id"]),
        )
        _update_item(db, revision["batch_id"], revision["revision_id"], status, None, None, now)
        return {"applied": False, "version": record_version}

    new_version = _commit(
        db, business_key, merged, record_version, revision["revision_id"], revision["station_id"], now
    )
    db.execute(
        "UPDATE sync_revisions SET status = 'APPLIED', reason_code = NULL, booked_at = ?, record_version_before = ?, record_version_after = ?, effective_json = ?, evidence_json = ? WHERE revision_id = ?",
        (
            now,
            record_version,
            new_version,
            dumps(merged),
            dumps({**(loads(revision["evidence_json"]) or {}), "resolved_by": operator, "resolved_at": now}),
            revision["revision_id"],
        ),
    )
    _update_item(db, revision["batch_id"], revision["revision_id"], "APPLIED", None, None, now)
    return {"applied": True, "version": new_version}


def _refresh_batch(db, batch_id, now):
    rows = db.execute("SELECT status FROM sync_batch_items WHERE batch_id = ?", (batch_id,)).fetchall()
    if not rows:
        return
    statuses = [row["status"] for row in rows]
    if all(item in BOOKED_STATUSES for item in statuses):
        status = "COMPLETED"
    elif any(item == "CONFLICT" for item in statuses):
        status = "HELD"
    else:
        status = "PARTIAL"
    batch = _row(db, "SELECT summary_json FROM sync_batches WHERE batch_id = ?", (batch_id,))
    if batch is None:
        return
    body = loads(batch["summary_json"]) or {}
    body["status"] = status
    body["status_label"] = BATCH_STATUS.get(status, status)
    body["counts"] = {
        row["status"]: row["n"]
        for row in db.execute(
            "SELECT status, COUNT(*) AS n FROM sync_batch_items WHERE batch_id = ? GROUP BY status",
            (batch_id,),
        )
    }
    db.execute(
        "UPDATE sync_batches SET status = ?, summary_json = ?, updated_at = ? WHERE batch_id = ?",
        (status, dumps(body), now, batch_id),
    )


def list_conflicts(status: str = "OPEN", business_key: str | None = None) -> dict:
    with read() as db:
        clauses, params = [], []
        if status and status.upper() != "ALL":
            clauses.append("status = ?")
            params.append(status.upper())
        if business_key:
            clauses.append("business_key = ?")
            params.append(business_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = db.execute(
            f"SELECT * FROM sync_conflicts {where} ORDER BY created_at, conflict_id", tuple(params)
        ).fetchall()
        return {"items": [_conflict_dict(row) for row in rows], "count": len(rows)}


# ---------------------------------------------------------------------------
# 中心复核
# ---------------------------------------------------------------------------


def review(payload: dict) -> dict:
    """中心复核写回；版本不匹配直接拒绝，不与并发同步抢写。"""

    if not isinstance(payload, dict):
        raise bad("BAD_REVIEW", "复核必须是对象")
    business_key = payload.get("business_key")
    if not isinstance(business_key, str) or not business_key.strip():
        raise bad("BAD_REVIEW", "缺少 business_key")
    business_key = business_key.strip()
    try:
        split_key(business_key)
    except ValueError as exc:
        raise bad("BAD_REVIEW", "business_key 必须是 issue|edition|page") from exc

    expected = payload.get("expected_version")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        raise bad("BAD_REVIEW", "expected_version 必须是整数")
    fields_payload = payload.get("fields")
    if not isinstance(fields_payload, dict):
        raise bad("BAD_REVIEW", "fields 必须是对象")
    approve = payload.get("approved_fields") or []
    if not isinstance(approve, list) or not all(isinstance(item, str) for item in approve):
        raise bad("BAD_REVIEW", "approved_fields 必须是字段名数组")
    operator = payload.get("operator")
    review_id = payload.get("review_id") or (
        "review-" + sha256(dumps(payload).encode("utf-8")).hexdigest()[:16]
    )
    if not isinstance(review_id, str) or not review_id.strip():
        raise bad("BAD_REVIEW", "review_id 必须是非空字符串")

    now = now_iso()
    with transaction() as db:
        prior = _row(db, "SELECT * FROM sync_reviews WHERE review_id = ?", (review_id,))
        if prior is not None:
            return {
                "review_id": review_id,
                "business_key": prior["business_key"],
                "version": prior["result_version"],
                "deduplicated": True,
            }

        record = _load_record(db, business_key)
        current = int(record["version"]) if record else 0
        if expected is not None and expected != current:
            raise bad(
                "STALE_VERSION",
                f"期望版本 {expected} 与当前版本 {current} 不一致，拒绝过期复核",
                409,
            )

        fields = normalize_fields(fields_payload)
        new_version = current + 1
        merged = dict(_record_fields(record)) if record else {}
        merged.update(fields)
        merged = {key: value for key, value in merged.items() if value is not None}
        approved = sorted(set(_approved_fields(record)) | set(approve)) if record else sorted(set(approve))
        approved_values = dict(_approved_values(record)) if record else {}
        approved_values.update({field: merged[field] for field in approve if field in merged})
        for field in approve:
            if field not in merged:
                approved_values.pop(field, None)

        if record is None:
            db.execute(
                "INSERT INTO sync_records (business_key, station_id, fields_json, version, head_revision, approved_fields_json, approved_values_json, approved_version, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    business_key,
                    payload.get("station_id"),
                    dumps(merged),
                    new_version,
                    dumps(approved),
                    dumps(approved_values),
                    new_version,
                    now,
                    now,
                ),
            )
        else:
            db.execute(
                "UPDATE sync_records SET fields_json = ?, version = ?, approved_fields_json = ?, approved_values_json = ?, approved_version = ?, updated_at = ? WHERE business_key = ?",
                (
                    dumps(merged),
                    new_version,
                    dumps(approved),
                    dumps(approved_values),
                    new_version,
                    now,
                    business_key,
                ),
            )
        _write_version(db, business_key, merged, new_version, None, now)

        db.execute(
            "INSERT INTO sync_reviews (review_id, business_key, expected_version, result_version, fields_json, approved_fields_json, operator, evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                review_id,
                business_key,
                expected,
                new_version,
                dumps(fields),
                dumps(approve),
                operator,
                dumps({"record_version_before": current}),
                now,
            ),
        )
        return {
            "review_id": review_id,
            "business_key": business_key,
            "version": new_version,
            "approved_fields": approved,
            "deduplicated": False,
        }


# ---------------------------------------------------------------------------
# 恢复与回读
# ---------------------------------------------------------------------------


def recover(payload: dict | None = None) -> dict:
    """重试未入账修订；已入账的修订与已接收的扫描件不会被重复处理。"""

    payload = payload or {}
    batch_id = payload.get("batch_id")
    revision_filter = payload.get("revision_id")

    with transaction() as db:
        clauses = [f"status IN ({','.join('?' for _ in RECOVERABLE_STATUSES)})"]
        params: list = list(RECOVERABLE_STATUSES)
        if batch_id:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        if revision_filter:
            clauses.append("(revision_id = ? OR revision_id LIKE ?)")
            params.extend([revision_filter, "%|" + revision_filter])
        rows = [
            dict(row)
            for row in db.execute(
                f"SELECT * FROM sync_revisions WHERE {' AND '.join(clauses)} ORDER BY seq",
                tuple(params),
            )
        ]

        results = []
        for row in rows:
            if row["status"] == "CONFLICT":
                results.append(
                    {
                        "revision_id": row["revision_id"],
                        "status": "CONFLICT",
                        "status_label": REVISION_STATUS["CONFLICT"],
                        "reason_code": row["reason_code"],
                        "message": "仍有未裁定冲突，先裁定再恢复",
                        "recovered": False,
                    }
                )
                continue

            spec = {
                "revision_id": row["revision_id"].split("|", 1)[-1],
                "business_key": row["business_key"],
                "station_id": row["station_id"],
                "parent_revision": row["parent_revision"],
                "base_version": row["base_version"],
                "expected_version": row["expected_version"],
                "mode": row["mode"],
                "fields": loads(row["fields_json"]),
                "changes": changes_from_json(loads(row["changes_json"])),
                "submitted_at": row["submitted_at"],
                "batch_id": row["batch_id"],
                "_index": 0,
            }
            digest = payload_digest(spec)
            if digest != row["payload_sha256"]:
                # 原始提报被损坏：保留证据，不猜测内容。
                raise bad("REVISION_CORRUPT", f"修订 {row['revision_id']} 的证据摘要不一致", 500)
            db.execute("DELETE FROM sync_conflicts WHERE revision_id = ? AND status = 'OPEN'", (row["revision_id"],))
            db.execute("DELETE FROM sync_revisions WHERE revision_id = ?", (row["revision_id"],))
            outcome = _apply(db, spec, row["revision_id"], digest, now_iso())
            outcome["recovered"] = outcome["status"] in BOOKED_STATUSES
            results.append(outcome)

        touched = sorted({row["batch_id"] for row in rows if row["batch_id"]})
        for item in touched:
            _refresh_batch(db, item, now_iso())

    return {"recovered": results, "count": len(results)}


def read_batch(batch_id: str) -> dict:
    with read() as db:
        batch = _row(db, "SELECT * FROM sync_batches WHERE batch_id = ?", (batch_id,))
        if batch is None:
            raise bad("BATCH_NOT_FOUND", "批次不存在", 404)
        summary = loads(batch["summary_json"]) or {}
        revisions = [
            _result_for_row(db, row)
            for row in db.execute(
                "SELECT * FROM sync_revisions WHERE batch_id = ? ORDER BY seq", (batch_id,)
            )
        ]
        items = [
            {
                "revision_id": row["revision_id"],
                "business_key": row["business_key"],
                "status": row["status"],
                "reason_code": row["reason_code"],
                "message": row["message"],
                "updated_at": row["updated_at"],
            }
            for row in db.execute(
                "SELECT * FROM sync_batch_items WHERE batch_id = ? ORDER BY revision_id",
                (batch_id,),
            )
        ]
        return {
            "batch_id": batch["batch_id"],
            "station_id": batch["station_id"],
            "status": batch["status"],
            "status_label": BATCH_STATUS.get(batch["status"], batch["status"]),
            "payload_sha256": batch["payload_sha256"],
            "summary": summary,
            "items": items,
            "revisions": revisions,
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
        }


def read_record(business_key: str) -> dict:
    with read() as db:
        record = _load_record(db, business_key)
        if record is None:
            raise bad("RECORD_NOT_FOUND", "记录不存在", 404)
        history = [
            {
                "version": row["version"],
                "revision_id": row["revision_id"],
                "approved_fields": loads(row["approved_fields_json"]) or [],
                "created_at": row["created_at"],
                "fields": loads(row["fields_json"]) or {},
            }
            for row in db.execute(
                "SELECT * FROM sync_record_versions WHERE business_key = ? ORDER BY version",
                (business_key,),
            )
        ]

        revisions = [
            {
                "revision_id": row["revision_id"],
                "station_id": row["station_id"],
                "status": row["status"],
                "reason_code": row["reason_code"],
                "booked_at": row["booked_at"],
            }
            for row in db.execute(
                "SELECT * FROM sync_revisions WHERE business_key = ? ORDER BY seq",
                (business_key,),
            )
        ]
        conflicts = [
            _conflict_dict(row)
            for row in db.execute(
                "SELECT * FROM sync_conflicts WHERE business_key = ? ORDER BY created_at",
                (business_key,),
            )
        ]
        scans = [
            {
                "station_id": row["station_id"],
                "sha256": row["sha256"],
                "business_key": row["business_key"],
                "file_name": row["file_name"],
                "line_no": row["line_no"],
                "batch_id": row["batch_id"],
                "received_at": row["received_at"],
            }
            for row in db.execute(
                "SELECT * FROM sync_scans WHERE business_key = ? ORDER BY received_at, file_name",
                (business_key,),
            )
        ]
        return {
            "business_key": record["business_key"],
            "fields": _record_fields(record),
            "version": record["version"],
            "head_revision": record["head_revision"],
            "approved_fields": _approved_fields(record),
            "approved_values": _approved_values(record),
            "approved_version": record["approved_version"],
            "history": history,
            "revisions": revisions,
            "conflicts": conflicts,
            "scans": scans,
            "updated_at": record["updated_at"],
        }


def list_scans(station_id: str | None = None, business_key: str | None = None) -> dict:
    with read() as db:
        clauses, params = [], []
        if station_id:
            clauses.append("station_id = ?")
            params.append(station_id)
        if business_key:
            clauses.append("business_key = ?")
            params.append(business_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = db.execute(
            f"SELECT * FROM sync_scans {where} ORDER BY received_at, file_name", tuple(params)
        ).fetchall()
        items = [
            {
                "station_id": row["station_id"],
                "sha256": row["sha256"],
                "business_key": row["business_key"],
                "file_name": row["file_name"],
                "line_no": row["line_no"],
                "batch_id": row["batch_id"],
                "received_at": row["received_at"],
            }
            for row in rows
        ]
        return {"items": items, "count": len(items)}


def list_revisions(
    station_id: str | None = None,
    business_key: str | None = None,
    status: str | None = None,
) -> dict:
    with read() as db:
        clauses, params = [], []
        if station_id:
            clauses.append("station_id = ?")
            params.append(station_id)
        if business_key:
            clauses.append("business_key = ?")
            params.append(business_key)
        if status:
            clauses.append("status = ?")
            params.append(status.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = db.execute(
            f"SELECT * FROM sync_revisions {where} ORDER BY seq", tuple(params)
        ).fetchall()
        items = [_result_for_row(db, row) for row in rows]
        return {"items": items, "count": len(items)}


def list_quarantine(station_id: str | None = None) -> dict:
    with read() as db:
        clauses, params = [], []
        if station_id:
            clauses.append("station_id = ?")
            params.append(station_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = db.execute(
            f"SELECT * FROM sync_scan_quarantine {where} ORDER BY created_at, line_no",
            tuple(params),
        ).fetchall()
        items = [
            {
                "quarantine_id": row["quarantine_id"],
                "station_id": row["station_id"],
                "batch_id": row["batch_id"],
                "line_no": row["line_no"],
                "reason_code": row["reason_code"],
                "raw_value": row["raw_value"],
                "message": row["message"],
            }
            for row in rows
        ]
        return {"items": items, "count": len(items)}


def progress(station_id: str | None = None) -> dict:
    """核对站点修订进度和已入账扫描。"""

    with read() as db:
        revision_clauses, revision_params = [], []
        if station_id:
            revision_clauses.append("station_id = ?")
            revision_params.append(station_id)
        where = f"WHERE {' AND '.join(revision_clauses)}" if revision_clauses else ""
        by_status = {
            row["status"]: row["n"]
            for row in db.execute(
                f"SELECT status, COUNT(*) AS n FROM sync_revisions {where} GROUP BY status",
                tuple(revision_params),
            )
        }
        revision_total = _row(
            db, f"SELECT COUNT(*) AS n FROM sync_revisions {where}", tuple(revision_params)
        )["n"]
        booked = by_status.get("APPLIED", 0) + by_status.get("UNCHANGED", 0)

        scan_clauses, scan_params = [], []
        quarantine_clauses, quarantine_params = [], []
        if station_id:
            scan_clauses.append("station_id = ?")
            scan_params.append(station_id)
            quarantine_clauses.append("station_id = ?")
            quarantine_params.append(station_id)
        scan_where = f"WHERE {' AND '.join(scan_clauses)}" if scan_clauses else ""
        quarantine_where = (
            f"WHERE {' AND '.join(quarantine_clauses)}" if quarantine_clauses else ""
        )
        scans = _row(
            db,
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT business_key) AS keys FROM sync_scans {scan_where}",
            tuple(scan_params),
        )
        quarantined = _row(
            db,
            f"SELECT COUNT(*) AS n FROM sync_scan_quarantine {quarantine_where}",
            tuple(quarantine_params),
        )["n"]

        conflict_sql = "SELECT COUNT(*) AS n FROM sync_conflicts WHERE status = 'OPEN'"
        conflict_params: tuple = ()
        if station_id:
            conflict_sql += " AND revision_id IN (SELECT revision_id FROM sync_revisions WHERE station_id = ?)"
            conflict_params = (station_id,)
        open_conflicts = _row(db, conflict_sql, conflict_params)["n"]

        stations = [
            row["station_id"]
            for row in db.execute("SELECT DISTINCT station_id FROM sync_batches ORDER BY station_id")
        ]
        pending_by_status = {
            key: value for key, value in by_status.items() if key not in BOOKED_STATUSES
        }
        return {
            "station_id": station_id,
            "stations": stations,
            "revisions": {
                "total": revision_total,
                "booked": booked,
                "pending": revision_total - booked,
                "by_status": by_status,
                "pending_by_status": pending_by_status,
            },
            "scans": {
                "received": scans["n"],
                "business_keys": scans["keys"],
                "quarantined": quarantined,
            },
            "conflicts": {"open": open_conflicts},
        }
