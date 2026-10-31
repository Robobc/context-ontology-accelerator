# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fork: a Bedrock Managed Knowledge Base is routed like any other source.

Serve learns which Knowledge Base a namespace reads from the namespace's
``BEDROCK_MANAGED_KB`` source record (sources registry), binds it to the request
at admission, and reaches a Knowledge Base in another account through the
source's ``crossAccountRoleArn`` assumed with the namespace's ExternalId, the
same contract the kg-build task and the Databricks connector follow. These
tests pin the lookup, the fail-closed admission, the per-source retriever cache
and the refreshable assume.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_serve.clients.datasource_session import DatasourceAssumeError, refreshable_datasource_session
from coa_serve.clients.sources_registry import BedrockKbSource, BedrockKbSourceUnavailableError, SourcesRegistry
from coa_serve.request_document_source import (
    bind_request_document_source,
    get_request_document_source,
    reset_request_document_source,
)
from coa_serve.tier3.bedrock_kb_retriever import BedrockKnowledgeBaseRetriever, KbRetrieverCache

NS = "0f1e2d3c-4b5a-4697-8899-aabbccddeeff"
KB = "ABCDEFGHIJ"
ROLE = "arn:aws:iam::111122223333:role/coa-dev-datasource-access-kb"


def _kb_item(**overrides):
    item = {
        "sourceType": "DOCUMENTS",
        "subType": "BEDROCK_MANAGED_KB",
        "sourceId": "DS#kb1",
        "bedrockKnowledgeBaseConfiguration": {"knowledgeBaseId": KB},
    }
    item.update(overrides)
    return item


def _registry(items):
    with patch("boto3.resource") as mock_resource:
        mock_table = MagicMock()
        # None = a truncated page, which the registry reports as "unknown".
        mock_table.query.return_value = (
            {"Items": items} if items is not None else {"Items": [], "LastEvaluatedKey": {"PK": "NS#x"}}
        )
        mock_resource.return_value.Table.return_value = mock_table
        return SourcesRegistry(table_name="test-sources", region="us-west-2")


def _source(**overrides) -> BedrockKbSource:
    base = {
        "namespace_id": NS,
        "source_id": "DS#kb1",
        "knowledge_base_id": KB,
        "region": None,
        "cross_account_role_arn": None,
    }
    base.update(overrides)
    return BedrockKbSource(**base)


@pytest.mark.unit
class TestFindBedrockKbSource:
    @pytest.mark.asyncio
    async def test_none_when_the_namespace_has_no_kb_source(self):
        registry = _registry(
            [{"sourceType": "DATABASE", "sourceId": "s1"}, {"sourceType": "DOCUMENTS", "sourceId": "d1"}]
        )
        assert await registry.find_bedrock_kb_source(NS) is None

    @pytest.mark.asyncio
    async def test_returns_the_registered_source_with_its_role_and_region(self):
        cfg = {
            "knowledgeBaseId": KB,
            "crossAccountRoleArn": ROLE,
            "region": "eu-central-1",
            "dataSourceIds": ["DS1234567A"],
        }
        registry = _registry([_kb_item(bedrockKnowledgeBaseConfiguration=cfg)])
        source = await registry.find_bedrock_kb_source(NS)
        assert source == BedrockKbSource(
            namespace_id=NS,
            source_id="DS#kb1",
            knowledge_base_id=KB,
            region="eu-central-1",
            cross_account_role_arn=ROLE,
            data_source_ids=("DS1234567A",),
        )

    @pytest.mark.asyncio
    async def test_same_account_source_has_no_role(self):
        source = await _registry([_kb_item()]).find_bedrock_kb_source(NS)
        assert source is not None and source.cross_account_role_arn is None and source.region is None

    @pytest.mark.asyncio
    async def test_unreadable_inventory_raises_instead_of_failing_open(self):
        with pytest.raises(BedrockKbSourceUnavailableError, match="unavailable"):
            await _registry(None).find_bedrock_kb_source(NS)  # truncated page: the composition would fail open

    @pytest.mark.asyncio
    async def test_two_kb_sources_are_refused(self):
        registry = _registry([_kb_item(), _kb_item(sourceId="DS#kb2")])
        with pytest.raises(BedrockKbSourceUnavailableError, match="more than one"):
            await registry.find_bedrock_kb_source(NS)

    @pytest.mark.asyncio
    async def test_record_without_a_kb_id_is_refused(self):
        registry = _registry([_kb_item(bedrockKnowledgeBaseConfiguration={})])
        with pytest.raises(BedrockKbSourceUnavailableError, match="knowledgeBaseId"):
            await registry.find_bedrock_kb_source(NS)

    @pytest.mark.asyncio
    async def test_deployment_without_a_sources_table_has_no_kb_sources(self):
        assert await SourcesRegistry(table_name="", region="us-west-2").find_bedrock_kb_source(NS) is None


