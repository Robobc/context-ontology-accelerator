#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Rewrite an R2RML mapping so Ontop accepts column names that contain a dot.

Ontop (5.x) rejects any ``rr:column`` / template / join-condition column whose
name contains ``.`` — even as a properly delimited identifier such as
``"Owner.Level_1"`` — with ``Fully qualified columns as "..." are not
accepted``. The R2RML is valid (a delimited identifier is one column); the
parser treats the dot as ``table.column`` regardless of quoting. Real-world
exports (ERP and portfolio-management exports: ``Owner.Level_1``,
``Phase_1.1_Track``) have such columns, so a published mapping that is correct
for every other R2RML engine leaves the VKG unable to reformulate anything.

The rewrite is the standard R2RML workaround applied mechanically: every
TriplesMap whose logical table has a dotted column gets an ``rr:sqlQuery`` view
that aliases the dotted columns (``"Owner.Level_1" AS "Owner_Level_1"``), and
every reference to those columns inside the TriplesMap — ``rr:column``,
``rr:template`` placeholders, ``rr:child`` of its own join conditions, and
``rr:parent`` of other TriplesMaps joining to it — is pointed at the alias.
TriplesMaps without dotted columns are left untouched, so the output is
byte-for-byte equivalent in meaning and idempotent (a second run changes nothing).

The ORIGINAL mapping stays the source of truth for ``translate-server.py``'s
datasource routing, which reads ``rr:tableName``; only Ontop is handed the
rewritten file. Ontop's generated SQL then references the alias inside the view
subquery, which stays executable downstream because the alias is defined there.

