"""M7 离线同步的纯规则：三方合并、已核准保护与修订校验。"""

import pytest

from app.errors import DomainError
from app.modules.m7_sync.model import (
    MISSING,
    apply_changes,
    normalize_revision,
    payload_digest,
    protect_approved,
    three_way_merge,
)


def test_merge_takes_independent_changes_from_both_sides():
    base = {"page": "001", "clarity": 80, "header_issue": "2026-11-01"}
    ours = {"page": "001", "clarity": 95, "header_issue": "2026-11-01"}
    theirs = {"page": "002", "clarity": 80, "header_issue": "2026-11-01"}
    merged, conflicts = three_way_merge(base, ours, theirs)
    assert merged == {"page": "002", "clarity": 95, "header_issue": "2026-11-01"}
    assert not conflicts


def test_merge_holds_same_field_divergence():
    base = {"clarity": 80}
    merged, conflicts = three_way_merge(base, {"clarity": 95}, {"clarity": 60})
    assert conflicts["clarity"]["ours"] == 95
    assert conflicts["clarity"]["theirs"] == 60
    assert merged["clarity"] == 95  # 未裁定前保留中心当前值


def test_merge_agreement_is_not_conflict():
    base = {"clarity": 80}
    merged, conflicts = three_way_merge(base, {"clarity": 95}, {"clarity": 95})
    assert not conflicts and merged["clarity"] == 95


def test_ours_untouched_adopts_theirs():
    base = {"clarity": 80, "page": "001"}
    merged, conflicts = three_way_merge(base, {"clarity": 80, "page": "001"}, {"clarity": 90, "page": "001"})
    assert not conflicts and merged["clarity"] == 90


def test_deleted_field_is_not_resurrected():
    base = {"clarity": 80, "note": "x"}
    ours = {"clarity": 80}
    theirs = {"clarity": 80, "note": "x"}
    merged, conflicts = three_way_merge(base, ours, theirs)
    assert "note" not in merged and not conflicts


def test_approved_field_blocks_regression_to_old_base():
    # 站点未带底稿版本时，合并以中心当前值为底稿；旧值会被当成"改动"。
    base = {"issue": "2026-11-01", "edition": "01", "page": "005"}
    ours = {"issue": "2026-11-01", "edition": "01", "page": "005"}
    theirs = {"issue": "2026-11-01", "edition": "01", "page": "004"}
    merged, conflicts = three_way_merge(base, ours, theirs)
    assert not conflicts and merged["page"] == "004"

    merged, approved = protect_approved(
        merged,
        base,
        ours,
        theirs,
        approved_fields=["page"],
        approved_values={"page": "005"},
    )
    assert "page" in approved
    assert merged["page"] == "005"  # 未裁定不落旧值


def test_apply_changes_reports_from_mismatch_but_still_merges():
    base = {"clarity": 80}
    changes = {"clarity": {"from": 70, "to": 88}}
    result, mismatched = apply_changes(base, changes)
    assert result["clarity"] == 88 and mismatched == ["clarity"]


def test_apply_changes_deletes_via_null_to():
    base = {"clarity": 80, "note": "x"}
    result, _ = apply_changes(base, {"note": {"to": None}})
    assert "note" not in result


def test_revision_requires_exactly_one_of_fields_or_changes():
    with pytest.raises(DomainError, match="fields 与 changes"):
        normalize_revision({"revision_id": "r", "business_key": "k", "fields": {}, "changes": {}})
    with pytest.raises(DomainError):
        normalize_revision({"revision_id": "r", "business_key": "k"})


def test_revision_rejects_non_scalar_values():
    with pytest.raises(DomainError):
        normalize_revision(
            {"revision_id": "r", "business_key": "k", "fields": {"page": ["001"]}}
        )


def test_payload_digest_is_stable_and_sensitive():
    spec = normalize_revision(
        {"revision_id": "r", "business_key": "k", "fields": {"page": "001"}}
    )
    same = normalize_revision(
        {"revision_id": "r", "business_key": "k", "fields": {"page": "001"}}
    )
    other = normalize_revision(
        {"revision_id": "r", "business_key": "k", "fields": {"page": "002"}}
    )
    assert payload_digest(spec) == payload_digest(same)
    assert payload_digest(spec) != payload_digest(other)
