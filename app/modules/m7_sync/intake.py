"""站点批次中的真实清单采信。

复用 M1 的解析与隔离规则，再按 M2 的契约规范化期号、版次和页码；清单只负责
"收到哪些扫描件"，不参与重扫优选。
"""

import json
import re
from hashlib import sha256

from app.errors import DomainError
from app.modules.m1_intake.integrity import complete_tail, valid_sha256
from app.modules.m1_intake.parser import parse_manifest
from app.modules.m2_issue.catalog import business_key as make_business_key
from app.modules.m2_issue.rules import normalize_edition, normalize_issue, normalize_page

REQUIRED = ("issue", "edition", "page", "file_name", "sha256", "phash", "clarity", "header_issue")
HEX = re.compile(r"^[0-9a-f]+$")


class ScanRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _list_rows(manifest: list) -> tuple[list[dict], list[dict]]:
    accepted, bad = [], []
    for index, raw in enumerate(manifest, start=1):
        line_no = index
        if not isinstance(raw, dict):
            bad.append({"line_no": line_no, "reason_code": "COLUMN_COUNT", "raw": _canon(raw)})
            continue
        missing = [name for name in REQUIRED if raw.get(name) is None]
        if missing:
            bad.append(
                {
                    "line_no": line_no,
                    "reason_code": "COLUMN_COUNT",
                    "raw": _canon(raw),
                    "missing": missing,
                }
            )
            continue
        if not str(raw.get("issue") or "").strip() or not str(raw.get("page") or "").strip():
            bad.append({"line_no": line_no, "reason_code": "EMPTY_VALUE", "raw": _canon(raw)})
            continue
        row = {name: raw.get(name) for name in REQUIRED}
        row["line_no"] = line_no
        accepted.append(row)
    return accepted, bad


def parse_manifest_input(manifest) -> dict:
    """把 CSV 文本或行对象列表解析为 {accepted, quarantine, input, tail_complete}。"""

    if manifest is None:
        return {"input": 0, "accepted": [], "quarantine": [], "tail_complete": True}
    if isinstance(manifest, list):
        accepted, bad = _list_rows(manifest)
        return {
            "input": len(manifest),
            "accepted": accepted,
            "quarantine": bad,
            "tail_complete": True,
        }
    if not isinstance(manifest, str):
        raise DomainError("BAD_MANIFEST", "manifest 必须是 CSV 文本或行对象列表")

    accepted, bad = parse_manifest(manifest)
    tail_complete = complete_tail(manifest)
    if not tail_complete and manifest.strip():
        # TRAP-16：末行没有换行符时视为截断，即使 csv 模块读出了看似完整的字段。
        last_line = manifest.count("\n") + 1
        for index, row in enumerate(accepted):
            if row["line_no"] == last_line:
                bad.append(
                    {
                        "line_no": last_line,
                        "reason_code": "TRUNCATED_INPUT",
                        "raw": _canon({key: value for key, value in row.items() if key != "line_no"}),
                    }
                )
                accepted.pop(index)
                break
        else:
            for row in bad:
                if row["line_no"] == last_line and last_line > 1:
                    row["original_reason_code"] = row["reason_code"]
                    row["reason_code"] = "TRUNCATED_INPUT"
                    break
    return {
        "input": len(accepted) + len(bad),
        "accepted": accepted,
        "quarantine": bad,
        "tail_complete": tail_complete,
    }


def normalize_scan(row: dict) -> dict:
    """校验并规范化一条扫描行；失败抛 ScanRejected，保留原因码。"""

    try:
        issue = normalize_issue(str(row["issue"]))
        edition = normalize_edition(str(row["edition"]))
        page = normalize_page(str(row["page"]))
    except (ValueError, TypeError) as exc:
        raise ScanRejected("BAD_FORMAT", f"期版页格式不正确: {exc}") from exc

    file_name = str(row["file_name"]).strip()
    if not file_name:
        raise ScanRejected("EMPTY_VALUE", "file_name 为空")

    digest = str(row["sha256"]).strip().lower()
    if not valid_sha256(digest):
        raise ScanRejected("BAD_FORMAT", "sha256 必须是 64 位十六进制")

    phash = str(row["phash"]).strip().lower()
    if not phash or not HEX.fullmatch(phash) or len(phash) % 2:
        raise ScanRejected("BAD_FORMAT", "phash 必须是偶数位十六进制")

    clarity_raw = str(row["clarity"]).strip()
    try:
        clarity = int(clarity_raw)
    except (TypeError, ValueError) as exc:
        raise ScanRejected("BAD_FORMAT", "clarity 必须是整数") from exc
    if not 0 <= clarity <= 100:
        raise ScanRejected("BAD_FORMAT", "clarity 必须在 0..100")

    header_issue = None
    raw_header = row.get("header_issue")
    if raw_header is not None and str(raw_header).strip():
        try:
            header_issue = normalize_issue(str(raw_header))
        except ValueError as exc:
            raise ScanRejected("BAD_FORMAT", "header_issue 格式不正确") from exc

    fields = {
        "issue": issue,
        "edition": edition,
        "page": page,
        "file_name": file_name,
        "sha256": digest,
        "phash": phash,
        "clarity": clarity,
        "header_issue": header_issue,
    }
    return {
        "line_no": int(row.get("line_no") or 0),
        "business_key": make_business_key({"issue": issue, "edition": edition, "page": page}),
        "fields": {key: value for key, value in fields.items() if value is not None},
        "sha256": digest,
        "file_name": file_name,
        "raw": _canon({key: value for key, value in row.items() if key != "line_no"}),
    }


def quarantine_key(station_id: str, entry: dict) -> str:
    return f"{station_id}|q|{sha256(_canon(entry).encode('utf-8')).hexdigest()[:24]}"


def prepare_manifest(manifest) -> dict:
    """解析、规范化并给出可直接入库的扫描件和隔离证据。"""

    parsed = parse_manifest_input(manifest)
    scans, quarantine = [], []
    for row in parsed["accepted"]:
        try:
            scans.append(normalize_scan(row))
        except ScanRejected as exc:
            quarantine.append(
                {
                    "line_no": row.get("line_no", 0),
                    "reason_code": exc.code,
                    "raw_value": _canon({key: value for key, value in row.items() if key != "line_no"}),
                    "message": exc.message,
                }
            )
    for entry in parsed["quarantine"]:
        entry = dict(entry)
        entry.setdefault("raw_value", entry.get("raw", ""))
        entry.pop("raw", None)
        quarantine.append(entry)

    return {
        "input": parsed["input"],
        "accepted": scans,
        "quarantine": quarantine,
        "tail_complete": parsed["tail_complete"],
    }
