"""M7 离线同步端到端行为：接收、合并、挂起、裁定、恢复与回读。"""

import pytest

from app.modules.m7_sync import service

from sync_helpers import batch, make_csv, revision, scan_row

KEY = "2026-11-01|01|001"


def seed_center(db):
    """中心已核准期版页归属，站点底稿尚未看到这次复核。"""

    return service.review(
        {
            "business_key": KEY,
            "review_id": "review-001",
            "fields": {
                "issue": "2026-11-01",
                "edition": "01",
                "page": "001",
                "clarity": 82,
                "header_issue": "2026-11-01",
            },
            "approved_fields": ["issue", "edition", "page"],
            "operator": "zhongxin",
        }
    )


def test_receive_imports_scans_and_books_non_conflicting_revision(db):
    result = service.receive(
        batch(
            scans=make_csv(scan_row("001"), scan_row("002")),
            revisions=[
                revision(
                    "r1",
                    KEY,
                    fields={"issue": "2026-11-01", "edition": "01", "page": "001", "clarity": 90},
                )
            ],
        )
    )
    assert result["status"] == "COMPLETED"
    assert result["scans"]["accepted"] == 2 and result["scans"]["newly_received"] == 2
    assert result["revisions"][0]["status"] == "APPLIED"

    record = service.read_record(KEY)
    assert record["version"] == 1 and record["fields"]["clarity"] == 90
    scans = service.list_scans(business_key=KEY)
    assert scans["count"] == 1 and scans["items"][0]["file_name"] == "scan_001.tif"


def test_same_batch_retry_does_not_receive_scans_twice(db):
    payload = batch(scans=make_csv(scan_row("001")), revisions=[])
    first = service.receive(payload)
    second = service.receive(payload)
    assert first["batch_id"] == second["batch_id"]
    assert second["deduplicated"] is True
    assert service.progress("ST-01")["scans"]["received"] == 1


def test_replayed_revision_same_content_is_deduplicated(db):
    body = batch(
        revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 90})],
    )
    service.receive(body)
    replay = service.receive(batch(revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 90})]))
    assert replay["revisions"][0]["deduplicated"] is True
    assert service.read_record(KEY)["version"] == 1


def test_revision_id_reused_with_different_content_is_rejected(db):
    service.receive(batch(revisions=[revision("r1", KEY, fields={"clarity": 90})]))
    with pytest.raises(Exception) as exc:
        service.receive(batch(revisions=[revision("r1", KEY, fields={"clarity": 50})]))
    assert getattr(exc.value, "code", None) == "REVISION_CONFLICT"


def test_offline_revision_merges_onto_common_base(db):
    seed_center(db)  # 中心已核准 page=001，版本 1
    result = service.receive(
        batch(
            scans=make_csv(scan_row("001"), scan_row("002")),
            revisions=[
                revision(
                    "r2",
                    KEY,
                    base_version=1,
                    changes={"clarity": {"from": 82, "to": 93}},
                )
            ],
        )
    )
    outcome = result["revisions"][0]
    assert outcome["status"] == "APPLIED" and outcome["record_version_after"] == 2
    record = service.read_record(KEY)
    # 站点只改了清晰度，已核准的期版页归属保持不动。
    assert record["fields"]["clarity"] == 93
    assert record["fields"]["page"] == "001"
    assert record["approved_fields"] == ["edition", "issue", "page"]


def test_old_base_cannot_regress_approved_page(db):
    seed_center(db)
    result = service.receive(
        batch(
            scans=make_csv(scan_row("001")),
            revisions=[
                revision(
                    "r3",
                    KEY,
                    base_version=1,
                    changes={
                        "page": {"from": "001", "to": "004"},
                        "clarity": {"from": 82, "to": 60},
                    },
                )
            ],
        )
    )
    outcome = result["revisions"][0]
    assert outcome["status"] == "CONFLICT"
    assert outcome["reason_code"] == "APPROVED_OVERRIDE"
    conflict = outcome["conflicts"][0]
    assert conflict["field"] == "page"
    assert (conflict["ours"], conflict["theirs"]) == ("001", "004")

    # 未裁定前中心值不被覆盖，但同单的清晰度照常合并入账。
    record = service.read_record(KEY)
    assert record["fields"]["page"] == "001"
    assert record["fields"]["clarity"] == 60
    assert record["version"] == 2
    assert outcome["evidence"]["held_fields"] == ["page"]
    # 新扫描仍然入账 —— 整单拒收会丢扫描。
    assert service.list_scans(business_key=KEY)["count"] == 1


