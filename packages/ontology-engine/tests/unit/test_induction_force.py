# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""``force`` on StartInduction skips the structural-duplicate short-circuit.

The fingerprint covers table names, column types and FKs only, so a steward
edit to source metadata never changes it; without ``force`` an accepted
proposal can never be superseded unless the schema changes. These tests pin
both behaviours: default still dedups, ``force`` reaches the real induction
path even when an accepted proposal with the same fingerprint exists.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogTable


@pytest.fixture(autouse=True)
def _isolate_jobs():
    from coa_ontology import induce_catalog

    saved = dict(induce_catalog._jobs)
    induce_catalog._jobs.clear()
    yield
    induce_catalog._jobs.clear()
    induce_catalog._jobs.update(saved)


def _schemas_mod():
    from coa_ontology.inducer import schemas

    return schemas


def _catalog_patches():
    return (
        patch(
            "coa_ontology.induce_catalog._fetch_catalog_from_smus",
            return_value={"databases": [{"name": "db"}], "sourceType": "POSTGRES"},
        ),
        patch(
            "coa_ontology.induce_catalog._catalog_to_tables",
            return_value=[
                {
                    "id": "t-customers",
                    "name": "customers",
                    "fullyQualifiedName": "db.customers",
                    "columns": [{"name": "id", "dataType": "INTEGER"}],
                }
            ],
        ),
    )


def _existing_accepted():
    from coa_ontology.induce_catalog import _compute_structural_fingerprint

    tables = [
        CatalogTable(
            id="t",
            name="customers",
            fullyQualifiedName="db.customers",
            columns=[CatalogColumn(name="id", dataType="INTEGER")],
        )
    ]
    return {
        "proposal_id": "accepted-proposal-123",
        "ontology_id": "http://test.org/ontology/induced#",
        "metadata": {"structural_fingerprint": _compute_structural_fingerprint(tables)},
    }


def _run(job_id: str, body, mock_dynamo, build_pipeline_fn):
    from coa_ontology.induce_catalog import _run_induction

    _run_induction(
        job_id=job_id,
        body=body,
        namespace="test-ns",
        config={"catalog_source": "smus", "llm_region": "us-west-2"},
        build_pipeline_fn=build_pipeline_fn,
        schemas_mod=_schemas_mod(),
        catalog_table_cls=CatalogTable,
    )


class TestForceField:
    def test_defaults_false_and_accepts_both_wire_names(self):
        from coa_ontology.induce_catalog import WorkbenchInductionRequest

        base = {"datasource_ids": ["ds-1"], "ontology_uri_prefix": "http://test.org/o#"}
        assert WorkbenchInductionRequest(**base).force is False
        assert WorkbenchInductionRequest(**base, force=True).force is True
        assert WorkbenchInductionRequest.model_validate({**base, "forceReinduction": True}).force is True


class TestDuplicateShortCircuit:
    def test_default_still_dedups(self):
        from coa_ontology.induce_catalog import WorkbenchInductionRequest, _jobs
        from coa_ontology.inducer.schemas import JobResponse, JobStatus

        job = JobResponse(job_id="job-default", status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z")
        _jobs["job-default"] = job
        body = WorkbenchInductionRequest(
            datasource_ids=["ds-1"], ontology_uri_prefix="http://test.org/ontology/induced#"
        )
        pipeline = MagicMock()
        p1, p2 = _catalog_patches()
        with (
            p1,
            p2,
            patch("coa_ontology.induce_catalog.dynamo_store") as mock_dynamo,
            patch("coa_ontology.induce_catalog.emit_induction_job_metrics"),
        ):
            mock_dynamo.list_proposals.return_value = [_existing_accepted()]
            _run("job-default", body, mock_dynamo, pipeline)

        assert job.status == JobStatus.COMPLETED
        assert job.duplicate_of == "accepted-proposal-123"
        mock_dynamo.list_proposals.assert_called_once_with(namespace="test-ns", status="accepted")
        pipeline.assert_not_called()  # no induction ran

    def test_force_skips_dedup_and_runs_induction(self):
        from coa_ontology.induce_catalog import WorkbenchInductionRequest, _jobs
        from coa_ontology.inducer.schemas import JobResponse, JobStatus

        job = JobResponse(job_id="job-force", status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z")
        _jobs["job-force"] = job
        body = WorkbenchInductionRequest(
            datasource_ids=["ds-1"], ontology_uri_prefix="http://test.org/ontology/induced#", force=True
        )
        # The pipeline factory is the first thing the real path touches after the
        # dedup block; make it raise so the test proves we got PAST the check
        # without standing up the whole induction pipeline.
        pipeline = MagicMock(side_effect=RuntimeError("reached real induction"))
        p1, p2 = _catalog_patches()
        with (
            p1,
            p2,
            patch("coa_ontology.induce_catalog.dynamo_store") as mock_dynamo,
            patch("coa_ontology.induce_catalog.emit_induction_job_metrics"),
        ):
            mock_dynamo.list_proposals.return_value = [_existing_accepted()]
            _run("job-force", body, mock_dynamo, pipeline)

        # Not short-circuited: the accepted-proposal lookup was skipped entirely
        # and the run proceeded into the induction path (where our sentinel fired).
        assert job.duplicate_of is None
        for call in mock_dynamo.list_proposals.call_args_list:
            assert call.kwargs.get("status") != "accepted"
        pipeline.assert_called()
        assert job.status == JobStatus.FAILED
        assert "reached real induction" in (job.error or "")
