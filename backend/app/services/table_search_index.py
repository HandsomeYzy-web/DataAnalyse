from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError
from openai import OpenAI

from app.core.settings import Settings


MAX_EMBEDDING_TEXT_CHARS = 12000


class TableSearchIndex:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.index_name = settings.elasticsearch_index
        self.client = self._build_client(settings)
        self.embedding_client = self._build_embedding_client(settings)
        self.embedding_model = settings.embedding_model
        self.last_embedding_error = ""

    def index_table(self, document: dict[str, Any]) -> dict[str, Any]:
        vector = self.embed_text(_document_search_text(document))
        self.ensure_index(vector_dims=len(vector) if vector else None)

        indexed_document = dict(document)
        if vector:
            indexed_document["summary_vector"] = vector
        elif self.last_embedding_error:
            indexed_document["embedding_error"] = self.last_embedding_error

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
                results = self._vector_search(vector, top_k)
                if results:
                    return results
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
            "batch_id": {"type": "keyword"},
            "filename": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "sheet_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "table_title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "parse_mode": {"type": "keyword"},
            "large_table_reason": {"type": "text"},
            "coverage": {"type": "object", "enabled": True},
            "embedding_error": {"type": "text"},
            "summary_text": {"type": "text"},
            "candidate_fields": {"type": "keyword"},
            "normalized_headers": {"type": "text"},
            "hierarchy_definition": {"type": "text"},
            "tree_path_text": {"type": "text"},
            "tree_leaf_text": {"type": "text"},
            "tree_metric_names": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "tree_search_text": {"type": "text"},
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

        mapping = self.client.indices.get_mapping(index=self.index_name)
        index_mapping = mapping[self.index_name]["mappings"].get("properties", {})
        missing_properties = {
            key: value for key, value in properties.items() if key not in index_mapping
        }
        if missing_properties:
            self.client.indices.put_mapping(
                index=self.index_name,
                properties=missing_properties,
            )

        if vector_dims:
            if "summary_vector" not in index_mapping:
                self.client.indices.put_mapping(
                    index=self.index_name,
                    properties={"summary_vector": properties["summary_vector"]},
                )

    def embed_text(self, text: str) -> list[float]:
        self.last_embedding_error = ""
        if not self.embedding_client or not self.embedding_model:
            return []
        if not text.strip():
            return []
        embedding_text = _truncate_embedding_text(text)
        try:
            response = self.embedding_client.embeddings.create(
                model=self.embedding_model,
                input=[embedding_text],
            )
            if not response.data:
                raise ValueError("No embedding data received")
            return list(response.data[0].embedding)
        except Exception as exc:
            self.last_embedding_error = f"{exc.__class__.__name__}: {exc}"
            print(
                f"[EMBEDDING_FALLBACK_TEXT_ONLY] {self.last_embedding_error}",
                flush=True,
            )
            return []

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
                        "tree_metric_names^6",
                        "tree_path_text^5",
                        "table_title^4",
                        "summary_text^3",
                        "tree_leaf_text^3",
                        "filename^2",
                        "sheet_name^2",
                        "candidate_fields^2",
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
    tree: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tree_fields = build_tree_index_fields(tree or {})
    candidate_fields = _dedupe_strings(
        [
            *_extract_candidate_fields(normalized_headers),
            *_extract_candidate_fields(hierarchy_definition),
        ]
    )
    indexed_summary = _append_metrics_to_summary(summary_text, candidate_fields)
    return {
        "summary_text": indexed_summary,
        "candidate_fields": candidate_fields,
        **tree_fields,
        "created_at": datetime.now(UTC).isoformat(),
    }


def build_tree_index_fields(tree: dict[str, Any]) -> dict[str, list[str] | str]:
    path_texts: list[str] = []
    leaf_texts: list[str] = []
    metric_names: list[str] = []

    def walk(node: Any, path: list[str]) -> None:
        if len(path_texts) >= 2000:
            return

        if isinstance(node, dict):
            if node and all(not isinstance(value, dict) for value in node.values()):
                _append_tree_index_record(
                    path=path,
                    data=node,
                    path_texts=path_texts,
                    leaf_texts=leaf_texts,
                    metric_names=metric_names,
                )
                return
            for key, value in node.items():
                walk(value, [*path, str(key)])
            return

        _append_tree_index_record(
            path=path,
            data=node,
            path_texts=path_texts,
            leaf_texts=leaf_texts,
            metric_names=metric_names,
        )

    walk(tree, [])
    deduped_paths = _dedupe_strings(path_texts, limit=2000)
    deduped_leafs = _dedupe_strings(leaf_texts, limit=2000)
    deduped_metrics = _dedupe_strings(metric_names, limit=300)
    tree_search_text = "\n".join(
        [
            "树路径：",
            *deduped_paths[:400],
            "树叶子数据：",
            *deduped_leafs[:400],
            "树指标：",
            "、".join(deduped_metrics),
        ]
    )
    return {
        "tree_path_text": deduped_paths,
        "tree_leaf_text": deduped_leafs,
        "tree_metric_names": deduped_metrics,
        "tree_search_text": tree_search_text,
    }