@pytest.mark.unit
class TestDataSourceScope:
    """Deployed finding: the registered dataSourceIds bound what the namespace reads.

    The graph is built from those data sources alone; a Retrieve over the whole
    KB returned documents from data sources the namespace never registered (same
    fixture file copied into non-ACL data sources), so every Retrieve, search and
    entitlement probe alike, carries the scope.
    """

    def _retriever(self, ids, client):
        from coa_serve.tier3.bedrock_kb_retriever import BedrockKnowledgeBaseRetriever

        return BedrockKnowledgeBaseRetriever(
            KB, client=client, search_mode="managed", enforce_user_context=True, data_source_ids=ids
        )

    @pytest.mark.asyncio
    async def test_search_is_scoped_to_the_registered_data_sources(self, monkeypatch):
        from coa_serve.request_identity import set_request_user

        client = MagicMock()
        client.retrieve.return_value = {"retrievalResults": []}
        set_request_user("alice@example.com")
        await self._retriever(["22THWQRHON"], client).retrieve("q", NS)
        cfg = client.retrieve.call_args.kwargs["retrievalConfiguration"]["managedSearchConfiguration"]
        assert cfg["filter"] == {"equals": {"key": "_data_source_id", "value": "22THWQRHON"}}

    @pytest.mark.asyncio
    async def test_several_data_sources_use_in_and_combine_with_a_server_filter(self):
        from coa_serve.request_identity import set_request_user

        client = MagicMock()
        client.retrieve.return_value = {"retrievalResults": []}
        set_request_user("alice@example.com")
        r = self._retriever(["B", "A", "A"], client)
        await r.retrieve("q", NS, retrieval_filter={"equals": {"key": "lang", "value": "en"}})
        cfg = client.retrieve.call_args.kwargs["retrievalConfiguration"]["managedSearchConfiguration"]
        assert cfg["filter"] == {
            "andAll": [
                {"in": {"key": "_data_source_id", "value": ["A", "B"]}},
                {"equals": {"key": "lang", "value": "en"}},
            ]
        }

    @pytest.mark.asyncio
    async def test_entitlement_probe_carries_the_scope(self):
        client = MagicMock()
        client.retrieve.return_value = {"retrievalResults": [{"content": {"text": "x"}}]}
        r = self._retriever(["22THWQRHON"], client)
        assert await r.entitled_source_uris(["https://b/doc.txt"], user_id="alice@example.com") == {"https://b/doc.txt"}
        flt = client.retrieve.call_args.kwargs["retrievalConfiguration"]["managedSearchConfiguration"]["filter"]
        assert flt == {
            "andAll": [
                {"equals": {"key": "_data_source_id", "value": "22THWQRHON"}},
                {"equals": {"key": "_source_uri", "value": "https://b/doc.txt"}},
            ]
        }

    @pytest.mark.asyncio
    async def test_no_data_source_ids_means_the_whole_kb(self):
        from coa_serve.request_identity import set_request_user

        client = MagicMock()
        client.retrieve.return_value = {"retrievalResults": []}
        set_request_user("alice@example.com")
        await self._retriever(None, client).retrieve("q", NS)
        assert "filter" not in client.retrieve.call_args.kwargs["retrievalConfiguration"]["managedSearchConfiguration"]

    def test_cache_builds_the_retriever_with_the_source_scope_and_keys_on_it(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        cache = KbRetrieverCache(default_region="us-east-1", session_factory=MagicMock(return_value=MagicMock()))
        a = cache.get(_source(data_source_ids=("X",)))
        b = cache.get(_source(data_source_ids=("X", "Y")))
        assert a.data_source_ids == ("X",) and b.data_source_ids == ("X", "Y") and a is not b
        assert cache.get(_source(data_source_ids=("Y", "X"))) is b


@pytest.mark.unit
class TestKbRetrieverCache:
    def test_same_account_source_uses_the_runtime_role(self):
        factory = MagicMock()
        cache = KbRetrieverCache(default_region="us-east-1", session_factory=factory)
        with patch("coa_serve.tier3.bedrock_kb_retriever.boto3.Session") as session_cls:
            retriever = cache.get(_source())
        factory.assert_not_called()
        session_cls.assert_called_once_with(region_name="us-east-1")
        assert isinstance(retriever, BedrockKnowledgeBaseRetriever)
        assert retriever.enforces_user_context and retriever._search_mode == "managed"
        assert retriever._knowledge_base_id == KB

    def test_cross_account_source_assumes_the_role_with_the_namespace_external_id(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        factory = MagicMock(return_value=MagicMock())
        cache = KbRetrieverCache(default_region="us-east-1", session_factory=factory)
        cache.get(_source(cross_account_role_arn=ROLE, region="eu-central-1"))
        factory.assert_called_once_with(ROLE, f"coa-dev-{NS}", "eu-central-1", f"coa-serve-{NS}")
        factory.return_value.client.assert_called_once()
        assert factory.return_value.client.call_args.args == ("bedrock-agent-runtime",)

    def test_cross_account_source_without_resource_prefix_fails_rather_than_defaulting(self, monkeypatch):
        """The ExternalId is a contract the customer's trust policy names; no default is safe."""
        monkeypatch.delenv("RESOURCE_PREFIX", raising=False)
        cache = KbRetrieverCache(default_region="us-east-1", session_factory=MagicMock())
        with pytest.raises(RuntimeError, match="RESOURCE_PREFIX"):
            cache.get(_source(cross_account_role_arn=ROLE))

    def test_one_retriever_per_source_key(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        factory = MagicMock(return_value=MagicMock())
        cache = KbRetrieverCache(default_region="us-east-1", session_factory=factory)
        with patch("coa_serve.tier3.bedrock_kb_retriever.boto3.Session"):
            first = cache.get(_source())
            assert cache.get(_source()) is first  # cached
            assert cache.get(_source(cross_account_role_arn=ROLE)) is not first  # a different role = new session
            assert cache.get(_source(namespace_id="other")) is not first  # another namespace = its own ExternalId
        assert factory.call_count == 1

    def test_eviction_closes_the_oldest_client(self):
        cache = KbRetrieverCache(default_region="us-east-1", max_entries=1)
        with patch("coa_serve.tier3.bedrock_kb_retriever.boto3.Session") as session_cls:
            first_client, second_client = MagicMock(), MagicMock()
            session_cls.return_value.client.side_effect = [first_client, second_client]
            cache.get(_source())
            cache.get(_source(knowledge_base_id="KLMNOPQRST"))
        first_client.close.assert_called_once()
        second_client.close.assert_not_called()


@pytest.mark.unit
class TestRefreshableDatasourceSession:
    def _sts(self, **overrides):
        sts = MagicMock()
        creds = {
            "AccessKeyId": "AKIA-TEST",
            "SecretAccessKey": "secret",
            "SessionToken": "token",
            "Expiration": datetime.now(UTC) + timedelta(hours=1),
        }
        sts.assume_role.return_value = {"Credentials": creds}
        for k, v in overrides.items():
            setattr(sts, k, v)
        return sts

    def test_requires_role_and_external_id(self):
        with pytest.raises(ValueError, match="role_arn"):
            refreshable_datasource_session("", "ext", "us-east-1", "s", sts_client_factory=self._sts)
        with pytest.raises(ValueError, match="external_id"):
            refreshable_datasource_session(ROLE, "", "us-east-1", "s", sts_client_factory=self._sts)

    def test_assumes_with_the_external_id_and_yields_refreshable_credentials(self):
        sts = self._sts()
        session = refreshable_datasource_session(
            ROLE, f"coa-dev-{NS}", "us-east-1", "x" * 80, sts_client_factory=lambda: sts
        )
        sts.assume_role.assert_called_once_with(RoleArn=ROLE, RoleSessionName="x" * 64, ExternalId=f"coa-dev-{NS}")
        creds = session.get_credentials()
        assert creds.access_key == "AKIA-TEST" and creds.method == "sts-assume-role"
        assert session.region_name == "us-east-1"

    def test_refusal_surfaces_only_the_sts_code(self):
        sts = self._sts()
        sts.assume_role.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "AssumeRole")
        with pytest.raises(DatasourceAssumeError) as info:
            refreshable_datasource_session(ROLE, "ext", "us-east-1", "s", sts_client_factory=lambda: sts)
        assert info.value.code == "AccessDenied"
        assert ROLE not in str(info.value)


@pytest.mark.unit
class TestAdmissionBinding:
    """main._bind_namespace_document_source: registry -> request binding, fail closed."""

    @staticmethod
    def _registry(outcome):
        """A registry whose ``find_bedrock_kb_source`` returns *outcome*, or raises it."""

        async def _find(namespace):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        registry = MagicMock()
        registry.find_bedrock_kb_source = _find
        return registry

    async def _bind(self, registry, cache):
        """Run the admission helper; returns ``(error, bound)`` and leaves no binding behind."""
        import coa_serve.main as main_mod

        token = bind_request_document_source(None)
        try:
            with patch.object(main_mod, "_sources_registry", registry), patch.object(main_mod, "_kb_retrievers", cache):
                error = await main_mod._bind_namespace_document_source(NS, "req-1")
            return error, get_request_document_source()
        finally:
            reset_request_document_source(token)

    @pytest.mark.asyncio
    async def test_namespace_without_a_kb_source_binds_nothing(self):
        cache = MagicMock()
        assert await self._bind(self._registry(None), cache) == (None, None)
        cache.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_kb_source_is_bound_with_its_retriever(self):
        source = _source()
        cache = MagicMock()
        cache.get.return_value = retriever = MagicMock()
        error, bound = await self._bind(self._registry(source), cache)
        assert error is None
        cache.get.assert_called_once_with(source)
        assert bound is not None and bound.namespace == NS and bound.retriever is retriever

    @pytest.mark.asyncio
    async def test_unreadable_inventory_rejects_the_request(self):
        error, bound = await self._bind(
            self._registry(BedrockKbSourceUnavailableError("source inventory unavailable")), MagicMock()
        )
        assert bound is None
        assert error is not None and error["statusCode"] == 502 and "document sources" in error["message"]

    @pytest.mark.asyncio
    async def test_refused_assume_rejects_the_request_without_naming_the_role(self):
        cache = MagicMock()
        cache.get.side_effect = DatasourceAssumeError("AccessDenied")
        error, bound = await self._bind(self._registry(_source(cross_account_role_arn=ROLE)), cache)
        assert bound is None
        assert error is not None and error["statusCode"] == 502
        assert ROLE not in error["message"] and "knowledge base" in error["message"]

    @pytest.mark.asyncio
    async def test_no_registry_means_no_kb_sources(self):
        assert await self._bind(None, None) == (None, None)


@pytest.mark.unit
class TestMainWiring:
    def test_admission_binds_the_document_source_after_the_existence_gate(self):
        """Pin statically: the binding runs on the namespace-scoped request path, before any retrieval."""
        import inspect

        import coa_serve.main as main_mod

        body = inspect.getsource(main_mod._invoke)
        existence = body.index("_sources_registry.namespace_exists(namespace)")
        binding = body.index("await _bind_namespace_document_source(namespace, request_id)")
        authz = body.index("await _authorize_namespace_access(")
        assert existence < binding < authz
