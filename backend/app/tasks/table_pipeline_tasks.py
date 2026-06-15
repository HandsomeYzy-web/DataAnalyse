from __future__ import annotations

from typing import Any

from app.celery_app import celery_app
from app.core.settings import get_settings
from app.services.table_object_storage import TableObjectStorage
from app.services.table_pipeline import TablePipelineService


if celery_app is not None:

    @celery_app.task(name="table_pipeline.ingest", bind=True)
    def ingest_table_task(
        self: Any,
        table_id: str,
        filename: str,
        source_object: str,
        sheet_name: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        storage = TableObjectStorage(settings)

        self.update_state(
            state="PROGRESS",
            meta=_task_meta(table_id, batch_id, filename, sheet_name, "loading_source"),
        )
        source_content = storage.get_bytes(source_object)

        self.update_state(
            state="PROGRESS",
            meta=_task_meta(table_id, batch_id, filename, sheet_name, "parsing_and_indexing"),
        )
        service = TablePipelineService(settings)
        result = service.ingest_table(
            filename=filename,
            content=source_content,
            sheet_name=sheet_name,
            table_id=table_id,
            batch_id=batch_id,
            source_object=source_object,
        )
        return _compact_ingest_result(result)


def _task_meta(
    table_id: str,
    batch_id: str | None,
    filename: str,
    sheet_name: str | None,
    step: str,
) -> dict[str, Any]:
    return {
        "table_id": table_id,
        "batch_id": batch_id,
        "filename": filename,
        "sheet_name": sheet_name,
        "step": step,
    }


def _compact_ingest_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {key: value for key, value in result.items() if key != "tree"}
    table_id = compact.get("table_id")
    if table_id:
        compact["links"] = {
            "summary": f"/api/table-pipeline/tables/{table_id}",
            "tree": f"/api/table-pipeline/tables/{table_id}/tree",
            "source": f"/api/table-pipeline/tables/{table_id}/source",
            "normalized": f"/api/table-pipeline/tables/{table_id}/normalized",
        }
    return compact
