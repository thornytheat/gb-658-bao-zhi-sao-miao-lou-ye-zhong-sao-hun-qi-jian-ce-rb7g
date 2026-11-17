def make_csv(*rows, header="issue,edition,page,file_name,sha256,phash,clarity,header_issue"):
    lines = [header] + list(rows)
    return "\n".join(lines) + "\n"


def scan_row(page="001", digest=None, clarity=90, file_name=None, issue="2026-11-01", edition="01"):
    digest = digest or (page * 64)[:64]
    return f"{issue},{edition},{page},{file_name or f'scan_{page}.tif'},{digest},0f0f,{clarity},{issue}"


def revision(revision_id, business_key, fields=None, **extra):
    body = {"revision_id": revision_id, "business_key": business_key}
    if fields is not None or "changes" not in extra:
        body["fields"] = fields or {}
    body.update(extra)
    return body


def batch(station_id="ST-01", scans=None, revisions=None, **extra):
    payload = {
        "station_id": station_id,
        "scans": scans if scans is not None else make_csv(*[]),
        "revisions": revisions or [],
    }
    payload.update(extra)
    return payload
