# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Rewrite COA's governed Athena SQL into a federated source's own dialect.

The ``translateSql`` serve action hands an agent a statement to run under its
OWN identity. COA produces that statement in Athena's Trino dialect, addressing a
federated source through its generated Athena DataCatalog
(``"<athenaDataCatalogName>"."<schema>".<table>``). An agent that wants to run the
query DIRECTLY on the source engine — a Databricks SQL warehouse, say — cannot use
the Athena form: the catalog name names nothing in Databricks, and Trino and
Databricks diverge on syntax sqlglot rewrites per dialect.

:func:`to_native` turns the Athena form into the source engine's own form for the
single-source federated case, and fails CLOSED (``applied=False`` with a reason)
for every case it cannot prove safe, returning the untouched Athena statement so
the caller can still hand back a runnable query:

- a statement spanning two or more datasources → ``cross_source`` (no single
  native catalog can name them all);
- a source whose sub-type has no native sqlglot dialect → ``engine_not_transpilable``;
- a source with no native catalog in its configuration → ``no_native_catalog``;
- a sqlglot parse/transpile failure → ``transpile_failed:<ExceptionName>``.

A direct-JDBC dialect (postgres/redshift/mysql/tsql) is ALREADY the source
engine's own dialect — COA generated it that way — so there is nothing to
transpile: ``applied=False``, ``reason=None``, and the caller reports the source
as directly addressable (``DIRECT_JDBC``).

Pure function, no I/O: the handler fetches the source records and passes them in.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
import sqlglot.errors
import sqlglot.expressions as exp
import structlog
from coa_common.constants import DatabaseSubType

from ..table_qualifier import distinct_datasources, real_tables

logger = structlog.get_logger(__name__)

# Source sub-type -> the sqlglot write dialect for its native engine. Only
# connector-backed sub-types with a KNOWN native dialect appear here; everything
# else (including the generic CUSTOM_CONNECTOR, which carries no single engine
# identity, and GLUE_DATABASE / JDBC_DATABASE, which are not federated-native
# targets) fails closed as engine_not_transpilable. The sub-type values come from
# ``coa_common.constants.DatabaseSubType`` so a rename there fails here rather
# than silently dropping a mapping.
#
# Snowflake has no dedicated DatabaseSubType member today — it is onboarded as a
# CUSTOM_CONNECTOR — so there is no sub-type to key a "snowflake" mapping on. When
# a distinct sub-type is introduced, add it here; until then a Snowflake source
# correctly falls through to engine_not_transpilable rather than guessing.
_SUB_TYPE_NATIVE_DIALECT: dict[str, str] = {
    DatabaseSubType.DATABRICKS_SQL_WAREHOUSE.value: "databricks",
}

# sqlglot dialects COA already generates natively for a direct-JDBC source. SQL in
# one of these needs no transpilation — it is the source engine's own dialect — so
# ``to_native`` reports it as directly addressable rather than rewriting it.
_DIRECT_JDBC_DIALECTS: frozenset[str] = frozenset({"postgres", "redshift", "mysql", "tsql"})


@dataclass(frozen=True)
class NativeTranspileResult:
    """Outcome of a native-transpile attempt.

    ``applied`` is True only when the statement was rewritten into the source
    engine's dialect AND catalog. On every other outcome ``sql`` is the ORIGINAL
    (untouched) statement so the caller can still return a runnable query, and
    ``reason`` names why the rewrite did not happen (``None`` for the direct-JDBC
    already-native case). ``dialect`` is the native sqlglot dialect when applied,
    else the input dialect. ``catalog`` is the source's native catalog when
    applied, else ``None``. ``is_direct_jdbc`` flags the already-native direct-JDBC
    case so the handler reports ``DIRECT_JDBC`` rather than ``ATHENA``.
    """

    applied: bool
    sql: str
    dialect: str
    reason: str | None = None
    catalog: str | None = None
    is_direct_jdbc: bool = False


def _native_catalog(source: dict, configuration: dict) -> str:
    """The source's native catalog for a 3-part ``catalog.schema.table`` name.

    Databricks records it as ``configuration.databricksCatalog``. Kept narrow on
    purpose: a sub-type whose native catalog lives elsewhere must add its own key
    here rather than silently reusing the Athena DataCatalog name, which names
    nothing in the native engine.
    """
    return str(configuration.get("databricksCatalog") or "")