def test_conflict_resolution_keeps_unrelated_field_changes(db):
    seed_center(db)
    result = service.receive(
        batch(
            station_id="ST-02",
            revisions=[
                revision(
                    "c1",
                    KEY,
                    base_version=1,
                    changes={
                        "clarity": {"from": 82, "to": 60},
                        "header_issue": {"from": "2026-11-01", "to": "2026-11-02"},
                        "page": {"from": "001", "to": "004"},
                    },
                )
            ],
        )
    )
    outcome = result["revisions"][0]
    conflict = outcome["conflicts"][0]
    assert conflict["field"] == "page"
    service.resolve(conflict["conflict_id"], {"value_choice": "ours"})

    record = service.read_record(KEY)
    assert record["fields"]["clarity"] == 60  # 非冲突字段已入账
    assert record["fields"]["header_issue"] == "2026-11-02"
    assert record["fields"]["page"] == "001"  # 裁定采纳中心值


def test_same_field_divergence_holds_both_sides_then_books_on_resolution(db):
    seed_center(db)
    first = service.receive(
        batch(
            station_id="ST-01",
            revisions=[revision("a1", KEY, base_version=1, changes={"clarity": {"to": 95}})],
        )
    )
    assert first["revisions"][0]["status"] == "APPLIED"
    assert service.read_record(KEY)["version"] == 2

    second = service.receive(
        batch(
            station_id="ST-02",
            revisions=[
                revision("b1", KEY, base_version=1, changes={"clarity": {"to": 60}})
            ],
        )
    )
    held = second["revisions"][0]
    assert held["status"] == "CONFLICT"
    conflict = held["conflicts"][0]
    assert (conflict["base"], conflict["ours"], conflict["theirs"]) == (82, 95, 60)
    assert conflict["evidence"]["reason_code"] == "FIELD_CONFLICT"

    listed = service.list_conflicts("OPEN", KEY)
    assert listed["count"] == 1

    resolved = service.resolve(conflict["conflict_id"], {"value_choice": "theirs", "operator": "lizhi"})
    assert resolved["booked"] is True
    record = service.read_record(KEY)
    assert record["fields"]["clarity"] == 60 and record["version"] == 3
    assert service.list_conflicts("OPEN", KEY)["count"] == 0
    assert record["conflicts"][0]["resolution_source"] == "theirs"


def test_resolution_stops_when_record_moved_since_hold(db):
    seed_center(db)
    service.receive(
        batch(station_id="ST-01", revisions=[revision("a1", KEY, base_version=1, changes={"clarity": {"to": 95}})])
    )
    held = service.receive(
        batch(station_id="ST-02", revisions=[revision("b1", KEY, base_version=1, changes={"clarity": {"to": 60}})])
    )
    conflict_id = held["revisions"][0]["conflicts"][0]["conflict_id"]

    service.receive(
        batch(station_id="ST-01", revisions=[revision("a2", KEY, base_version=2, changes={"clarity": {"to": 88}})])
    )
    with pytest.raises(Exception) as exc:
        service.resolve(conflict_id, {"value_choice": "theirs"})
    assert getattr(exc.value, "code", None) == "STALE_RESOLUTION"


def test_missing_parent_is_staged_not_merged(db):
    result = service.receive(
        batch(
            scans=make_csv(scan_row("001")),
            revisions=[
                revision("child", KEY, parent_revision="missing-root", changes={"clarity": {"to": 99}})
            ],
        )
    )
    outcome = result["revisions"][0]
    assert outcome["status"] == "STAGED" and outcome["reason_code"] == "MISSING_PARENT"
    # 扫描照常入账，修订不落记录，等前序补齐。
    assert service.list_scans(business_key=KEY)["count"] == 1
    assert service.list_revisions(business_key=KEY)["items"][0]["status"] == "STAGED"
    assert service.progress("ST-01")["revisions"]["pending_by_status"] == {"STAGED": 1}


