# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for r2rml-ontop-compat.py — the dotted-column rewrite handed to Ontop.

The fixture mirrors the published shape of COA's R2RML (delimited identifiers,
blank-node logical tables, IRI object maps, join conditions) with an ERP
export's ``demand`` table's dotted columns, so what passes here is what the VKG
container will feed Ontop.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.compare import isomorphic

_SCRIPT = Path(__file__).resolve().parents[2] / "r2rml-ontop-compat.py"
_spec = importlib.util.spec_from_file_location("r2rml_ontop_compat", _SCRIPT)
assert _spec and _spec.loader
compat = importlib.util.module_from_spec(_spec)
sys.modules["r2rml_ontop_compat"] = compat
_spec.loader.exec_module(compat)

RR = Namespace("http://www.w3.org/ns/r2rml#")
IND = Namespace("http://example.org/induced#")

FIXTURE = r"""
@prefix ind: <http://example.org/induced#> .
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix scl: <http://coa.amazon.com/vocab/coa#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ind:TriplesMap_Demand a rr:TriplesMap ;
    scl:datasourceId "ds-1" ;
    scl:sourceSchema "datavault" ;
    rr:logicalTable [ rr:tableName "\"demand\"" ] ;
    rr:subjectMap [ rr:class ind:Demand ;
        rr:template "http://example.org/induced#demand/{\"demand_id\"}" ] ;
    rr:predicateObjectMap <http://example.org/induced#TriplesMap_Demand/POM_CostType>,
        <http://example.org/induced#TriplesMap_Demand/POM_Level1>,
        <http://example.org/induced#TriplesMap_Demand/POM_Level2>,
        <http://example.org/induced#TriplesMap_Demand/POM_Project> .

<http://example.org/induced#TriplesMap_Demand/POM_CostType> rr:predicate ind:demand_costType ;
    rr:objectMap [ rr:column "\"cost_type\"" ; rr:datatype xsd:string ] .
<http://example.org/induced#TriplesMap_Demand/POM_Level1> rr:predicate ind:demand_level1 ;
    rr:objectMap [ rr:column "\"Owner.Level_1\"" ; rr:datatype xsd:string ] .
<http://example.org/induced#TriplesMap_Demand/POM_Level2> rr:predicate ind:demand_level2 ;
    rr:objectMap [ rr:column "\"Owner.Level_2\"" ; rr:datatype xsd:string ] .
<http://example.org/induced#TriplesMap_Demand/POM_Project> rr:predicate ind:demand_project ;
    rr:objectMap [ rr:parentTriplesMap ind:TriplesMap_Project ;
        rr:joinCondition [ rr:child "\"project_id\"" ;
                           rr:parent "\"project_id\"" ] ] .

ind:TriplesMap_Project a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\"project\"" ] ;
    rr:subjectMap [ rr:class ind:Project ;
        rr:template "http://example.org/induced#project/{\"project_id\"}" ] ;
    rr:predicateObjectMap [ rr:predicate ind:project_status ;
        rr:objectMap [ rr:column "\"Status\"" ; rr:datatype xsd:string ] ] .

# A map joining INTO demand on its dotted column: exercises the rr:parent rewrite.
ind:TriplesMap_Function a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\"function\"" ] ;
    rr:subjectMap [ rr:class ind:Function ; rr:template "http://example.org/induced#function/{\"name\"}" ] ;
    rr:predicateObjectMap [ rr:predicate ind:function_demand ;
        rr:objectMap [ rr:parentTriplesMap ind:TriplesMap_Demand ;
            rr:joinCondition [ rr:child "\"name\"" ; rr:parent "\"Owner.Level_1\"" ] ] ] .
"""


def _graph(ttl: str = FIXTURE) -> Graph:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


def _logical(g: Graph, tmap):
    return next(g.objects(tmap, RR.logicalTable))


def _columns(g: Graph) -> set[str]:
    return {str(o) for o in g.objects(None, RR.column)}


class TestIdentifiers:
    def test_delimited_and_regular(self):
        assert compat.parse_sql_identifier('"Owner.Level_1"') == "Owner.Level_1"
        assert compat.parse_sql_identifier('"a""b"') == 'a"b'
        assert compat.parse_sql_identifier("amount") == "amount"
        assert compat.delimit('a"b') == '"a""b"'

    def test_alias_is_dot_free_and_collision_safe(self):
        assert compat.alias_for("Owner.Level_1", set()) == "Owner_Level_1"
        assert compat.alias_for("Owner.Level_1", {"Owner_Level_1"}) == "Owner_Level_1_2"
        assert compat.alias_for("Phase_1.1_Track", set()) == "Phase_1_1_Track"