def _native_schema(table: exp.Table, source: dict, configuration: dict) -> str:
    """Native schema for ``table``: its own schema part, else the source default.

    The reference's own schema wins (it is exact); otherwise fall back to the
    source's configured database, then its discovered schema. Empty only when none
    is available, which the caller treats as unrewritable.
    """
    if table.db:
        return table.db
    database = str(configuration.get("databaseName") or "")
    if database:
        return database
    discovered = [str(s) for s in source.get("discoveredSchemas") or []]
    return discovered[0] if discovered else ""


def to_native(
    sql: str,
    *,
    dialect: str,
    routing: dict[str, dict[str, str]],
    sources: dict[str, dict],
) -> NativeTranspileResult:
    """Rewrite ``sql`` from Athena/Trino into a single federated source's dialect.

    Args:
        sql: The firewall-approved statement, in ``dialect``.
        dialect: sqlglot dialect of ``sql`` (e.g. ``athena``, or a direct-JDBC one).
        routing: ``datasourceRouting``-shaped map (bare table -> ``{datasourceId}``),
            as produced by ``sql_generator.sql_table_routing``.
        sources: datasourceId -> source record, fetched by the handler. The
            ``configuration`` field is parsed (a dict) before being passed in.

    Returns:
        A :class:`NativeTranspileResult`. ``applied`` is True only when the
        statement was rewritten into the source's native dialect and catalog;
        otherwise ``sql`` is returned untouched with a ``reason`` (``None`` for the
        already-native direct-JDBC case).
    """
    # A direct-JDBC dialect is already the source engine's own — nothing to do.
    if dialect in _DIRECT_JDBC_DIALECTS:
        return NativeTranspileResult(applied=False, sql=sql, dialect=dialect, reason=None, is_direct_jdbc=True)

    datasources = distinct_datasources(routing)
    if len(datasources) >= 2:
        return NativeTranspileResult(applied=False, sql=sql, dialect=dialect, reason="cross_source")

    # No routed datasource (e.g. all-CTE or unrouted SQL) → nothing to attribute to
    # a native engine; return the Athena form unchanged.
    if not datasources:
        return NativeTranspileResult(applied=False, sql=sql, dialect=dialect, reason="engine_not_transpilable")

    ds_id = next(iter(datasources))
    source = sources.get(ds_id) or {}
    sub_type = str(source.get("sourceSubType") or "")
    native_dialect = _SUB_TYPE_NATIVE_DIALECT.get(sub_type)
    if native_dialect is None:
        return NativeTranspileResult(applied=False, sql=sql, dialect=dialect, reason="engine_not_transpilable")

    configuration = source.get("configuration")
    configuration = configuration if isinstance(configuration, dict) else {}
    native_catalog = _native_catalog(source, configuration)
    if not native_catalog:
        return NativeTranspileResult(applied=False, sql=sql, dialect=dialect, reason="no_native_catalog")

    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError as exc:
        # Type only, never str(exc): sqlglot embeds a window of the statement
        # (literals included) in its message.
        logger.warning("native_transpile_parse_failed", error=type(exc).__name__, sql_len=len(sql))
        return NativeTranspileResult(
            applied=False, sql=sql, dialect=dialect, reason=f"transpile_failed:{type(exc).__name__}"
        )

    # Rewrite every REAL table reference (CTEs skipped) to the native 3-part
    # catalog.schema.table, dropping the Athena DataCatalog name entirely.
    for table in real_tables(parsed):
        schema = _native_schema(table, source, configuration)
        if not schema:
            # A reference with no derivable schema cannot be rewritten to a 3-part
            # native name; fail closed rather than emit a half-qualified statement.
            logger.warning("native_transpile_no_schema", table=table.name)
            return NativeTranspileResult(applied=False, sql=sql, dialect=dialect, reason="no_native_catalog")
        table.set("db", exp.to_identifier(schema))
        table.set("catalog", exp.to_identifier(native_catalog))

    native_athena_form = parsed.sql(dialect=dialect)
    try:
        transpiled = sqlglot.transpile(native_athena_form, read=dialect, write=native_dialect)[0]
    except sqlglot.errors.SqlglotError as exc:
        logger.warning(
            "native_transpile_failed",
            error=type(exc).__name__,
            read=dialect,
            write=native_dialect,
            sql_len=len(sql),
        )
        return NativeTranspileResult(
            applied=False, sql=sql, dialect=dialect, reason=f"transpile_failed:{type(exc).__name__}"
        )

    logger.info("native_transpile_applied", datasource_id=ds_id, dialect=native_dialect, catalog=native_catalog)
    return NativeTranspileResult(
        applied=True,
        sql=transpiled,
        dialect=native_dialect,
        reason=None,
        catalog=native_catalog,
    )
