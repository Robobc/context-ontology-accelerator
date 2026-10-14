"""Normalize Managed-KB results and assemble bounded, cited context."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from typing import Any

from coa_common import strip_hidden_model_characters

_IRI_LOCAL_NAME = re.compile(r"<[^>]*[/#]([A-Za-z_][A-Za-z0-9_-]*)>")
_PREFIXED_NAME = re.compile(r"\b[A-Za-z_][A-Za-z0-9_-]*:([A-Za-z_][A-Za-z0-9_-]*)\b")


@dataclass(frozen=True)
class Evidence:
    """One normalized, attributable retrieval result."""

    evidence_id: str
    target_id: str
    target_kind: str
    text: str
    source: str
    score: float | None
    source_retriever: str | None
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def semantic_concepts(sparql: str, limit: int = 16) -> list[str]:
    """Extract stable class/property local names from COA's SPARQL plan."""
    seen: set[str] = set()
    concepts: list[str] = []
    sparql_without_iris = re.sub(r"<[^>]+>", "", sparql)
    for match in [
        *_IRI_LOCAL_NAME.findall(sparql),
        *_PREFIXED_NAME.findall(sparql_without_iris),
    ]:
        if match.lower() in {"type", "label"} or match in seen:
            continue
        seen.add(match)
        concepts.append(match)
        if len(concepts) >= limit:
            break
    return concepts


def grounded_query(question: str, concepts: list[str]) -> str:
    """Add ontology-derived vocabulary without replacing the user question."""
    if not concepts:
        return question
    return f"{question}\n\nOntology-grounded concepts and relations: {', '.join(concepts)}"


def extract_evidence(
    payload: Any,
    *,
    target_id: str = "managed-kb",
    target_kind: str = "MANAGED_KB",
) -> list[Evidence]:
    """Find result.results arrays in the connector's MCP result."""
    items: list[Evidence] = []
    seen: set[tuple[str, str]] = set()
    for candidate in _result_items(payload):
        content = candidate.get("content")
        text = _content_text(content)
        if not text:
            continue
        metadata = candidate.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        source = _source(candidate, metadata)
        key = (source, text)
        if key in seen:
            continue
        seen.add(key)
        retriever = candidate.get("sourceRetriever")
        identifier = retriever.get("identifier") if isinstance(retriever, dict) else None
        score = candidate.get("score")
        items.append(
            Evidence(
                evidence_id=f"E{len(items) + 1}",
                target_id=target_id,
                target_kind=target_kind,
                text=text,
                source=source,
                score=float(score) if isinstance(score, (int, float)) else None,
                source_retriever=(identifier if isinstance(identifier, str) else None),
                metadata=metadata,
            )
        )
    return items


def assemble_context(
    evidence: list[Evidence],
    *,
    max_items: int,
    max_characters: int,
) -> tuple[str, list[Evidence]]:
    """Build bounded context blocks while retaining evidence identifiers."""
    selected: list[Evidence] = []
    blocks: list[str] = []
    used = 0
    for item in evidence[:max_items]:
        block = (
            f"[{item.evidence_id}] target={item.target_id} kind={item.target_kind} source={item.source}\n{item.text}"
        )
        remaining = max_characters - used
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = block[:remaining]
        blocks.append(block)
        selected.append(item)
        used += len(block)
        if len(block) == remaining:
            break
    return "\n\n".join(blocks), selected


def _result_items(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, str):
        try:
            yield from _result_items(json.loads(value))
        except json.JSONDecodeError:
            return
    elif isinstance(value, list):
        for item in value:
            yield from _result_items(item)
    elif isinstance(value, dict):
        results = value.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    yield item
        for nested in value.values():
            yield from _result_items(nested)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return strip_hidden_model_characters(content).strip()
    if not isinstance(content, dict):
        return ""
    for key in ("text", "byteContent"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return strip_hidden_model_characters(value).strip()
    return ""


def _source(candidate: dict[str, Any], metadata: dict[str, Any]) -> str:
    for key in (
        "x-amz-bedrock-kb-source-uri",
        "sourceUri",
        "source_uri",
        "uri",
    ):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    location = candidate.get("location")
    if isinstance(location, dict):
        for nested in location.values():
            if isinstance(nested, dict):
                uri = nested.get("uri")
                if isinstance(uri, str) and uri:
                    return uri
    return "managed-kb"
