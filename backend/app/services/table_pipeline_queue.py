from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.core.settings import Settings
from app.services.table_object_storage import TableObjectStorage


class TablePipelineQueue:
    def __init__(self, settings: Settings):
        self.settings = settings

    def submit(
        self,
        filename: str,
        content: bytes,
        sheet_name: str | None = None,
    ) -> dict[str, Any]:
        celery_app = _require_celery_app()
        table_id = uuid4().hex
        storage = TableObjectStorage(self.settings)
        source_object = storage.store_source_file(table_id, filename, content)
        task = celery_app.send_task(
            "table_pipeline.ingest",
            kwargs={
                "table_id": table_id,
                "filename": filename,
                "source_object": source_object,
                "sheet_name": sheet_name,
            },
            task_id=table_id,
        )
        return {
            "job_id": task.id,
            "table_id": table_id,
            "filename": filename,
            "sheet_name": sheet_name,
            "status": "queued",
            "submitted_at": _now(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result": None,
            "queue_size": 0,
            "source_object": source_object,
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        celery_app = _require_celery_app()
        async_result = celery_app.AsyncResult(job_id)
        state = async_result.state
        info = async_result.info if isinstance(async_result.info, dict) else {}
        result = async_result.result if state == "SUCCESS" and isinstance(async_result.result, dict) else None
        table_id = _first_present(
            info.get("table_id"),
            result.get("table_id") if result else None,
            job_id,
        )
        filename = _first_present(
            info.get("filename"),
            result.get("filename") if result else None,
            "",
        )
        sheet_name = _first_present(
            info.get("sheet_name"),
            result.get("sheet_name") if result else None,
            None,
        )
        return {
            "job_id": job_id,
            "table_id": table_id,
            "filename": filename,
            "sheet_name": sheet_name,
            "status": _map_celery_state(state),
            "celery_state": state,
            "step": info.get("step"),
            "submitted_at": None,
            "started_at": None,
            "finished_at": _now() if state in {"SUCCESS", "FAILURE", "REVOKED"} else None,
            "error": _format_error(async_result) if state == "FAILURE" else None,
            "result": result,
            "queue_size": 0,
        }

    def get_job_by_table_id(self, table_id: str) -> dict[str, Any]:
        return self.get_job(table_id)

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        celery_app = _require_celery_app()
        inspector = celery_app.control.inspect(timeout=1)
        raw_jobs: list[dict[str, Any]] = []
        for state_name, fetch in [
            ("running", inspector.active),
            ("queued", inspector.reserved),
            ("scheduled", inspector.scheduled),
        ]:
            try:
                worker_payload = fetch() or {}
            except Exception:
                worker_payload = {}
            for worker_name, tasks in worker_payload.items():
                for task in tasks or []:
                    raw_jobs.append(_inspect_task_to_job(task, worker_name, state_name))
                    if len(raw_jobs) >= limit:
                        return raw_jobs
        return raw_jobs


def _require_celery_app() -> Any:
    try:
        from app.celery_app import celery_app
    except ImportError as exc:
        raise RuntimeError(
            "Celery is not installed. Run: pip install -r backend/requirements.txt"
        ) from exc
    if celery_app is None:
        raise RuntimeError(
            "Celery is not installed. Run: pip install -r backend/requirements.txt"
        )
    return celery_app


def _map_celery_state(state: str) -> str:
    if state == "SUCCESS":
        return "completed"
    if state in {"FAILURE", "REVOKED"}:
        return "failed"
    if state in {"STARTED", "PROGRESS", "RETRY"}:
        return "running"
    return "queued"


def _format_error(async_result: Any) -> str:
    result = async_result.result
    if result is None:
        return "Task failed"
    return f"{result.__class__.__name__}: {result}" if isinstance(result, BaseException) else str(result)


def _inspect_task_to_job(task: dict[str, Any], worker_name: str, status: str) -> dict[str, Any]:
    task_id = task.get("id") or task.get("request", {}).get("id")
    kwargs = task.get("kwargs") or {}
    if isinstance(kwargs, str):
        kwargs = {}
    return {
        "job_id": task_id,
        "table_id": kwargs.get("table_id") or task_id,
        "filename": kwargs.get("filename") or "",
        "sheet_name": kwargs.get("sheet_name"),
        "status": status,
        "worker": worker_name,
        "submitted_at": None,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "result": None,
        "queue_size": 0,
    }


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat()