Usage: r2rml-ontop-compat.py <in.r2rml> <out.r2rml>
Prints a JSON report on stdout; exits non-zero only on a parse/serialize failure
(the caller falls back to the original mapping).
"""

from __future__ import annotations

import json
import re
import sys

from rdflib import BNode, Graph, Literal, Namespace, URIRef

RR = Namespace("http://www.w3.org/ns/r2rml#")

# R2RML template placeholder: ``{...}`` with ``\{`` / ``\}`` escapes allowed inside.
_TEMPLATE_REF = re.compile(r"(?<!\\)\{((?:[^{}\\]|\\.)*)\}")


def parse_sql_identifier(raw: str) -> str:
    """Return the column NAME an R2RML SQL identifier literal denotes.

    ``'"Owner.Level_1"'`` -> ``Owner.Level_1`` (delimited: strip the quotes and
    un-double embedded ``""``); ``'amount'`` -> ``amount`` (regular identifier,
    kept verbatim — case folding is Ontop's business, not ours).
    """
    s = raw.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('""', '"')
    return s


def delimit(name: str) -> str:
    """SQL-delimit a column name: ``Owner_Level_1`` -> ``"Owner_Level_1"``."""
    return '"' + name.replace('"', '""') + '"'


def alias_for(name: str, taken: set[str]) -> str:
    """Dot-free alias for ``name`` that collides with nothing in ``taken``."""
    base = name.replace(".", "_")
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def _column_refs(g: Graph, tmap: URIRef | BNode) -> list[tuple]:
    """Collect every column reference of ``tmap``'s logical table.

    Returns ``(subject, predicate, literal, [column names])`` tuples in
    deterministic graph order, covering ``rr:column``, ``rr:template``
    placeholders, ``rr:child`` of the map's own joins and ``rr:parent`` of
    other maps joining into it.
    """
    refs: list[tuple] = []

    def term_map(node) -> None:
        for lit in g.objects(node, RR.column):
            refs.append((node, RR.column, lit, [parse_sql_identifier(str(lit))]))
        for lit in g.objects(node, RR.template):
            cols = [parse_sql_identifier(m) for m in _TEMPLATE_REF.findall(str(lit))]
            if cols:
                refs.append((node, RR.template, lit, cols))
        for gm in g.objects(node, RR.graphMap):
            term_map(gm)

    for sm in g.objects(tmap, RR.subjectMap):
        term_map(sm)
    for pom in g.objects(tmap, RR.predicateObjectMap):
        for pm in g.objects(pom, RR.predicateMap):
            term_map(pm)
        for gm in g.objects(pom, RR.graphMap):
            term_map(gm)
        for om in g.objects(pom, RR.objectMap):
            term_map(om)
            # rr:child names a column of THIS table (the child side of the join).
            for jc in g.objects(om, RR.joinCondition):
                for lit in g.objects(jc, RR.child):
                    refs.append((jc, RR.child, lit, [parse_sql_identifier(str(lit))]))
    # Other TriplesMaps joining to this one name OUR columns in rr:parent.
    for om in g.subjects(RR.parentTriplesMap, tmap):
        for jc in g.objects(om, RR.joinCondition):
            for lit in g.objects(jc, RR.parent):
                refs.append((jc, RR.parent, lit, [parse_sql_identifier(str(lit))]))
    return refs


def _rewrite_template(template: str, aliases: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = parse_sql_identifier(m.group(1))
        return "{" + delimit(aliases[name]) + "}" if name in aliases else m.group(0)

    return _TEMPLATE_REF.sub(sub, template)


def rewrite_dotted_columns(g: Graph) -> list[dict]:
    """Rewrite ``g`` in place; return one report entry per TriplesMap changed."""
    report: list[dict] = []
    # Sorted for a deterministic output regardless of rdflib iteration order.
    tmaps = sorted(set(g.subjects(RR.logicalTable, None)), key=str)
    for tmap in tmaps:
        tables = [(lt, tn) for lt in g.objects(tmap, RR.logicalTable) for tn in g.objects(lt, RR.tableName)]
        if not tables:
            continue  # already an rr:sqlQuery view (or malformed) — nothing to do
        refs = _column_refs(g, tmap)
        columns: list[str] = []
        for *_, cols in refs:
            for c in cols:
                if c not in columns:
                    columns.append(c)
        dotted = [c for c in columns if "." in c]
        if not dotted:
            continue

        taken = set(columns)
        aliases: dict[str, str] = {}
        for c in dotted:
            aliases[c] = alias_for(c, taken)
            taken.add(aliases[c])

        select = ", ".join(f"{delimit(c)} AS {delimit(aliases[c])}" if c in aliases else delimit(c) for c in columns)
        for lt, tn in tables:
            sql = f"SELECT {select} FROM {tn}"
            view = BNode()
            g.remove((tmap, RR.logicalTable, lt))
            g.add((tmap, RR.logicalTable, view))
            g.add((view, RR.sqlQuery, Literal(sql)))
            if not any(True for _ in g.subjects(RR.logicalTable, lt)):
                g.remove((lt, None, None))  # orphaned base-table node

        for s, p, lit, cols in refs:
            if not any(c in aliases for c in cols):
                continue
            new = _rewrite_template(str(lit), aliases) if p == RR.template else delimit(aliases[cols[0]])
            g.remove((s, p, lit))
            g.add((s, p, Literal(new, datatype=lit.datatype, lang=lit.language)))

        report.append(
            {
                "triplesMap": str(tmap),
                "table": str(tables[0][1]),
                "aliases": aliases,
            }
        )
    return report


def main(argv: list[str]) -> int:
    """CLI: ``<in.r2rml> <out.r2rml>``; JSON report on stdout, non-zero on failure."""
    if len(argv) != 3:
        print("usage: r2rml-ontop-compat.py <in.r2rml> <out.r2rml>", file=sys.stderr)
        return 2
    src, dst = argv[1], argv[2]
    try:
        g = Graph()
        g.parse(src, format="turtle")
        report = rewrite_dotted_columns(g)
        g.serialize(destination=dst, format="turtle")
    except Exception as e:  # noqa: BLE001 — any failure means "use the original mapping"
        print(json.dumps({"status": "failed", "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps({"status": "ok", "rewritten": report, "source": src, "output": dst}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
