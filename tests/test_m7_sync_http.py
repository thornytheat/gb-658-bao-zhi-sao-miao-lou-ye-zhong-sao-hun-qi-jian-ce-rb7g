"""M7 的 HTTP 接口与并发行为。"""

import threading

import pytest
from fastapi.testclient import TestClient

import app.store as store
from app.main import app
from app.modules.m7_sync import service

from sync_helpers import batch, make_csv, revision, scan_row

KEY = "2026-11-01|01|001"


@pytest.fixture()
def client(db):
    return TestClient(app)


def test_sync_page_is_reachable(client):
    response = client.get("/sync")
    assert response.status_code == 200 and "panel" in response.text


def test_receive_and_read_back_over_http(client):
    payload = batch(
        scans=make_csv(scan_row("001")),
        revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 90})],
    )
    created = client.post("/api/sync/batches", json=payload)
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "COMPLETED"

    detail = client.get(f"/api/sync/batches/{body['batch_id']}")
    assert detail.status_code == 200 and detail.json()["items"][0]["status"] == "APPLIED"

    record = client.get(f"/api/sync/records/{KEY}")
    assert record.status_code == 200 and record.json()["fields"]["clarity"] == 90

    progress = client.get("/api/sync/progress", params={"station_id": "ST-01"})
    assert progress.json()["scans"]["received"] == 1

    scans = client.get("/api/sync/scans", params={"business_key": KEY})
    assert scans.json()["count"] == 1


def test_http_conflict_listing_and_resolution(client, db):
    service.review(
        {
            "business_key": KEY,
            "review_id": "rv",
            "fields": {"issue": "2026-11-01", "edition": "01", "page": "001", "clarity": 82},
            "approved_fields": ["page"],
        }
    )
    client.post(
        "/api/sync/batches",
        json=batch(
            station_id="A",
            revisions=[revision("a1", KEY, base_version=1, changes={"clarity": {"to": 95}})],
        ),
    )
    held = client.post(
        "/api/sync/batches",
        json=batch(
            station_id="B",
            revisions=[revision("b1", KEY, base_version=1, changes={"clarity": {"to": 60}})],
        ),
    ).json()["revisions"][0]
    conflict_id = held["conflicts"][0]["conflict_id"]

    listed = client.get("/api/sync/conflicts", params={"business_key": KEY}).json()
    assert listed["count"] == 1 and listed["items"][0]["base"] == 82

    resolved = client.post(
        f"/api/sync/conflicts/{conflict_id}/resolve",
        json={"value_choice": "theirs", "operator": "lizhi"},
    )
    assert resolved.status_code == 200 and resolved.json()["booked"] is True
    assert client.get(f"/api/sync/records/{KEY}").json()["fields"]["clarity"] == 60


def test_http_stale_review_returns_409(client, db):
    service.receive(batch(revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 80})]))
    response = client.post(
        "/api/sync/reviews",
        json={
            "business_key": KEY,
            "review_id": "rv-stale",
            "expected_version": 0,
            "fields": {"page": "001"},
            "approved_fields": ["page"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STALE_VERSION"


def test_http_domain_errors_use_detail_code(client):
    response = client.post("/api/sync/batches", json={"revisions": []})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "BAD_BATCH"


def test_database_path_redirected_before_app_startup(db):
    assert str(store.DATABASE_PATH).endswith("sync.sqlite3")


def test_concurrent_review_and_sync_do_not_double_book(db, monkeypatch):
    service.receive(batch(revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 80})]))

    started = threading.Barrier(2)
    outcomes = {}

    def do_review():
        started.wait()
        try:
            outcomes["review"] = service.review(
                {
                    "business_key": KEY,
                    "review_id": "rv-race",
                    "expected_version": 1,
                    "fields": {"page": "001", "clarity": 88},
                    "approved_fields": ["page"],
                }
            )
        except Exception as exc:  # pragma: no cover - 只在并发失败时记录
            outcomes["review"] = exc

    def do_sync():
        started.wait()
        try:
            outcomes["sync"] = service.receive(
                batch(
                    station_id="ST-09",
                    revisions=[revision("r2", KEY, expected_version=1, changes={"clarity": {"to": 99}})],
                )
            )
        except Exception as exc:  # pragma: no cover
            outcomes["sync"] = exc

    threads = [threading.Thread(target=do_review), threading.Thread(target=do_sync)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    record = service.read_record(KEY)
    # 两个写者都拿版本 1 作前提；只有一个能推进版本，另一个必须被过期检查挡住。
    applied = [outcome for outcome in outcomes.values() if not isinstance(outcome, Exception)]
    assert record["version"] == 2
    sync_result = outcomes["sync"]
    if isinstance(sync_result, dict):
        assert sync_result["revisions"][0]["status"] in ("APPLIED", "REJECTED")
        if sync_result["revisions"][0]["status"] == "REJECTED":
            assert sync_result["revisions"][0]["reason_code"] == "STALE_VERSION"


def test_http_revisions_and_quarantine_readback(client, db):
    text = (
        "issue,edition,page,file_name,sha256,phash,clarity,header_issue\n"
        "2026-11-01,01,001,a.tif," + "a" * 64 + ",0f0f,90,2026-11-01\n"
        "2026-11-01,01,002\n"
    )
    client.post(
        "/api/sync/batches",
        json=batch(
            scans=text,
            revisions=[revision("r1", KEY, fields={"page": "001", "clarity": 90})],
        ),
    )
    revisions = client.get("/api/sync/revisions", params={"station_id": "ST-01"}).json()
    assert revisions["count"] == 1 and revisions["items"][0]["status"] == "APPLIED"

    quarantine = client.get("/api/sync/quarantine", params={"station_id": "ST-01"}).json()
    assert quarantine["count"] == 1 and quarantine["items"][0]["line_no"] == 3
