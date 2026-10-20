# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the translateSql serve action.

Covers three layers:
- ``NLtoSQLStrategy.compile`` — the exact pre-execution prefix of ``resolve``:
  SQL/trace parity up to execution, None/raise parity, and the translate-only
  skip step.
- ``native_transpile.to_native`` — Databricks single-source rewrite (asserted via
  sqlglot AST equality), cross-source refusal, non-transpilable engine, and the
  already-native direct-JDBC case.
- ``_handle_translate_sql`` — handler wiring: executionTarget derivation and the
  native-transpile branch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlglot
from coa_serve.exceptions import AccessDeniedError, AmbiguousReferenceError, QueryTranslationError
from coa_serve.tier2.nl_to_sql.native_transpile import to_native
from coa_serve.tier2.nl_to_sql.strategy import CompiledSQL, NLtoSQLStrategy

# Reuse the strategy test fakes so compile() is exercised against the SAME mocks
# resolve() is, which is the whole point of the prefix-parity assertions.
from tests.unit.test_nl_to_sql_strategy import (
    _make_context,
    _make_exec_result,
    _make_firewall_result,
    _make_nl_to_sql_result,
)

pytestmark = pytest.mark.unit


# ── Single-source fixture (contract) ─────────────────────────────────────────

_DEMO_ATHENA_SQL = (
    "SELECT COUNT(DISTINCT p.order_id) "
    'FROM "demodatasource_1a2b3c4d5e6f7a8b"."sales".orders p '
    "WHERE p.status = 'Active' AND p.region = 'NA'"
)
_DEMO_EXPECTED_NATIVE = (
    "SELECT COUNT(DISTINCT p.order_id) "
    "FROM demo_catalog.sales.orders AS p "
    "WHERE p.status = 'Active' AND p.region = 'NA'"
)
_DEMO_SOURCE = {
    "sourceId": "ds-databricks",
    "sourceSubType": "DATABRICKS_SQL_WAREHOUSE",
    "athenaDataCatalogName": "demodatasource_1a2b3c4d5e6f7a8b",
    "discoveredSchemas": ["sales"],
    "configuration": {"databricksCatalog": "demo_catalog", "databaseName": "sales"},
}


# ── compile() — prefix parity with resolve() ─────────────────────────────────


