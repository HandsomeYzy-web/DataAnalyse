# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from openai import OpenAI

from app.core.settings import Settings


@dataclass
class TreeRecord:
    id: int
    path: list[str]
    path_text: str
    data: Any
    leaf_text: str
    search_text: str


def flatten_tree(tree: dict[str, Any], metadata: dict[str, Any] | None = None) -> list[TreeRecord]:
    records: list[TreeRecord] = []

    if metadata:
        for key, value in metadata.items():
            if value in (None, ""):
                continue
            _append_record(records, ["表格元信息", str(key)], value)

    def walk(node: Any, path: list[str]) -> None:
        if isinstance(node, dict):
            if node and all(not isinstance(value, dict) for value in node.values()):
                _append_record(records, path, node)
                return
            for key, value in node.items():
                walk(value, [*path, str(key)])
            return

        _append_record(records, path, node)

    walk(tree, [])
    return records


class TableQAService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = self._build_llm(settings)
        self.embedding_client = self._build_embedding_client(settings)
        self.embedding_model = settings.embedding_model

    def answer(
        self,
        question: str,
        tree: dict[str, Any],
        limit: int = 12,
        use_llm: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        records = flatten_tree(tree, metadata=metadata)
        if not records:
            return {
                "answer": "当前语义树为空，无法回答问题。",
                "mode": "empty_tree",
                "evidence_paths": [],
                "candidates": [],
            }

        candidates = self.retrieve_candidates(question, records, limit=max(limit, 20))
        selected = (
            self.rerank_with_llm(question, candidates, limit=limit)
            if use_llm and self.llm is not None
            else candidates[:limit]
        )

        if not selected:
            return {
                "answer": "当前证据不足，未找到与问题相关的路径。",
                "mode": "semantic_retrieval",
                "evidence_paths": [],
                "candidates": [_record_to_dict(record) for record in candidates],
            }

        if use_llm and self.llm is not None:
            answer = self.generate_answer(question, selected)
            mode = "semantic_retrieval_llm"
        else:
            answer = _format_retrieval_answer(selected)
            mode = "semantic_retrieval"

        return {
            "answer": answer,
            "mode": mode,
            "evidence_paths": [_record_to_dict(record) for record in selected],
            "candidates": [_record_to_dict(record) for record in candidates],
        }

    def retrieve_candidates(
        self,
        question: str,
        records: list[TreeRecord],
        limit: int,
    ) -> list[TreeRecord]:
        if self.embedding_client and self.embedding_model:
            return self.semantic_retrieve(question, records, limit=limit)
        if self.llm is not None:
            return self.select_candidates_with_llm(question, records, limit=limit)
        return lexical_retrieve(question, records, limit=limit)

    def semantic_retrieve(
        self,
        question: str,
        records: list[TreeRecord],
        limit: int,
    ) -> list[TreeRecord]:
        texts = [question, *[record.search_text for record in records]]
        vectors = self._embed_texts(texts)
        if len(vectors) != len(texts):
            return lexical_retrieve(question, records, limit=limit)

        question_vector = vectors[0]
        scored = [
            (_cosine_similarity(question_vector, vector), record)
            for vector, record in zip(vectors[1:], records, strict=True)
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, record in scored[:limit]]

    def select_candidates_with_llm(
        self,
        question: str,
        records: list[TreeRecord],
        limit: int,
        batch_size: int = 80,
    ) -> list[TreeRecord]:
        selected_ids: list[int] = []
        for start in range(0, len(records), batch_size):
            batch = records[start : start + batch_size]
            selected_ids.extend(self._select_batch_ids(question, batch, max_ids=limit))
            if len(selected_ids) >= limit * 3:
                break

        if not selected_ids:
            return lexical_retrieve(question, records, limit=limit)

        by_id = {record.id: record for record in records}
        selected = [by_id[item_id] for item_id in selected_ids if item_id in by_id]
        return _dedupe_records(selected)[:limit]

    def rerank_with_llm(
        self,
        question: str,
        candidates: list[TreeRecord],
        limit: int,
    ) -> list[TreeRecord]:
        if not candidates:
            return []

        selected_ids = self._select_batch_ids(question, candidates, max_ids=limit)
        if not selected_ids:
            return candidates[:limit]

        by_id = {record.id: record for record in candidates}
        selected = [by_id[item_id] for item_id in selected_ids if item_id in by_id]
        return _dedupe_records(selected)[:limit] or candidates[:limit]

    def generate_answer(self, question: str, candidates: list[TreeRecord]) -> str:
        evidence = json.dumps(
            [_record_to_dict(record) for record in candidates],
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        prompt = f"""
你是一个表格问答助手。请只根据给定的 evidence_paths 回答用户问题，不要使用外部知识。

要求：
1. 如果证据中有明确答案，直接给出答案，并说明依据的路径。
2. 如果多个证据共同构成答案，请综合这些证据回答。
3. 如果证据不足，请明确说明“当前证据不足”。
4. 不要编造 evidence_paths 中不存在的数据。

用户问题：
{question}

evidence_paths:
{evidence}
"""
        response = self.llm.invoke([HumanMessage(content=prompt)])
        return str(getattr(response, "content", response)).strip()

    def _select_batch_ids(
        self,
        question: str,
        records: list[TreeRecord],
        max_ids: int,
    ) -> list[int]:
        candidate_text = json.dumps(
            [
                {
                    "id": record.id,
                    "path_text": record.path_text,
                    "leaf_text": record.leaf_text,
                }
                for record in records
            ],
            ensure_ascii=False,
            default=str,
        )
        prompt = f"""
你是一个表格证据检索助手。请从候选路径中选择最能回答用户问题的证据。

规则：
1. 只输出 JSON 对象，不要输出解释。
2. selected_ids 最多 {max_ids} 个。
3. 如果需要多个字段共同回答问题，可以选择多个 id。
4. 如果候选都无关，selected_ids 返回空数组。

输出格式：
{{"selected_ids": [1, 2, 3]}}

用户问题：
{question}

候选路径：
{candidate_text}
"""
        response = self.llm.invoke([HumanMessage(content=prompt)])
        content = str(getattr(response, "content", response)).strip()
        try:
            payload = json.loads(_extract_json_object(content))
            selected = payload.get("selected_ids", [])
            return [int(item) for item in selected if isinstance(item, (int, str))]
        except Exception:
            return []

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        response = self.embedding_client.embeddings.create(
            model=self.embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    @staticmethod
    def _build_llm(settings: Settings) -> ChatOpenAI | None:
        if not settings.llm_model or not settings.llm_api_key:
            return None
        kwargs: dict[str, Any] = {
            "model": settings.llm_model,
            "api_key": settings.llm_api_key,
            "temperature": settings.llm_temperature,
            "timeout": settings.llm_timeout_seconds,
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        return ChatOpenAI(**kwargs)

    @staticmethod
    def _build_embedding_client(settings: Settings) -> OpenAI | None:
        if not settings.embedding_model:
            return None
        api_key = settings.embedding_api_key or settings.llm_api_key
        base_url = settings.embedding_base_url or settings.llm_base_url
        if not api_key:
            return None
        return OpenAI(api_key=api_key, base_url=base_url or None)


def lexical_retrieve(
    question: str,
    records: list[TreeRecord],
    limit: int,
) -> list[TreeRecord]:
    normalized_question = _normalize_text(question)
    terms = _extract_question_terms(normalized_question)
    scored: list[tuple[int, TreeRecord]] = []

    for record in records:
        search_text = _normalize_text(record.search_text)
        score = 0
        for path_part in record.path:
            normalized_part = _normalize_text(path_part)
            suffix = _normalize_text(path_part.split(" - ", 1)[-1])
            if normalized_part and normalized_part in normalized_question:
                score += 8
            if suffix and suffix in normalized_question:
                score += 6
        if isinstance(record.data, dict):
            for key in record.data.keys():
                normalized_key = _normalize_text(str(key))
                if normalized_key and normalized_key in normalized_question:
                    score += 5
        for term in terms:
            if term in search_text:
                score += 2
        if score > 0:
            scored.append((score, record))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [record for _, record in scored[:limit]]


def _append_record(records: list[TreeRecord], path: list[str], data: Any) -> None:
    path_text = " | ".join(path)
    leaf_text = _leaf_to_text(data)
    records.append(
        TreeRecord(
            id=len(records),
            path=path,
            path_text=path_text,
            data=data,
            leaf_text=leaf_text,
            search_text=f"路径：{path_text}\n数据：{leaf_text}",
        )
    )


def _leaf_to_text(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{key}={item}" for key, item in value.items())
    return str(value)


def _record_to_dict(record: TreeRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "path": record.path,
        "path_text": record.path_text,
        "data": record.data,
        "leaf_text": record.leaf_text,
    }


def _format_retrieval_answer(candidates: list[TreeRecord]) -> str:
    lines = ["找到以下相关路径："]
    for record in candidates[:5]:
        lines.append(f"- {record.path_text}: {record.leaf_text}")
    return "\n".join(lines)


def _dedupe_records(records: list[TreeRecord]) -> list[TreeRecord]:
    seen: set[int] = set()
    deduped: list[TreeRecord] = []
    for record in records:
        if record.id in seen:
            continue
        seen.add(record.id)
        deduped.append(record)
    return deduped


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _extract_question_terms(question: str) -> list[str]:
    terms = re.split(r"[\s,，。；;：:？?、（）()]+", question)
    return [term for term in terms if len(term) >= 2]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower())


def _extract_json_object(text: str) -> str:
    if "```json" in text:
        return text.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in text:
        return text.split("```", 1)[1].split("```", 1)[0].strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("No JSON object found")
    return text[start:end]
