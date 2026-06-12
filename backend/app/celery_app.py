from __future__ import annotations

from app.core.settings import get_settings

try:
    from celery import Celery
except ImportError:  # Allows the FastAPI app to show a clear runtime error before deps are installed.
    Celery = None  # type: ignore[assignment]


settings = get_settings()

if Celery is None:
    celery_app = None
else:
    celery_app = Celery(
        "excel_data_assistant",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=["app.tasks.table_pipeline_tasks"],
    )
    celery_app.conf.update(
        task_track_started=True,
        result_extended=True,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="Asia/Shanghai",
        enable_utc=True,
        worker_prefetch_multiplier=1,
        task_acks_late=True,
    )