@pytest.mark.unit
class TestCompilePrefixParity:
    @pytest.fixture
    def sql_generator(self):
        return AsyncMock()

    @pytest.fixture
    def firewall(self):
        return MagicMock()

    @pytest.fixture
    def query_executor(self):
        return AsyncMock()

    @pytest.fixture
    def strategy(self, sql_generator, firewall, query_executor):
        return NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            oss_ontology_index="test-index",
        )

    @pytest.mark.asyncio
    async def test_compile_returns_same_sql_and_trace_as_resolve_up_to_execution(
        self, strategy, sql_generator, firewall, query_executor
    ):
        """compile() emits the SAME SQL and the SAME generate/firewall trace steps
        resolve() does, then stops — the only difference is the execute step
        (skipped translate_only vs a real execution) and no executor call."""
        authorized = "SELECT count(*) FROM orders"
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql=authorized)
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=authorized)

        # resolve() baseline: a successful execution.
        query_executor.execute.return_value = _make_exec_result()
        resolve_ctx = _make_context()
        resolve_result = await strategy.resolve("how many orders?", "ns", resolve_ctx)

        compile_ctx = _make_context()
        compiled = await strategy.compile("how many orders?", "ns", compile_ctx)

        assert compiled is not None
        assert isinstance(compiled, CompiledSQL)
        # Same statement resolve() ran.
        assert compiled.sql == resolve_result.sql == authorized
        assert compiled.confidence == resolve_result.confidence
        assert compiled.dialect  # dialect resolved (mock executor -> AsyncMock value)

        def gen_steps(ctx):
            return [(s.step, s.status) for s in ctx.trace.steps if s.step.startswith("t2.sql.generate")]

        # Identical generate/firewall/authorize steps up to execution.
        assert gen_steps(compile_ctx) == gen_steps(resolve_ctx)
        # compile() records the execute step as a deliberate translate-only skip,
        # and never calls the executor a second time for the compile path.
        skip = [s for s in compile_ctx.trace.steps if s.step == "t2.sql.execute"]
        assert skip and skip[0].status == "skipped" and skip[0].detail == "translate_only"
        # The executor was used by resolve() (1 call) but NOT by compile().
        assert query_executor.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_compile_returns_none_on_generation_failure(self, strategy, sql_generator, firewall):
        """None parity: generation error → None, no firewall, like resolve()."""
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="", error="LLM timeout")

        compiled = await strategy.compile("q", "ns", _make_context())

        assert compiled is None
        firewall.evaluate.assert_not_called()

    @pytest.mark.asyncio
    async def test_compile_returns_none_on_unsafe_sql(self, strategy, sql_generator, firewall):
        """Unsafe SQL → None (abandon), exactly as resolve()'s first shot does."""
        from coa_serve.tier2.sql_firewall import UnsafeSQLError

        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.side_effect = UnsafeSQLError("Only SELECT statements are allowed")

        compiled = await strategy.compile("q", "ns", _make_context())

        assert compiled is None

    @pytest.mark.asyncio
    async def test_compile_raises_access_denied(self, strategy, sql_generator, firewall):
        """Raise parity: a firewall deny is terminal (403), as in resolve()."""
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result(denied=True, reason="not in allowlist")

        with pytest.raises(AccessDeniedError):
            await strategy.compile("q", "ns", _make_context())

    @pytest.mark.asyncio
    async def test_compile_does_not_require_executor(self, sql_generator, firewall):
        """compile() must work with no executor — it never executes."""
        strategy = NLtoSQLStrategy(sql_generator=sql_generator, firewall=firewall, query_executor=None)
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="SELECT 1 FROM orders")
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql="SELECT 1 FROM orders")

        compiled = await strategy.compile("q", "ns", _make_context())

        assert compiled is not None
        assert compiled.sql == "SELECT 1 FROM orders"

    @pytest.mark.asyncio
    async def test_compile_cross_source_sql_is_qualified(self):
        """Cross-source preparation runs on compile() too (post-authorization)."""
        sql_generator, firewall = AsyncMock(), MagicMock()
        sql = "SELECT a.id FROM claims a JOIN policies b ON a.id = b.claim_id"
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql=sql,
            table_sources={"claims": "ds-pg", "policies": "ds-glue"},
            data_source_id="",
        )
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=sql)
        registry = AsyncMock()
        registry.get_source.side_effect = lambda namespace, data_source_id: {
            "ds-pg": {"athenaDataCatalogName": "pg_cat", "discoveredSchemas": ["public"]},
            "ds-glue": {"glueDatabaseName": "insurance"},
        }.get(data_source_id, {})
        strategy = NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=None,
            sources_registry=registry,
        )

        compiled = await strategy.compile("cross-source q", "ns", _make_context())

        assert compiled is not None
        qualified = compiled.sql.replace('"', "")
        assert "pg_cat.public.claims" in qualified
        assert "awsdatacatalog.insurance.policies" in qualified

    @pytest.mark.asyncio
    async def test_compile_ambiguous_reference_raises(self):
        """An ambiguous reference refuses the query (400), never returns bare SQL."""
        sql_generator, firewall = AsyncMock(), MagicMock()
        sql = "SELECT a.id FROM customers a JOIN policies b ON a.id = b.id"
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql=sql,
            table_sources={"customers": "__ambiguous__", "policies": "ds-glue"},
            data_source_id="",
        )
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=sql)
        registry = AsyncMock()
        registry.get_source.side_effect = lambda namespace, data_source_id: {"glueDatabaseName": "insurance"}
        strategy = NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=None,
            sources_registry=registry,
        )

        with pytest.raises(AmbiguousReferenceError):
            await strategy.compile("ambiguous q", "ns", _make_context())


# ── native_transpile.to_native ───────────────────────────────────────────────