class TestRewrite:
    def test_dotted_table_becomes_aliased_view(self):
        g = _graph()
        report = compat.rewrite_dotted_columns(g)

        assert [r["table"] for r in report] == ['"demand"']
        assert report[0]["aliases"] == {"Owner.Level_1": "Owner_Level_1", "Owner.Level_2": "Owner_Level_2"}

        lt = _logical(g, IND.TriplesMap_Demand)
        assert next(g.objects(lt, RR.tableName), None) is None
        sql = str(next(g.objects(lt, RR.sqlQuery)))
        assert sql.startswith("SELECT ") and sql.endswith(' FROM "demand"')
        assert '"Owner.Level_1" AS "Owner_Level_1"' in sql
        assert '"Owner.Level_2" AS "Owner_Level_2"' in sql
        # every column the map references is projected, including the join key
        for col in ('"demand_id"', '"cost_type"', '"project_id"'):
            assert col in sql
        # the dotted names appear ONLY on the left of an AS
        assert sql.count("Owner.Level_1") == 1

    def test_column_references_point_at_alias(self):
        g = _graph()
        compat.rewrite_dotted_columns(g)
        cols = _columns(g)
        assert '"Owner_Level_1"' in cols and '"Owner_Level_2"' in cols
        assert not any("." in compat.parse_sql_identifier(c) for c in cols)
        assert '"cost_type"' in cols  # non-dotted untouched

    def test_parent_join_from_other_map_is_rewritten(self):
        g = _graph()
        compat.rewrite_dotted_columns(g)
        parents = {str(o) for o in g.objects(None, RR.parent)}
        assert '"Owner_Level_1"' in parents
        assert '"Owner.Level_1"' not in parents
        # the child side (a column of "function") is not ours to touch
        assert '"name"' in {str(o) for o in g.objects(None, RR.child)}

    def test_untouched_maps_keep_table_name(self):
        g = _graph()
        maps = (IND.TriplesMap_Project, IND.TriplesMap_Function)
        before = {t: str(next(g.objects(_logical(g, t), RR.tableName))) for t in maps}
        compat.rewrite_dotted_columns(g)
        for t, name in before.items():
            assert str(next(g.objects(_logical(g, t), RR.tableName))) == name
        assert '"Status"' in _columns(g)

    def test_template_placeholder_is_rewritten(self):
        ttl = FIXTURE.replace(
            'rr:template "http://example.org/induced#demand/{\\"demand_id\\"}"',
            'rr:template "http://example.org/induced#demand/{\\"demand_id\\"}/{\\"Phase_1.1_Track\\"}"',
        )
        g = _graph(ttl)
        report = compat.rewrite_dotted_columns(g)
        assert report[0]["aliases"]["Phase_1.1_Track"] == "Phase_1_1_Track"
        sm = next(g.objects(IND.TriplesMap_Demand, RR.subjectMap))
        tmpl = str(next(g.objects(sm, RR.template)))
        assert '{"Phase_1_1_Track"}' in tmpl and '{"demand_id"}' in tmpl
        sql = str(next(g.objects(_logical(g, IND.TriplesMap_Demand), RR.sqlQuery)))
        assert '"Phase_1.1_Track" AS "Phase_1_1_Track"' in sql

    def test_alias_collision_with_real_column(self):
        ttl = FIXTURE.replace(
            'rr:objectMap [ rr:column "\\"cost_type\\"" ; rr:datatype xsd:string ]',
            'rr:objectMap [ rr:column "\\"Owner_Level_1\\"" ; rr:datatype xsd:string ]',
        )
        g = _graph(ttl)
        report = compat.rewrite_dotted_columns(g)
        assert report[0]["aliases"]["Owner.Level_1"] == "Owner_Level_1_2"
        cols = _columns(g)
        assert '"Owner_Level_1"' in cols and '"Owner_Level_1_2"' in cols

    def test_idempotent(self):
        g = _graph()
        compat.rewrite_dotted_columns(g)
        once = Graph()
        once += g
        assert compat.rewrite_dotted_columns(g) == []
        assert isomorphic(once, g)

    def test_no_dotted_columns_is_a_no_op(self):
        g = _graph()
        g.remove((None, RR.column, Literal('"Owner.Level_1"')))
        g.remove((None, RR.column, Literal('"Owner.Level_2"')))
        g.remove((None, RR.parent, Literal('"Owner.Level_1"')))
        before = Graph()
        before += g
        assert compat.rewrite_dotted_columns(g) == []
        assert isomorphic(before, g)


class TestCli:
    def test_writes_output_and_reports(self, tmp_path: Path):
        src = tmp_path / "mappings.r2rml"
        dst = tmp_path / "mappings.ontop.r2rml"
        src.write_text(FIXTURE)
        proc = subprocess.run([sys.executable, str(_SCRIPT), str(src), str(dst)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        report = json.loads(proc.stdout)
        assert report["status"] == "ok" and [r["table"] for r in report["rewritten"]] == ['"demand"']
        out = Graph()
        out.parse(dst, format="turtle")
        assert any("Owner_Level_1" in str(o) for o in out.objects(None, RR.sqlQuery))
        # source untouched: translate-server keeps reading rr:tableName from it
        kept = Graph()
        kept.parse(src, format="turtle")
        assert '"demand"' in {str(o) for o in kept.objects(None, RR.tableName)}

    def test_unparseable_input_fails_closed(self, tmp_path: Path):
        src = tmp_path / "broken.r2rml"
        dst = tmp_path / "out.r2rml"
        src.write_text("this is not turtle @@@")
        proc = subprocess.run([sys.executable, str(_SCRIPT), str(src), str(dst)], capture_output=True, text=True)
        assert proc.returncode == 1
        assert json.loads(proc.stdout)["status"] == "failed"
        assert not dst.exists()

    def test_usage_error(self):
        proc = subprocess.run([sys.executable, str(_SCRIPT)], capture_output=True, text=True)
        assert proc.returncode == 2


@pytest.mark.parametrize("name", ["Owner.Level_3", "a.b.c"])
def test_multi_dot_names_alias_every_dot(name):
    assert "." not in compat.alias_for(name, set())
