from __future__ import annotations

import io
import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage
from openpyxl import load_workbook

from app.core.settings import Settings
from app.services.table2tree_enhanced import LangChainEnhancedTableParser
from app.services.table_file_converter import convert_table_file_to_xlsx
from app.services.table_object_storage import TableObjectStorage
from app.services.table_qa import TableQAService
from app.services.table_search_index import TableSearchIndex, build_table_summary


class TablePipelineService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = TableObjectStorage(settings)
        self.index = TableSearchIndex(settings)

    def ingest_table(
        self,
        filename: str,
        content: bytes,
        sheet_name: str | None = None,
        table_id: str | None = None,
    ) -> dict[str, Any]:
        converted = convert_table_file_to_xlsx(filename, content)
        workbook = load_workbook(io.BytesIO(converted.xlsx_content), data_only=True)
        if sheet_name:
            if sheet_name not in workbook.sheetnames:
                raise ValueError(f"Sheet not found: {sheet_name}")
            sheet = workbook[sheet_name]
        else:
            sheet = workbook.active

        parser = LangChainEnhancedTableParser(self.settings)
        parse_result = parser.parse_sheet(sheet)
        table_id = table_id or uuid4().hex
        table_name = _table_display_name(parse_result.table_title, sheet.title, converted.original_filename)
        tree_with_name = _attach_table_name(parse_result.tree, table_name)
        tree_refs_with_name = _attach_table_name(parse_result.tree_with_cell_refs, table_name)

        artifact = _json_safe(
            {
                "table_id": table_id,
                "filename": converted.original_filename,
                "normalized_filename": converted.normalized_filename,
                "source_extension": converted.source_extension,
                "sheet_name": sheet.title,
                "table_title": parse_result.table_title,
                "markdown_table": parse_result.markdown_table,
                "normalized_headers": parse_result.normalized_headers,
                "hierarchy_definition": parse_result.hierarchy_definition,
                "summary_text": parse_result.summary_text,
                "final_json_tree": parse_result.final_json_tree,
                "tree_with_cell_refs": tree_refs_with_name,
                "tree": tree_with_name,
            }
        )

        stored = self.storage.store_table_artifacts(
            table_id=table_id,
            source_filename=converted.original_filename,
            source_content=converted.source_content,
            normalized_filename=converted.normalized_filename,
            xlsx_content=converted.xlsx_content,
            artifact=artifact,
        )

        summary = build_table_summary(
            normalized_headers=parse_result.normalized_headers,
            hierarchy_definition=parse_result.hierarchy_definition,
            summary_text=parse_result.summary_text,
        )
        index_document = {
            "table_id": table_id,
            "filename": converted.original_filename,
            "normalized_filename": converted.normalized_filename,
            "source_extension": converted.source_extension,
            "sheet_name": sheet.title,
            "table_title": parse_result.table_title,
            "normalized_headers": parse_result.normalized_headers,
            "hierarchy_definition": parse_result.hierarchy_definition,
            "source_object": stored.source_object,
            "xlsx_object": stored.xlsx_object,
            "tree_object": stored.tree_object,
            **summary,
        }
        indexed_document = self.index.index_table(index_document)

        return _json_safe(
            {
                "table_id": table_id,
                "filename": converted.original_filename,
                "normalized_filename": converted.normalized_filename,
                "sheet_name": sheet.title,
                "table_title": parse_result.table_title,
                "summary_text": summary["summary_text"],
                "candidate_fields": summary["candidate_fields"],
                "minio_objects": {
                    "source_object": stored.source_object,
                    "xlsx_object": stored.xlsx_object,
                    "tree_object": stored.tree_object,
                },
                "indexed": True,
                "indexed_vector": bool(indexed_document.get("summary_vector")),
                "tree": tree_with_name,
            }
        )

    def answer_question(
        self,
        question: str,
        top_k: int = 3,
        evidence_limit: int = 12,
        use_llm: bool = True,
    ) -> dict[str, Any]:
        candidates = self.index.search(question, top_k=top_k)
        if not candidates:
            return {
                "answer": "当前未检索到相关表格，无法回答问题。",
                "mode": "pipeline_no_table_candidates",
                "table_candidates": [],
                "table_answers": [],
            }

        qa_service = TableQAService(self.settings)
        table_answers: list[dict[str, Any]] = []

        for candidate in candidates:
            try:
                artifact = self.storage.get_json(candidate["tree_object"])
                metadata = {
                    "table_id": artifact.get("table_id"),
                    "filename": artifact.get("filename"),
                    "sheet_name": artifact.get("sheet_name"),
                    "table_title": artifact.get("table_title"),
                }
                qa_result = qa_service.answer(
                    question=question,
                    tree=artifact.get("tree") or {},
                    metadata=metadata,
                    limit=evidence_limit,
                    use_llm=use_llm,
                )
                table_answers.append(
                    {
                        "table": {
                            "table_id": candidate.get("table_id"),
                            "filename": candidate.get("filename"),
                            "sheet_name": candidate.get("sheet_name"),
                            "table_title": candidate.get("table_title"),
                            "score": candidate.get("score"),
                        },
                        "qa": qa_result,
                    }
                )
            except Exception as exc:
                table_answers.append(
                    {
                        "table": {
                            "table_id": candidate.get("table_id"),
                            "filename": candidate.get("filename"),
                            "sheet_name": candidate.get("sheet_name"),
                            "table_title": candidate.get("table_title"),
                            "score": candidate.get("score"),
                        },
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )

        answer = _synthesize_pipeline_answer(
            question=question,
            table_answers=table_answers,
            llm=qa_service.llm if use_llm else None,
        )
        return _json_safe(
            {
                "answer": answer,
                "mode": "es_minio_tree_qa",
                "table_candidates": _trim_candidate_payload(candidates),
                "table_answers": table_answers,
            }
        )

    def get_table_artifact(self, table_id: str) -> dict[str, Any]:
        return self.storage.get_json(self.storage.tree_object_name(table_id))

    def get_table_summary(self, table_id: str) -> dict[str, Any]:
        artifact = self.get_table_artifact(table_id)
        document = self.index.get_table_document(table_id)
        return _json_safe(
            {
                "table_id": table_id,
                "filename": artifact.get("filename"),
                "normalized_filename": artifact.get("normalized_filename"),
                "source_extension": artifact.get("source_extension"),
                "sheet_name": artifact.get("sheet_name"),
                "table_title": artifact.get("table_title"),
                "minio_objects": artifact.get("minio_objects"),
                "summary_text": (document or {}).get("summary_text"),
                "candidate_fields": (document or {}).get("candidate_fields", []),
                "indexed": document is not None,
            }
        )

    def list_table_summaries(self, limit: int = 50) -> list[dict[str, Any]]:
        tables: list[dict[str, Any]] = []
        for document in self.index.list_tables(limit=limit):
            tables.append(
                {
                    "table_id": document.get("table_id"),
                    "filename": document.get("filename"),
                    "normalized_filename": document.get("normalized_filename"),
                    "source_extension": document.get("source_extension"),
                    "sheet_name": document.get("sheet_name"),
                    "table_title": document.get("table_title"),
                    "summary_text": document.get("summary_text"),
                    "candidate_fields": document.get("candidate_fields", []),
                    "created_at": document.get("created_at"),
                    "indexed": True,
                    "minio_objects": {
                        "source_object": document.get("source_object"),
                        "xlsx_object": document.get("xlsx_object"),
                        "tree_object": document.get("tree_object"),
                    },
                }
            )
        return _json_safe(tables)

    def get_table_tree(self, table_id: str) -> dict[str, Any]:
        artifact = self.get_table_artifact(table_id)
        table_name = _table_display_name(
            artifact.get("table_title"),
            artifact.get("sheet_name"),
            artifact.get("filename"),
        )
        return _json_safe(
            {
                "table_id": table_id,
                "filename": artifact.get("filename"),
                "sheet_name": artifact.get("sheet_name"),
                "table_title": artifact.get("table_title"),
                "tree": _attach_table_name(artifact.get("tree") or {}, table_name),
                "tree_with_cell_refs": _attach_table_name(
                    artifact.get("tree_with_cell_refs") or {},
                    table_name,
                ),
            }
        )

    def get_table_file(self, table_id: str, kind: str = "source") -> tuple[str, str, bytes]:
        artifact = self.get_table_artifact(table_id)
        objects = artifact.get("minio_objects") or {}
        if kind == "source":
            object_name = objects.get("source_object") or _lookup_object_from_index(
                self.index,
                table_id,
                "source_object",
            )
            filename = artifact.get("filename") or f"{table_id}-source"
            media_type = "application/octet-stream"
        elif kind == "normalized":
            object_name = objects.get("xlsx_object") or _lookup_object_from_index(
                self.index,
                table_id,
                "xlsx_object",
            )
            filename = artifact.get("normalized_filename") or f"{table_id}.xlsx"
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            raise ValueError(f"Unsupported table file kind: {kind}")

        if not object_name:
            raise ValueError(f"Cannot find MinIO object for table {table_id}: {kind}")
        return str(filename), media_type, self.storage.get_bytes(str(object_name))


def _synthesize_pipeline_answer(
    question: str,
    table_answers: list[dict[str, Any]],
    llm: Any | None,
) -> str:
    compact_answers = [
        {
            "table": item.get("table"),
            "answer": item.get("qa", {}).get("answer"),
            "evidence_paths": item.get("qa", {}).get("evidence_paths", []),
            "error": item.get("error"),
        }
        for item in table_answers
    ]
    if llm is None:
        lines = ["检索到的表格问答结果："]
        for item in compact_answers:
            table = item.get("table") or {}
            title = table.get("table_title") or table.get("filename") or table.get("table_id")
            lines.append(f"- {title}: {item.get('answer') or item.get('error')}")
        return "\n".join(lines)

    prompt = f"""
你是一个跨表格问答助手。请只根据 table_answers 中的答案和 evidence_paths 综合回答用户问题。

要求：
1. 不要使用外部知识。
2. 如果证据足够，直接回答，并说明来自哪些表格。
3. 如果多个表格给出互补证据，请合并回答。
4. 如果候选表格都没有足够证据，请说明“当前证据不足”。

用户问题：
{question}

table_answers:
{json.dumps(compact_answers, ensure_ascii=False, indent=2, default=str)}
"""
    response = llm.invoke([HumanMessage(content=prompt)])
    return str(getattr(response, "content", response)).strip()


def _trim_candidate_payload(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        result.append(
            {
                "score": candidate.get("score"),
                "table_id": candidate.get("table_id"),
                "filename": candidate.get("filename"),
                "sheet_name": candidate.get("sheet_name"),
                "table_title": candidate.get("table_title"),
                "tree_object": candidate.get("tree_object"),
            }
        )
    return result


def _lookup_object_from_index(index: TableSearchIndex, table_id: str, field_name: str) -> Any:
    document = index.get_table_document(table_id)
    if not document:
        return None
    return document.get(field_name)


def _table_display_name(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return "未命名表格"


def _attach_table_name(tree: Any, table_name: str) -> dict[str, Any]:
    if isinstance(tree, dict):
        if tree.get("表格名") == table_name:
            return tree
        return {
            "表格名": table_name,
            **{key: value for key, value in tree.items() if key != "表格名"},
        }
    return {
        "表格名": table_name,
        "数据": tree,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
