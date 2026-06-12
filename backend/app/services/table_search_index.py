from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError
from openai import OpenAI

from app.core.settings import Settings


class TableSearchIndex:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.index_name = settings.elasticsearch_index
        self.client = self._build_client(settings)
        self.embedding_client = self._build_embedding_client(settings)
        self.embedding_model = settings.embedding_model

    def index_table(self, document: dict[str, Any]) -> dict[str, Any]:
        vector = self.embed_text(document["summary_text"])
        self.ensure_index(vector_dims=len(vector) if vector else None)

        indexed_document = dict(document)
        if vector:
            indexed_document["summary_vector"] = vector

        self.client.index(
            index=self.index_name,
            id=document["table_id"],
            document=indexed_document,
            refresh="wait_for",
        )
        return indexed_document

    def search(self, question: str, top_k: int = 3) -> list[dict[str, Any]]:
        if not self.client.indices.exists(index=self.index_name):
            return []

        vector = self.embed_text(question)
        if vector:
            try:
                return self._vector_search(vector, top_k)
            except Exception:
                pass
        return self._text_search(question, top_k)

    def get_table_document(self, table_id: str) -> dict[str, Any] | None:
        try:
            response = self.client.get(index=self.index_name, id=table_id)
        except NotFoundError:
            return None
        source = response.get("_source")
        if not isinstance(source, dict):
            return None
        return source

    def list_tables(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.client.indices.exists(index=self.index_name):
            return []

        response = self.client.search(
            index=self.index_name,
            body={
                "query": {"match_all": {}},
                "sort": [
                    {"created_at": {"order": "desc", "unmapped_type": "date"}},
                ],
                "_source": {"excludes": ["summary_vector"]},
            },
            size=limit,
        )
        return [_hit_to_result(hit) for hit in response["hits"]["hits"]]

    def ensure_index(self, vector_dims: int | None = None) -> None:
        properties: dict[str, Any] = {
            "table_id": {"type": "keyword"},
            "filename": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "sheet_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "table_title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "summary_text": {"type": "text"},
            "candidate_fields": {"type": "keyword"},
            "normalized_headers": {"type": "text"},
            "hierarchy_definition": {"type": "text"},
            "source_object": {"type": "keyword"},
            "xlsx_object": {"type": "keyword"},
            "tree_object": {"type": "keyword"},
            "created_at": {"type": "date"},
        }
        if vector_dims:
            properties["summary_vector"] = {
                "type": "dense_vector",
                "dims": vector_dims,
                "index": True,
                "similarity": "cosine",
            }

        if not self.client.indices.exists(index=self.index_name):
            self.client.indices.create(
                index=self.index_name,
                mappings={"properties": properties},
            )
            return

        if vector_dims:
            mapping = self.client.indices.get_mapping(index=self.index_name)
            index_mapping = mapping[self.index_name]["mappings"].get("properties", {})
            if "summary_vector" not in index_mapping:
                self.client.indices.put_mapping(
                    index=self.index_name,
                    properties={"summary_vector": properties["summary_vector"]},
                )

    def embed_text(self, text: str) -> list[float]:
        if not self.embedding_client or not self.embedding_model:
            return []
        response = self.embedding_client.embeddings.create(
            model=self.embedding_model,
            input=[text],
        )
        return list(response.data[0].embedding)

    def _vector_search(self, vector: list[float], top_k: int) -> list[dict[str, Any]]:
        response = self.client.search(
            index=self.index_name,
            body={
                "knn": {
                    "field": "summary_vector",
                    "query_vector": vector,
                    "k": top_k,
                    "num_candidates": max(top_k * 10, 50),
                },
                "_source": True,
            },
            size=top_k,
        )
        return [_hit_to_result(hit) for hit in response["hits"]["hits"]]

    def _text_search(self, question: str, top_k: int) -> list[dict[str, Any]]:
        response = self.client.search(
            index=self.index_name,
            query={
                "multi_match": {
                    "query": question,
                    "fields": [
                        "table_title^4",
                        "summary_text^3",
                        "filename^2",
                        "sheet_name^2",
                        "normalized_headers",
                        "hierarchy_definition",
                    ],
                }
            },
            size=top_k,
        )
        return [_hit_to_result(hit) for hit in response["hits"]["hits"]]

    @staticmethod
    def _build_client(settings: Settings) -> Elasticsearch:
        kwargs: dict[str, Any] = {
            "hosts": [settings.elasticsearch_url],
            "verify_certs": settings.elasticsearch_verify_certs,
        }
        if settings.elasticsearch_api_key:
            kwargs["api_key"] = settings.elasticsearch_api_key
        elif settings.elasticsearch_username:
            kwargs["basic_auth"] = (
                settings.elasticsearch_username,
                settings.elasticsearch_password,
            )
        return Elasticsearch(**kwargs)

    @staticmethod
    def _build_embedding_client(settings: Settings) -> OpenAI | None:
        if not settings.embedding_model:
            return None
        api_key = settings.embedding_api_key or settings.llm_api_key
        base_url = settings.embedding_base_url or settings.llm_base_url
        if not api_key:
            return None
        return OpenAI(api_key=api_key, base_url=base_url or None)


def build_table_summary(
    normalized_headers: str,
    hierarchy_definition: str,
    summary_text: str,
) -> dict[str, Any]:
    candidate_fields = _dedupe_strings(
        [
            *_extract_candidate_fields(normalized_headers),
            *_extract_candidate_fields(hierarchy_definition),
        ]
    )
    return {
        "summary_text": summary_text.strip(),
        "candidate_fields": candidate_fields,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _extract_candidate_fields(text: str) -> list[str]:
    parsed = _try_parse_json_like(text)
    values: list[str] = []
    if parsed is not None:
        _collect_strings(parsed, values)
    if not values:
        values = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_#（）()、\-]{2,}", text)
    return [value.strip() for value in values if 1 < len(value.strip()) <= 80]


def _try_parse_json_like(text: str) -> Any:
    candidate = text.strip()
    if "```json" in candidate:
        candidate = candidate.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in candidate:
        candidate = candidate.split("```", 1)[1].split("```", 1)[0].strip()
    try:
        return json.loads(candidate)
    except Exception:
        pass

    start_positions = [candidate.find("{"), candidate.find("[")]
    starts = [item for item in start_positions if item >= 0]
    if not starts:
        return None
    start = min(starts)
    end = max(candidate.rfind("}"), candidate.rfind("]")) + 1
    if end <= start:
        return None
    try:
        return json.loads(candidate[start:end])
    except Exception:
        return None


def _collect_strings(value: Any, output: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            output.append(str(key))
            _collect_strings(item, output)
    elif isinstance(value, list):
        for item in value:
            _collect_strings(item, output)
    elif isinstance(value, str):
        output.append(value)


def _dedupe_strings(values: list[str], limit: int = 160) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = re.sub(r"\s+", "", value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _hit_to_result(hit: dict[str, Any]) -> dict[str, Any]:
    source = hit.get("_source", {})
    return {
        "score": hit.get("_score"),
        **source,
    }