def test_forked_parent_refuses_automatic_merge(db):
    service.receive(batch(revisions=[revision("root", KEY, fields={"page": "001", "clarity": 80})]))
    other = "2026-11-01|01|002"
    service.receive(batch(revisions=[revision("root2", other, fields={"page": "002", "clarity": 80})]))
    result = service.receive(
        batch(revisions=[revision("fork", KEY, parent_revision="root2", changes={"clarity": {"to": 70}})])
    )
    outcome = result["revisions"][0]
    assert outcome["status"] == "REJECTED" and outcome["reason_code"] == "PARENT_DIVERGED"


def test_parent_staged_child_stays_staged(db):
    service.receive(
        batch(
            revisions=[revision("root", KEY, parent_revision="nowhere", changes={"clarity": {"to": 70}})]
        )
    )
    result = service.receive(
        batch(revisions=[revision("child", KEY, parent_revision="root", changes={"clarity": {"to": 60}})])
    )
    assert result["revisions"][0]["status"] == "STAGED"
    assert result["revisions"][0]["reason_code"] == "MISSING_PARENT"


def test_center_review_rejects_stale_expected_version(db):
    service.receive(batch(revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 80})]))
    with pytest.raises(Exception) as exc:
        service.review(
            {
                "business_key": KEY,
                "review_id": "review-stale",
                "expected_version": 0,
                "fields": {"page": "001", "clarity": 85},
                "approved_fields": ["page"],
            }
        )
    assert getattr(exc.value, "code", None) == "STALE_VERSION"
    assert service.read_record(KEY)["version"] == 1


def test_sync_rejects_revision_based_on_stale_expected_version(db):
    service.receive(batch(revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 80})]))
    result = service.receive(
        batch(revisions=[revision("r2", KEY, expected_version=0, changes={"clarity": {"to": 99}})])
    )
    outcome = result["revisions"][0]
    assert outcome["status"] == "REJECTED" and outcome["reason_code"] == "STALE_VERSION"
    assert service.read_record(KEY)["fields"]["clarity"] == 80


def test_recover_retries_unbooked_revisions_without_receiving_scans_again(db):
    service.receive(
        batch(
            scans=make_csv(scan_row("001")),
            revisions=[revision("child", KEY, parent_revision="root", changes={"clarity": {"to": 77}})],
        )
    )
    assert service.progress("ST-01")["revisions"]["by_status"] == {"STAGED": 1}

    service.receive(batch(revisions=[revision("root", KEY, fields={"page": "001", "clarity": 70})]))
    scans_before = service.progress("ST-01")["scans"]["received"]

    outcome = service.recover({"revision_id": "child"})
    assert outcome["count"] == 1 and outcome["recovered"][0]["status"] == "APPLIED"
    assert outcome["recovered"][0]["record_version_after"] == 2
    assert service.progress("ST-01")["scans"]["received"] == scans_before
    assert service.read_record(KEY)["fields"]["clarity"] == 77


def test_interrupted_batch_can_resume_from_unbooked_point(db):
    # 第一批次只有子修订：前序缺失，挂起但扫描已入账。
    first = service.receive(
        batch(
            scans=make_csv(scan_row("001")),
            revisions=[
                revision("child", KEY, parent_revision="root", changes={"clarity": {"to": 77}}),
                revision("free", "2026-11-01|01|009", fields={"page": "009", "clarity": 50}),
            ],
        )
    )
    statuses = {item["client_revision_id"]: item["status"] for item in first["revisions"]}
    assert statuses == {"child": "STAGED", "free": "APPLIED"}

    # 前序补到后，按批次恢复只处理未入账项，不重复接收扫描。
    service.receive(batch(revisions=[revision("root", KEY, fields={"page": "001", "clarity": 70})]))
    resumed = service.recover({"batch_id": first["batch_id"]})
    assert [item["client_revision_id"] for item in resumed["recovered"]] == ["child"]
    assert resumed["recovered"][0]["status"] == "APPLIED"