def _append_tree_index_record(
    path: list[str],
    data: Any,
    path_texts: list[str],
    leaf_texts: list[str],
    metric_names: list[str],
) -> None:
    path_text = " | ".join(path)
    leaf_text = _leaf_to_text(data)
    if path_text:
        path_texts.append(path_text)
        metric_names.extend(_extract_metric_names_from_text(path_text))
    if leaf_text:
        leaf_texts.append(leaf_text)
        metric_names.extend(_extract_metric_names_from_text(leaf_text))
    if isinstance(data, dict):
        for key in data.keys():
            metric_names.extend(_extract_metric_names_from_text(str(key)))


def _leaf_to_text(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{key}={item}" for key, item in value.items())
    return str(value)


def _extract_metric_names_from_text(text: str) -> list[str]:
    values: list[str] = []
    for part in re.split(r"[|;；,，:：=]+", text):
        cleaned = part.strip()
        if not cleaned:
            continue
        values.append(cleaned)
        if " - " in cleaned:
            values.extend(item.strip() for item in cleaned.split(" - ") if item.strip())
    return [
        value
        for value in values
        if 1 < len(value) <= 100 and not re.fullmatch(r"[\d.\-]+", value)
    ]


def _document_search_text(document: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in [
        "table_title",
        "filename",
        "sheet_name",
        "summary_text",
    ]:
        value = document.get(key)
        if value:
            parts.append(str(value))
    fields = document.get("candidate_fields") or []
    if fields and "指标：" not in str(document.get("summary_text") or ""):
        parts.append("指标：" + "、".join(str(item) for item in fields[:120]))
    return "\n".join(parts)


def _truncate_embedding_text(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= MAX_EMBEDDING_TEXT_CHARS:
        return cleaned

    head_chars = int(MAX_EMBEDDING_TEXT_CHARS * 0.75)
    tail_chars = MAX_EMBEDDING_TEXT_CHARS - head_chars
    return f"{cleaned[:head_chars]}\n...\n{cleaned[-tail_chars:]}"


def _append_metrics_to_summary(summary_text: str, candidate_fields: list[str]) -> str:
    summary = summary_text.strip()
    metrics = _dedupe_strings(candidate_fields, limit=80)
    if not metrics:
        return summary

    metric_text = "、".join(metrics)
    if summary:
        return f"{summary}\n\n指标：{metric_text}"
    return f"指标：{metric_text}"


def _extract_candidate_fields(text: str) -> list[str]:
    parsed = _try_parse_json_like(text)
    values: list[str] = []
    if parsed is not None:
        _collect_strings(parsed, values)
    if not values:
        values = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_#（）()、\-]{2,}", text)
    return [
        value.strip()
        for value in values
        if _is_candidate_metric_text(value.strip())
    ]


def _is_candidate_metric_text(value: str) -> bool:
    if not (1 < len(value) <= 80):
        return False
    if re.fullmatch(r"[\d.\-]+", value):
        return False
    if re.fullmatch(r"\$?[A-Z]+\$?\d+(:\$?[A-Z]+\$?\d+)?", value.upper()):
        return False
    if value in {
        "header",
        "group",
        "col",
        "ref",
        "role",
        "value",
        "row",
        "path",
        "notes",
        "table_type",
        "table_range",
        "title_ranges",
        "header_ranges",
        "data_row_range",
        "hierarchy_columns",
        "value_columns",
        "hierarchy_fill_down",
        "validation_warnings",
        "coverage",
        "data_rows",
        "expected_cells",
        "covered_cells",
        "missing_cells",
        "skipped_rows",
        "is_complete",
    }:
        return False
    return True


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
