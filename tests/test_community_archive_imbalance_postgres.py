# SPDX-License-Identifier: AGPL-3.0-or-later
"""Restore the durable release required to approve a released E66 draft."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

import billing_approval
import billing_runner
import community_archive
from tests import test_billing_imbalance_precision_postgres as imbalance
from tests import test_community_archive_evidence_postgres as archives
from tests.test_community_archive_imbalance import FAULTS, V3_TABLES, corrupt_release

interest_database = imbalance.interest_database
calculated_client = imbalance.calculated_client
assignment_request = imbalance.assignment_request
configured_e66 = imbalance.configured_e66
rounded_delivery = imbalance.rounded_delivery
unbalanced_period = imbalance.unbalanced_period
pytestmark = pytest.mark.integration


@pytest.fixture
def released_archive(unbalanced_period):
    db, cid, start, end = unbalanced_period
    with pytest.raises(billing_runner.BillingRunError):
        billing_runner.run_billing_period(cid, start, end)
    # IDs must exceed a fresh destination sequence, otherwise a forgotten
    # sequence advance might pass by allocating the next free low ID.
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT setval('billing_imbalance_releases_id_seq', 40, true)")
    db.save_billing_imbalance_release(
        cid, start, end, Decimal("0.001"), "Synthetic first attempt", "member"
    )
    release = db.save_billing_imbalance_release(
        cid, start, end, Decimal("0.002"), "Synthetic VNB ticket 4711", "member"
    )
    prepared = billing_runner.run_billing_period(
        cid, start, end, prepared_by="producer"
    )
    # Keep another blocked window visible as well as the stored figures for
    # the released draft, which list_billing_imbalance_blocks intentionally hides.
    figures = {
        "community_consumption_kwh": Decimal("0.999000"),
        "community_production_kwh": Decimal("1.000000"),
        "imbalance_kwh": Decimal("0.001000"),
        "imbalance_ratio": Decimal("0.001002"),
        "tolerance_ratio": Decimal("0.001000"),
    }
    db.record_billing_imbalance_block(
        cid, start + timedelta(days=1), end + timedelta(days=1), figures
    )
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO communities (community_id, name, status) VALUES ('foreign-archive-leg', 'Synthetic foreign LEG', 'active')"
        )
    db.save_billing_imbalance_release(
        "foreign-archive-leg",
        start,
        end,
        Decimal("0.002"),
        "PRIVATE-FOREIGN-RELEASE",
        "foreign-actor",
    )
    db.record_billing_imbalance_block("foreign-archive-leg", start, end, figures)
    expected = {}
    with db.get_connection() as conn, conn.cursor() as cur:
        for table in ("billing_imbalance_releases", "billing_imbalance_blocks"):
            cur.execute(
                f"SELECT * FROM {table} WHERE community_id = %s ORDER BY period_start, period_end",
                (cid,),
            )
            expected[table] = sorted(
                [dict(row) for row in cur.fetchall()], key=lambda row: row.get("id", 0)
            )
    return db, cid, start, end, prepared["period_id"], release, expected


def test_v3_pg_roundtrip_preserves_own_releases_blocks_approval_and_sequence(
    released_archive, monkeypatch
):
    db, cid, start, end, period_id, release, expected = released_archive
    archive = community_archive.export_community_archive(cid)
    payload = json.loads(archive)
    assert payload["manifest"]["schema_version"] == "openleg-community-archive/3"
    assert set(payload["datasets"]) == V3_TABLES
    assert b"PRIVATE-FOREIGN" not in archive
    for table in expected:
        assert {row["community_id"] for row in payload["datasets"][table]} == {cid}
    with archives.clean_destination(db, monkeypatch):
        assert community_archive.restore_community_archive(archive, dry_run=True) == {
            "valid": True,
            "errors": [],
            "conflicts": [],
        }
        result = community_archive.restore_community_archive(archive)
        assert result["valid"], result
        assert community_archive.restore_community_archive(archive)["restored"] is True
        with db.get_connection() as conn, conn.cursor() as cur:
            for table, rows in expected.items():
                cur.execute(f"SELECT * FROM {table} ORDER BY period_start, period_end")
                assert (
                    sorted(
                        [dict(row) for row in cur.fetchall()],
                        key=lambda row: row.get("id", 0),
                    )
                    == rows
                )
        # Actual approval reads the frozen release ID and all audit fields from
        # PostgreSQL; losing it during archive restore must fail this operation.
        assert db.approve_billing_period(period_id, cid, approver_id="consumer-b")
        next_release = db.save_billing_imbalance_release(
            cid, start, end, Decimal("0.003"), "Synthetic next release", "member"
        )
        assert next_release["id"] > release["id"]


@pytest.mark.parametrize("fault", FAULTS)
def test_v3_pg_restore_refuses_release_scope_or_frozen_reference_corruption(
    released_archive, monkeypatch, fault
):
    db, cid, _, _, _, _, _ = released_archive
    archive = json.loads(community_archive.export_community_archive(cid))
    assert archive["manifest"]["schema_version"] == "openleg-community-archive/3"
    # The fixture retains a historical release. Corrupt the exact row frozen in
    # the draft, not whichever row the exporter happens to return first.
    decoded = community_archive.PostgresArchiveStore().decode_records(
        archive["datasets"]
    )
    frozen_id = decoded["billing_periods"][0]["reconciliation"][
        "vnb_imbalance_release"
    ]["id"]
    decoded["billing_imbalance_releases"].sort(key=lambda row: row["id"] != frozen_id)
    corrupt_release(decoded, fault)
    for table in (
        "billing_imbalance_releases",
        "billing_imbalance_blocks",
        "billing_periods",
    ):
        archive["datasets"][table] = community_archive._json_value(decoded[table])
        archives._rehash(archive, table)
    with archives.clean_destination(db, monkeypatch):
        result = community_archive.restore_community_archive(json.dumps(archive))
        assert result["valid"] is False
        assert result["errors"]
        with db.get_connection() as conn, conn.cursor() as cur:
            for table in (
                "communities",
                "billing_periods",
                "billing_imbalance_releases",
                "billing_imbalance_blocks",
            ):
                cur.execute(f"SELECT count(*) AS n FROM {table}")
                assert cur.fetchone()["n"] == 0


def test_pg_export_refuses_a_draft_referencing_a_foreign_release(released_archive):
    db, cid, start, end, period_id, _, _ = released_archive
    foreign = db.get_billing_imbalance_release("foreign-archive-leg", start, end)
    period = db.get_billing_period(period_id, cid)
    period["reconciliation"]["vnb_imbalance_release"]["id"] = foreign["id"]
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE billing_periods SET reconciliation = %s::jsonb WHERE id = %s",
            (json.dumps(period["reconciliation"]), period_id),
        )
    with pytest.raises(billing_approval.BillingApprovalError):
        db.approve_billing_period(period_id, cid, approver_id="consumer-b")
    with pytest.raises(community_archive.ArchiveError):
        community_archive.export_community_archive(cid)