def test_recover_leaves_open_conflicts_for_adjudication(db):
    seed_center(db)
    service.receive(
        batch(station_id="ST-01", revisions=[revision("a1", KEY, base_version=1, changes={"clarity": {"to": 95}})])
    )
    service.receive(
        batch(station_id="ST-02", revisions=[revision("b1", KEY, base_version=1, changes={"clarity": {"to": 60}})])
    )
    outcome = service.recover({})
    assert outcome["recovered"][0]["status"] == "CONFLICT"
    assert outcome["recovered"][0]["recovered"] is False


def test_progress_reports_station_revisions_and_booked_scans(db):
    service.receive(
        batch(
            scans=make_csv(scan_row("001"), "2026-11-01,01,002,scan_002.tif," + "b" * 64 + ",0f1f,80,2026-11-01"),
            revisions=[
                revision("r1", KEY, fields={"page": "001", "clarity": 90}),
                revision("r2", "2026-11-01|01|002", parent_revision="absent", changes={"clarity": {"to": 88}}),
            ],
        )
    )
    progress = service.progress("ST-01")
    assert progress["revisions"]["total"] == 2
    assert progress["revisions"]["booked"] == 1
    assert progress["revisions"]["pending_by_status"] == {"STAGED": 1}
    assert progress["scans"] == {"received": 2, "business_keys": 2, "quarantined": 0}


def test_readback_batch_has_items_and_revision_evidence(db):
    created = service.receive(
        batch(
            scans=make_csv(scan_row("001")),
            revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 90})],
        )
    )
    detail = service.read_batch(created["batch_id"])
    assert detail["status"] == "COMPLETED"
    assert detail["items"][0]["status"] == "APPLIED"
    assert detail["revisions"][0]["evidence"]["record_version"] == 0


def test_quarantine_keeps_line_numbers_and_raw_values(db):
    text = (
        "issue,edition,page,file_name,sha256,phash,clarity,header_issue\n"
        "2026-11-01,01,001,a.tif," + "a" * 64 + ",0f0f,90,2026-11-01\n"
        "2026-11-01,01,002\n"
    )
    result = service.receive(batch(scans=text, revisions=[]))
    assert result["scans"]["accepted"] == 1 and result["scans"]["quarantined"] == 1
    entries = service.list_quarantine("ST-01")["items"]
    assert entries[0]["reason_code"] == "COLUMN_COUNT" and entries[0]["line_no"] == 3


def test_truncated_tail_scan_is_quarantined_not_accepted(db):
    text = make_csv(scan_row("001")).rstrip("\n")
    result = service.receive(batch(scans=text, revisions=[]))
    assert result["scans"]["accepted"] == 0
    assert service.list_quarantine("ST-01")["items"][0]["reason_code"] == "TRUNCATED_INPUT"


def test_review_writes_approved_values_and_bumps_version(db):
    service.receive(batch(revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 80})]))
    reviewed = service.review(
        {
            "business_key": KEY,
            "review_id": "review-9",
            "expected_version": 1,
            "fields": {"page": "001", "edition": "01"},
            "approved_fields": ["page"],
        }
    )
    assert reviewed["version"] == 2
    record = service.read_record(KEY)
    assert record["approved_values"] == {"page": "001"}
    assert [item["version"] for item in record["history"]] == [1, 2]