@pytest.mark.unit
class TestToNative:
    def test_databricks_single_source_rewrites_to_native_catalog_and_dialect(self):
        result = to_native(
            _DEMO_ATHENA_SQL,
            dialect="athena",
            routing={"orders": {"datasourceId": "ds-databricks"}},
            sources={"ds-databricks": _DEMO_SOURCE},
        )

        assert result.applied is True
        assert result.dialect == "databricks"
        assert result.catalog == "demo_catalog"
        assert result.reason is None
        # Exact native output per the contract — asserted via AST equality, not
        # string equality (sqlglot's whitespace/AS rendering is its own).
        assert sqlglot.parse_one(result.sql, read="databricks") == sqlglot.parse_one(
            _DEMO_EXPECTED_NATIVE, read="databricks"
        )

    def test_cross_source_is_refused(self):
        result = to_native(
            "SELECT * FROM a JOIN b ON a.id = b.id",
            dialect="athena",
            routing={"a": {"datasourceId": "ds-1"}, "b": {"datasourceId": "ds-2"}},
            sources={},
        )

        assert result.applied is False
        assert result.reason == "cross_source"
        assert result.sql == "SELECT * FROM a JOIN b ON a.id = b.id"  # untouched

    def test_non_transpilable_engine_fails_closed(self):
        # A CUSTOM_CONNECTOR has no known native dialect -> engine_not_transpilable.
        source = {**_DEMO_SOURCE, "sourceSubType": "CUSTOM_CONNECTOR"}
        result = to_native(
            _DEMO_ATHENA_SQL,
            dialect="athena",
            routing={"orders": {"datasourceId": "ds-databricks"}},
            sources={"ds-databricks": source},
        )

        assert result.applied is False
        assert result.reason == "engine_not_transpilable"

    def test_no_native_catalog_fails_closed(self):
        source = {**_DEMO_SOURCE, "configuration": {"databaseName": "sales"}}  # no databricksCatalog
        result = to_native(
            _DEMO_ATHENA_SQL,
            dialect="athena",
            routing={"orders": {"datasourceId": "ds-databricks"}},
            sources={"ds-databricks": source},
        )

        assert result.applied is False
        assert result.reason == "no_native_catalog"

    def test_direct_jdbc_dialect_is_already_native(self):
        result = to_native(
            "SELECT 1 FROM t",
            dialect="postgres",
            routing={"t": {"datasourceId": "ds-pg"}},
            sources={"ds-pg": {}},
        )

        assert result.applied is False
        assert result.reason is None
        assert result.is_direct_jdbc is True


# ── _handle_translate_sql — handler wiring ───────────────────────────────────


