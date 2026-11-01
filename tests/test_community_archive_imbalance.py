# SPDX-License-Identifier: AGPL-3.0-or-later
"""Archive release evidence by identity, including the draft's frozen reference."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import community_archive
from tests.test_community_archive import MemoryArchiveStore, representative_data
from tests.test_community_archive_evidence_postgres import _rehash
from tests.test_e66_evidence_postgres import synthetic_e66

V1_TABLES = frozenset(
    [
        "buildings",
        "communities",
        "community_members",
        "consents",
        "data_consents",
        "meter_readings",
        "community_documents",
        "leg_documents",
        "correspondence_log",
        "metering_points",
        "sdat_imports",
        "metering_point_readings",
        "sdat_veracity_flags",
        "billing_tariffs",
        "billing_periods",
        "billing_line_items",
        "invoices",
        "invoice_lifecycle_events",
        "invoice_delivery_jobs",
        "invoice_corrections",
    ]
)
V2_TABLES = V1_TABLES | frozenset(
    [
        "vnb_calculated_values_deliveries",
        "vnb_calculated_values_events",
        "vnb_submission_cases",
        "vnb_mutation_cases",
        "vnb_mutation_events",
        "bank_statement_imports",
        "bank_statement_entries",
        "invoice_queries",
        "invoice_query_messages",
        "invoice_query_events",
        "community_role_events",
        "community_batteries",
        "community_battery_shares",
        "operator_events",
        "operator_action_idempotency",
    ]
)
V3_TABLES = V2_TABLES | {"billing_imbalance_releases", "billing_imbalance_blocks"}


def release_data():
    data = representative_data()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    released = datetime(2026, 9, 2, tzinfo=UTC)
    data["billing_imbalance_releases"] = [
        {
            "id": 31,
            "community_id": "leg-1",
            "period_start": start,
            "period_end": end,
            "max_imbalance_ratio": Decimal("0.003000"),
            "reason": "Synthetic VNB ticket 4711",
            "released_by": "house-1",
            "released_at": released,
        }
    ]
    data["billing_imbalance_blocks"] = [
        {
            "community_id": "leg-1",
            "period_start": start,
            "period_end": end,
            "community_consumption_kwh": Decimal("0.999000"),
            "community_production_kwh": Decimal("1.000000"),
            "imbalance_kwh": Decimal("0.001000"),
            "imbalance_ratio": Decimal("0.001002"),
            "tolerance_ratio": Decimal("0.001000"),
            "recorded_at": released - timedelta(hours=1),
        }
    ]
    data["billing_periods"][0].update(
        period_start=start,
        period_end=end,
        reconciliation={
            "vnb_imbalance_tolerance_ratio": 0.003,
            "vnb_imbalance_release": {
                "id": 31,
                "max_imbalance_ratio": "0.003000",
                "reason": "Synthetic VNB ticket 4711",
                "released_by": "house-1",
                "released_at": released.isoformat(),
            },
        },
    )
    return data


def archive_payload(data):
    """Build a valid v3 wire payload without relying on the broken exporter."""
    payload = json.loads(
        community_archive.export_community_archive(
            "leg-1", store=MemoryArchiveStore({"leg-1": representative_data()})
        )
    )
    payload["manifest"]["schema_version"] = "openleg-community-archive/3"
    for table in V3_TABLES:
        payload["datasets"][table] = community_archive._json_value(data.get(table, []))
        _rehash(payload, table)
    return payload


def test_v3_archive_roundtrip_preserves_release_and_block_evidence():
    data = release_data()
    archive = community_archive.export_community_archive(
        "leg-1", store=MemoryArchiveStore({"leg-1": data})
    )
    payload = json.loads(archive)
    assert payload["manifest"]["schema_version"] == "openleg-community-archive/3"
    assert set(payload["datasets"]) == V3_TABLES
    target = MemoryArchiveStore()
    assert community_archive.restore_community_archive(archive, store=target)["valid"]
    for table in (
        "billing_imbalance_releases",
        "billing_imbalance_blocks",
        "billing_periods",
    ):
        assert target.restored[table] == data[table]


def test_release_actor_may_have_left_the_community():
    data = release_data()
    data["billing_imbalance_releases"][0]["released_by"] = "former-member"
    data["billing_periods"][0]["reconciliation"]["vnb_imbalance_release"][
        "released_by"
    ] = "former-member"
    archive = community_archive.export_community_archive(
        "leg-1", store=MemoryArchiveStore({"leg-1": data})
    )
    assert community_archive.restore_community_archive(
        archive, store=MemoryArchiveStore()
    )["valid"]


@pytest.mark.parametrize("identifier", [[], {}, True, "31", 0, -1])
def test_malformed_durable_release_identifiers_return_archive_errors(identifier):
    data = release_data()
    data["billing_imbalance_releases"][0]["id"] = identifier
    with pytest.raises(community_archive.ArchiveError):
        community_archive.export_community_archive(
            "leg-1", store=MemoryArchiveStore({"leg-1": data})
        )
    target = MemoryArchiveStore()
    result = community_archive.restore_community_archive(
        json.dumps(archive_payload(data)), store=target
    )
    assert result["valid"] is False
    assert result["errors"]
    assert target.restore_calls == 0


def test_duplicate_durable_release_identifiers_are_refused():
    data = release_data()
    data["billing_imbalance_releases"].append(
        deepcopy(data["billing_imbalance_releases"][0])
    )
    with pytest.raises(community_archive.ArchiveError):
        community_archive.export_community_archive(
            "leg-1", store=MemoryArchiveStore({"leg-1": data})
        )


@pytest.mark.parametrize(
    "table", ["billing_imbalance_releases", "billing_imbalance_blocks"]
)
def test_foreign_audit_rows_are_refused_even_without_a_draft_reference(table):
    data = release_data()
    data["billing_periods"] = []
    data[table][0]["community_id"] = "foreign-leg"
    with pytest.raises(community_archive.ArchiveError):
        community_archive.export_community_archive(
            "leg-1", store=MemoryArchiveStore({"leg-1": data})
        )


@pytest.mark.parametrize(
    "table", ["billing_imbalance_releases", "billing_imbalance_blocks"]
)
def test_malformed_imbalance_dataset_is_refused_on_export(table):
    data = release_data()
    data[table] = None
    with pytest.raises(community_archive.ArchiveError):
        community_archive.export_community_archive(
            "leg-1", store=MemoryArchiveStore({"leg-1": data})
        )


@pytest.mark.parametrize("version,tables", [(1, V1_TABLES), (2, V2_TABLES)])
def test_previous_archive_versions_keep_their_exact_dataset_contract(version, tables):
    payload = archive_payload(representative_data())
    payload["manifest"]["schema_version"] = f"openleg-community-archive/{version}"
    payload["datasets"] = {
        key: value for key, value in payload["datasets"].items() if key in tables
    }
    payload["manifest"]["hashes"] = {
        key: value
        for key, value in payload["manifest"]["hashes"].items()
        if key in tables
    }
    target = MemoryArchiveStore()
    result = community_archive.restore_community_archive(
        json.dumps(payload), store=target
    )
    assert result["valid"], result
    assert set(target.restored) == tables
    for added in V3_TABLES - tables:
        invalid = deepcopy(payload)
        invalid["datasets"][added] = []
        _rehash(invalid, added)
        rejected = community_archive.restore_community_archive(
            json.dumps(invalid), store=MemoryArchiveStore()
        )
        assert rejected["valid"] is False
        assert "Archive dataset list does not match schema" in rejected["errors"]


FAULTS = [
    "foreign_release",
    "foreign_block",
    "missing_release",
    "foreign_id",
    "malformed_id",
    "malformed_snapshot",
    "window",
    "actor",
    "reason",
    "ratio",
    "time",
]


def corrupt_release(data, fault):
    release = data["billing_imbalance_releases"][0]
    snapshot = data["billing_periods"][0]["reconciliation"]["vnb_imbalance_release"]
    if fault == "foreign_release":
        release["community_id"] = "foreign-leg"
    elif fault == "foreign_block":
        data["billing_imbalance_blocks"][0]["community_id"] = "foreign-leg"
    elif fault == "missing_release":
        data["billing_imbalance_releases"] = []
    elif fault == "foreign_id":
        snapshot["id"] = 999
    elif fault == "malformed_id":
        snapshot["id"] = []
    elif fault == "malformed_snapshot":
        data["billing_periods"][0]["reconciliation"]["vnb_imbalance_release"] = "broken"
    elif fault == "window":
        release["period_end"] += timedelta(days=1)
    elif fault == "actor":
        snapshot["released_by"] = "forged-actor"
    elif fault == "reason":
        snapshot["reason"] = "forged reason"
    elif fault == "ratio":
        release["max_imbalance_ratio"] = Decimal("0.004000")
    else:
        snapshot["released_at"] = "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize("fault", FAULTS)
def test_export_refuses_foreign_or_malformed_release_evidence(fault):
    data = release_data()
    corrupt_release(data, fault)
    with pytest.raises(community_archive.ArchiveError):
        community_archive.export_community_archive(
            "leg-1", store=MemoryArchiveStore({"leg-1": data})
        )


@pytest.mark.parametrize("fault", FAULTS)
def test_restore_refuses_foreign_or_malformed_release_evidence_before_writes(fault):
    # The valid counterpart must be accepted, so an unsupported version or a
    # dataset-list error cannot masquerade as relationship validation.
    good = archive_payload(release_data())
    baseline = community_archive.restore_community_archive(
        json.dumps(good), dry_run=True, store=MemoryArchiveStore()
    )
    assert baseline == {"valid": True, "errors": [], "conflicts": []}
    data = release_data()
    corrupt_release(data, fault)
    target = MemoryArchiveStore()
    result = community_archive.restore_community_archive(
        json.dumps(archive_payload(data)), store=target
    )
    assert result["valid"] is False
    assert result["errors"]
    assert target.restore_calls == 0


@pytest.mark.parametrize("version", [2, 3])
def test_e66_ledger_scope_remains_validated_for_v2_and_v3(version):
    import e66_evidence

    data = representative_data()
    source = synthetic_e66()
    document = e66_evidence.parse_source(source)
    digest = hashlib.sha256(source).hexdigest()
    data["communities"][0]["admin_building_id"] = "house-1"
    data["buildings"] = [{"building_id": "house-1", "city_id": "dietikon"}]
    data["vnb_calculated_values_deliveries"] = [
        {
            "id": 20,
            "community_id": "leg-1",
            "territory": "dietikon",
            "status": "accepted",
            "format_version": "e66/1",
            "vnb_case_id": document["document_id"],
            "evidence_bytes": source,
            "evidence_sha256": digest,
            "period_start": document["period_start"],
            "period_end": document["period_end"],
            "source_metadata": {
                "vnb_community_id": document["vnb_community_id"],
                "evidence_sha256": digest,
            },
        }
    ]
    data["sdat_imports"] = [
        {
            "document_id": document["document_id"],
            "doc_type": "E66",
            **{
                key: document[key]
                for key in (
                    "vnb_community_id",
                    "period_start",
                    "period_end",
                    "document_created_at",
                )
            },
        }
    ]
    # No raw reading links this ledger row. The accepted E66 document owns it.
    payload = archive_payload(data)
    payload["manifest"]["schema_version"] = f"openleg-community-archive/{version}"
    names = V2_TABLES if version == 2 else V3_TABLES
    payload["datasets"] = {
        key: value for key, value in payload["datasets"].items() if key in names
    }
    payload["manifest"]["hashes"] = {
        key: value
        for key, value in payload["manifest"]["hashes"].items()
        if key in names
    }
    good = community_archive.restore_community_archive(
        json.dumps(payload), dry_run=True, store=MemoryArchiveStore()
    )
    assert good == {"valid": True, "errors": [], "conflicts": []}
    for field, value in (
        ("document_id", "foreign-document"),
        ("vnb_community_id", "foreign-leg"),
    ):
        invalid = deepcopy(payload)
        invalid["datasets"]["sdat_imports"][0][field] = value
        _rehash(invalid, "sdat_imports")
        target = MemoryArchiveStore()
        result = community_archive.restore_community_archive(
            json.dumps(invalid), store=target
        )
        assert result["valid"] is False
        assert any(
            "scope" in error.lower() or "identity" in error.lower()
            for error in result["errors"]
        )
        assert target.restore_calls == 0