def test_review_during_offline_edit_keeps_both_sides_as_evidence(db):
    """中心核准页归属后，站点拿旧底稿改同一页：挂起并同时保留中心与站点证据。"""

    seed_center(db)
    result = service.receive(
        batch(
            scans=make_csv(scan_row("001")),
            revisions=[
                revision(
                    "old-base-1",
                    KEY,
                    base_version=1,
                    changes={"page": {"from": "001", "to": "004"}},
                )
            ],
        )
    )
    conflict = result["revisions"][0]["conflicts"][0]
    assert conflict["reason_code"] == "APPROVED_OVERRIDE"
    assert conflict["ours_present"] and conflict["theirs_present"]
    assert conflict["evidence"]["reason_code"] == "APPROVED_OVERRIDE"

    # 站点重传完整 CSV 与同一修订：不重复收扫描，也不重复挂冲突。
    retry = service.receive(
        batch(
            scans=make_csv(scan_row("001")),
            revisions=[
                revision(
                    "old-base-1",
                    KEY,
                    base_version=1,
                    changes={"page": {"from": "001", "to": "004"}},
                )
            ],
        )
    )
    assert retry["deduplicated"] is True
    assert len(service.list_conflicts("ALL", KEY)["items"]) == 1
    assert service.progress("ST-01")["scans"]["received"] == 1


def test_state_survives_reconnect(db):
    service.receive(batch(revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 90})]))
    # 新连接（模拟进程重启后的下一次请求）回读一致。
    again = service.read_record(KEY)
    assert again["version"] == 1 and again["fields"]["clarity"] == 90
    assert service.progress("ST-01")["revisions"]["booked"] == 1


def test_recover_is_idempotent_after_success(db):
    service.receive(batch(revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 90})]))
    assert service.recover({})["count"] == 0
    assert service.read_record(KEY)["version"] == 1


def test_recover_rejects_corrupted_evidence(db):
    from app.store import transaction

    service.receive(
        batch(revisions=[revision("child", KEY, parent_revision="root", changes={"clarity": {"to": 70}})])
    )
    with transaction() as conn:
        conn.execute("UPDATE sync_revisions SET fields_json = '{\"page\":\"999\"}' WHERE revision_id = ?", ("ST-01|child",))
    with pytest.raises(Exception) as exc:
        service.recover({"revision_id": "child"})
    assert getattr(exc.value, "code", None) == "REVISION_CORRUPT"


def test_resolution_of_approved_field_updates_approved_baseline(db):
    seed_center(db)
    held = service.receive(
        batch(
            station_id="ST-02",
            revisions=[
                revision(
                    "p1",
                    KEY,
                    base_version=1,
                    changes={"page": {"from": "001", "to": "004"}},
                )
            ],
        )
    )["revisions"][0]
    resolved = service.resolve(held["conflicts"][0]["conflict_id"], {"value_choice": "theirs"})
    assert resolved["booked"] is True and resolved["record_version"] == 2
    record = service.read_record(KEY)
    assert record["fields"]["page"] == "004"
    assert record["approved_values"]["page"] == "004"

    # 后续同一页再改一次，不应被旧核准值误拦。
    follow = service.receive(
        batch(
            station_id="ST-02",
            revisions=[
                revision("p2", KEY, base_version=2, changes={"page": {"from": "004", "to": "006"}})
            ],
        )
    )
    assert follow["revisions"][0]["status"] == "CONFLICT"  # 已核准字段再次改动仍需复核裁定
    assert follow["revisions"][0]["conflicts"][0]["ours"] == "004"


def test_csv_and_row_object_manifests_are_equivalent(db):
    by_csv = service.receive(
        batch(
            scans=make_csv(scan_row("001")),
            revisions=[revision("c1", "2026-11-01|01|001", fields={"page": "001", "clarity": 90})],
        )
    )
    rows = [
        {
            "issue": "2026-11-01",
            "edition": "01",
            "page": "002",
            "file_name": "scan_002.tif",
            "sha256": "002" * 21 + "0",
            "phash": "0f1f",
            "clarity": 80,
            "header_issue": "2026-11-01",
        }
    ]
    by_rows = service.receive(
        batch(
            station_id="ST-02",
            scans=rows,
            revisions=[revision("c2", "2026-11-01|01|002", fields={"page": "002", "clarity": 80})],
        )
    )
    assert by_csv["scans"]["accepted"] == 1 and by_rows["scans"]["accepted"] == 1
    assert service.list_scans("ST-02")["items"][0]["line_no"] == 1