@pytest.mark.unit
class TestHandleTranslateSql:
    def _compiled(self, sql="SELECT count(*) FROM orders", dialect="athena", table_sources=None, ds="ds-1"):
        return CompiledSQL(
            sql=sql,
            dialect=dialect,
            confidence=0.9,
            data_source_id=ds,
            table_sources=table_sources or {"orders": ds},
            retrieved_tables=["orders"],
            expanded_tables=["orders"],
            trace_steps=[],
        )

    @pytest.mark.asyncio
    async def test_invalid_query_is_400(self):
        from coa_serve.main import _handle_translate_sql

        with patch("coa_serve.main._nl_to_sql_strategy", AsyncMock()):
            result = await _handle_translate_sql({"query": 42, "namespace": "ns"}, "req-1", None)

        assert result["statusCode"] == 400

    @pytest.mark.asyncio
    async def test_missing_namespace_is_400(self):
        from coa_serve.main import _handle_translate_sql

        with patch("coa_serve.main._nl_to_sql_strategy", AsyncMock()):
            result = await _handle_translate_sql({"query": "q", "namespace": ""}, "req-1", None)

        assert result["statusCode"] == 400

    @pytest.mark.asyncio
    async def test_none_compile_raises_translation_error(self):
        from coa_serve.main import _handle_translate_sql

        strategy = AsyncMock()
        strategy.compile.return_value = None
        reg = AsyncMock()
        reg.resolve_workgroup.return_value = "wg"
        with (
            patch("coa_serve.main._nl_to_sql_strategy", strategy),
            patch("coa_serve.main._orchestrator", None),
            patch("coa_serve.main._sources_registry", reg),
            pytest.raises(QueryTranslationError),
        ):
            await _handle_translate_sql({"query": "q", "namespace": "ns"}, "req-1", None)

    @pytest.mark.asyncio
    async def test_athena_execution_target(self):
        from coa_serve.main import _handle_translate_sql

        strategy = AsyncMock()
        strategy.compile.return_value = self._compiled()
        reg = AsyncMock()
        reg.resolve_workgroup.return_value = "ns-workgroup"
        with (
            patch("coa_serve.main._nl_to_sql_strategy", strategy),
            patch("coa_serve.main._orchestrator", None),  # no embedding path
            patch("coa_serve.main._sources_registry", reg),
        ):
            result = await _handle_translate_sql({"query": "q", "namespace": "ns"}, "req-1", None)

        assert result["statusCode"] == 200
        target = result["executionTarget"]
        assert target["kind"] == "ATHENA"
        assert target["engine"] == "ATHENA"
        assert target["workgroup"] == "ns-workgroup"
        assert target["sourceIds"] == ["ds-1"]
        assert result["dialect"] == "athena"
        assert result["ontologyVersion"] is None
        assert result["firewall"]["allowed"] is True
        assert "nativeTranspile" not in result  # only present for target=native

    @pytest.mark.asyncio
    async def test_direct_jdbc_execution_target(self):
        from coa_serve.main import _handle_translate_sql

        strategy = AsyncMock()
        strategy.compile.return_value = self._compiled(dialect="postgres")
        reg = AsyncMock()
        reg.get_source.return_value = {"configuration": '{"engine": "POSTGRESQL"}'}
        reg.parse_configuration = lambda item: {"engine": "POSTGRESQL"}
        with (
            patch("coa_serve.main._nl_to_sql_strategy", strategy),
            patch("coa_serve.main._orchestrator", None),
            patch("coa_serve.main._sources_registry", reg),
        ):
            result = await _handle_translate_sql({"query": "q", "namespace": "ns"}, "req-1", None)

        target = result["executionTarget"]
        assert target["kind"] == "DIRECT_JDBC"
        assert target["engine"] == "POSTGRESQL"
        assert target["workgroup"] is None

    @pytest.mark.asyncio
    async def test_native_target_applies_and_sets_native_execution_target(self):
        from coa_serve.main import _handle_translate_sql

        strategy = AsyncMock()
        strategy.compile.return_value = self._compiled(
            sql=_DEMO_ATHENA_SQL,
            dialect="athena",
            table_sources={"orders": "ds-databricks"},
            ds="ds-databricks",
        )
        reg = AsyncMock()
        reg.get_source.return_value = _DEMO_SOURCE
        reg.parse_configuration = lambda item: _DEMO_SOURCE["configuration"]
        reg.resolve_workgroup.return_value = "wg"
        with (
            patch("coa_serve.main._nl_to_sql_strategy", strategy),
            patch("coa_serve.main._orchestrator", None),
            patch("coa_serve.main._sources_registry", reg),
        ):
            result = await _handle_translate_sql(
                {"query": "q", "namespace": "ns", "options": {"target": "native"}}, "req-1", None
            )

        assert result["nativeTranspile"]["applied"] is True
        assert result["nativeTranspile"]["reason"] is None
        target = result["executionTarget"]
        assert target["kind"] == "NATIVE"
        assert target["engine"] == "DATABRICKS_SQL_WAREHOUSE"
        assert target["catalog"] == "demo_catalog"
        assert result["dialect"] == "databricks"
        assert sqlglot.parse_one(result["sql"], read="databricks") == sqlglot.parse_one(
            _DEMO_EXPECTED_NATIVE, read="databricks"
        )

    @pytest.mark.asyncio
    async def test_native_target_not_applied_keeps_athena_and_reports_reason(self):
        from coa_serve.main import _handle_translate_sql

        # Cross-source statement: native cannot apply; the Athena form is kept and
        # the reason is surfaced.
        strategy = AsyncMock()
        strategy.compile.return_value = self._compiled(
            sql="SELECT a.id FROM claims a JOIN policies b ON a.id = b.id",
            dialect="athena",
            table_sources={"claims": "ds-1", "policies": "ds-2"},
            ds="",
        )
        reg = AsyncMock()
        reg.get_source.return_value = {}
        reg.parse_configuration = lambda item: {}
        reg.resolve_workgroup.return_value = "wg"
        with (
            patch("coa_serve.main._nl_to_sql_strategy", strategy),
            patch("coa_serve.main._orchestrator", None),
            patch("coa_serve.main._sources_registry", reg),
        ):
            result = await _handle_translate_sql(
                {"query": "q", "namespace": "ns", "options": {"target": "native"}}, "req-1", None
            )

        assert result["nativeTranspile"]["applied"] is False
        assert result["nativeTranspile"]["reason"] == "cross_source"
        assert result["executionTarget"]["kind"] == "ATHENA"
        assert result["executionTarget"]["sourceIds"] == ["ds-1", "ds-2"]
