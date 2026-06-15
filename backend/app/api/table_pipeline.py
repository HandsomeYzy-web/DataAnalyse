from __future__ import annotations

import logging
from urllib.parse import quote

from elasticsearch.exceptions import (
    ApiError,
    ConnectionError as ElasticsearchConnectionError,
    SerializationError,
    TransportError,
    UnsupportedProductError,
)
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from minio.error import S3Error
from pydantic import BaseModel, Field

from app.core.settings import Settings, get_settings
from app.services.table_pipeline import TablePipelineService
from app.services.table_pipeline_queue import TablePipelineQueue

router = APIRouter(prefix="/table-pipeline", tags=["table-pipeline"])
logger = logging.getLogger(__name__)
ELASTICSEARCH_ERRORS = (
    ApiError,
    ElasticsearchConnectionError,
    SerializationError,
    TransportError,
    UnsupportedProductError,
)


class PipelineQuestionRequest(BaseModel):
    question: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=10)
    evidence_limit: int = Field(default=12, ge=1, le=50)
    use_llm: bool = True


def get_pipeline_queue(settings: Settings = Depends(get_settings)) -> TablePipelineQueue:
    return TablePipelineQueue(settings)


@router.post("/upload", status_code=202)
async def upload_table_to_pipeline(
    file: UploadFile = File(...),
    sheet_name: str | None = Query(None),
    queue: TablePipelineQueue = Depends(get_pipeline_queue),
):
    filename = file.filename or ""
    if not filename:
        raise HTTPException(status_code=400, detail="Uploaded file must have a filename.")

    try:
        content = await file.read()
        return queue.submit_batch(filename=filename, content=content, sheet_name=sheet_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Pipeline enqueue failed")
        raise HTTPException(
            status_code=500,
            detail=f"{exc.__class__.__name__}: {exc}",
        ) from exc


@router.get("/jobs")
def list_pipeline_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    queue: TablePipelineQueue = Depends(get_pipeline_queue),
):
    try:
        return {"jobs": queue.list_jobs(limit=limit)}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/jobs/by-table/{table_id}")
def get_pipeline_job_by_table_id(
    table_id: str,
    queue: TablePipelineQueue = Depends(get_pipeline_queue),
):
    try:
        return queue.get_job_by_table_id(table_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Job not found for table: {table_id}") from exc


@router.get("/jobs/{job_id}")
def get_pipeline_job(
    job_id: str,
    queue: TablePipelineQueue = Depends(get_pipeline_queue),
):
    try:
        return queue.get_job(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}") from exc


@router.get("/tables")
def list_table_summaries(
    limit: int = Query(default=50, ge=1, le=200),
    settings: Settings = Depends(get_settings),
):
    try:
        service = TablePipelineService(settings)
        return {"tables": service.list_table_summaries(limit=limit)}
    except ELASTICSEARCH_ERRORS as exc:
        logger.exception("Pipeline Elasticsearch list failed")
        raise HTTPException(status_code=502, detail=f"Elasticsearch list failed: {exc}") from exc


@router.get("/tables/{table_id}")
def get_table_summary(
    table_id: str,
    settings: Settings = Depends(get_settings),
):
    try:
        service = TablePipelineService(settings)
        return service.get_table_summary(table_id)
    except S3Error as exc:
        _raise_minio_http(exc)
    except ELASTICSEARCH_ERRORS as exc:
        logger.exception("Pipeline Elasticsearch read failed")
        raise HTTPException(status_code=502, detail=f"Elasticsearch read failed: {exc}") from exc


@router.get("/tables/{table_id}/artifact")
def get_table_artifact(
    table_id: str,
    settings: Settings = Depends(get_settings),
):
    try:
        service = TablePipelineService(settings)
        return service.get_table_artifact(table_id)
    except S3Error as exc:
        _raise_minio_http(exc)
    except ELASTICSEARCH_ERRORS as exc:
        logger.exception("Pipeline Elasticsearch read failed")
        raise HTTPException(status_code=502, detail=f"Elasticsearch read failed: {exc}") from exc


@router.get("/tables/{table_id}/tree")
def get_table_tree(
    table_id: str,
    settings: Settings = Depends(get_settings),
):
    try:
        service = TablePipelineService(settings)
        return service.get_table_tree(table_id)
    except S3Error as exc:
        _raise_minio_http(exc)
    except ELASTICSEARCH_ERRORS as exc:
        logger.exception("Pipeline Elasticsearch read failed")
        raise HTTPException(status_code=502, detail=f"Elasticsearch read failed: {exc}") from exc


@router.get("/tables/{table_id}/source")
def download_table_source(
    table_id: str,
    settings: Settings = Depends(get_settings),
):
    return _download_table_file(table_id=table_id, kind="source", settings=settings)


@router.get("/tables/{table_id}/normalized")
def download_table_normalized_xlsx(
    table_id: str,
    settings: Settings = Depends(get_settings),
):
    return _download_table_file(table_id=table_id, kind="normalized", settings=settings)


@router.post("/answer")
def answer_from_pipeline(
    request: PipelineQuestionRequest,
    settings: Settings = Depends(get_settings),
):
    try:
        service = TablePipelineService(settings)
        return service.answer_question(
            question=request.question,
            top_k=request.top_k,
            evidence_limit=request.evidence_limit,
            use_llm=request.use_llm,
        )
    except ELASTICSEARCH_ERRORS as exc:
        logger.exception("Pipeline Elasticsearch search failed")
        raise HTTPException(
            status_code=502,
            detail=f"Elasticsearch search failed: {exc}",
        ) from exc
    except S3Error as exc:
        logger.exception("Pipeline MinIO read failed")
        raise HTTPException(
            status_code=502,
            detail=f"MinIO read failed: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("Pipeline QA failed")
        raise HTTPException(
            status_code=500,
            detail=f"{exc.__class__.__name__}: {exc}",
        ) from exc


def _download_table_file(table_id: str, kind: str, settings: Settings) -> Response:
    try:
        service = TablePipelineService(settings)
        filename, media_type, content = service.get_table_file(table_id, kind=kind)
        quoted_filename = quote(filename)
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quoted_filename}"
                )
            },
        )
    except S3Error as exc:
        _raise_minio_http(exc)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ELASTICSEARCH_ERRORS as exc:
        logger.exception("Pipeline Elasticsearch read failed")
        raise HTTPException(status_code=502, detail=f"Elasticsearch read failed: {exc}") from exc


def _raise_minio_http(exc: S3Error) -> None:
    status_code = 404 if exc.code in {"NoSuchKey", "NoSuchBucket", "NoSuchObject"} else 502
    raise HTTPException(status_code=status_code, detail=f"MinIO operation failed: {exc}") from exc
