from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from typing import Any

from minio import Minio
from minio.datatypes import Object

from app.core.settings import Settings


@dataclass
class StoredTableArtifacts:
    source_object: str
    xlsx_object: str
    tree_object: str


class TableObjectStorage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bucket = settings.minio_bucket
        self.client = Minio(
            _normalize_minio_endpoint(settings.minio_endpoint),
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    def store_table_artifacts(
        self,
        table_id: str,
        batch_id: str,
        source_filename: str,
        normalized_filename: str,
        xlsx_content: bytes,
        artifact: dict[str, Any],
        source_content: bytes | None = None,
        source_object: str | None = None,
    ) -> StoredTableArtifacts:
        self.ensure_bucket()
        prefix = self._sheet_prefix(batch_id, table_id)
        if source_object is None:
            if source_content is None:
                raise ValueError("source_content is required when source_object is not provided")
            source_object = self.source_object_name(batch_id, source_filename)
        xlsx_object = f"{prefix}/normalized/{_safe_object_name(normalized_filename)}"
        tree_object = f"{prefix}/tree.json"
        artifact_with_objects = {
            **artifact,
            "minio_objects": {
                "source_object": source_object,
                "xlsx_object": xlsx_object,
                "tree_object": tree_object,
            },
        }

        if source_content is not None:
            self.put_bytes(source_object, source_content, "application/octet-stream")
        self.put_bytes(
            xlsx_object,
            xlsx_content,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.put_json(tree_object, artifact_with_objects)
        return StoredTableArtifacts(
            source_object=source_object,
            xlsx_object=xlsx_object,
            tree_object=tree_object,
        )

    def store_source_file(
        self,
        batch_id: str,
        source_filename: str,
        source_content: bytes,
    ) -> str:
        self.ensure_bucket()
        object_name = self.source_object_name(batch_id, source_filename)
        self.put_bytes(object_name, source_content, "application/octet-stream")
        return object_name

    def ensure_bucket(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def put_bytes(self, object_name: str, content: bytes, content_type: str) -> None:
        self.client.put_object(
            self.bucket,
            object_name,
            io.BytesIO(content),
            length=len(content),
            content_type=content_type,
        )

    def put_json(self, object_name: str, payload: dict[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.put_bytes(object_name, content, "application/json; charset=utf-8")

    def get_json(self, object_name: str) -> dict[str, Any]:
        response = self.client.get_object(self.bucket, object_name)
        try:
            content = response.read().decode("utf-8")
            payload = json.loads(content)
        finally:
            response.close()
            response.release_conn()
        if not isinstance(payload, dict):
            raise ValueError(f"MinIO object is not a JSON object: {object_name}")
        return payload

    def get_bytes(self, object_name: str) -> bytes:
        response = self.client.get_object(self.bucket, object_name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def stat_object(self, object_name: str) -> Object:
        return self.client.stat_object(self.bucket, object_name)

    def tree_object_name(self, table_id: str) -> str:
        return f"{self._legacy_table_prefix(table_id)}/tree.json"

    def source_object_name(self, batch_id: str, source_filename: str) -> str:
        return f"{self._batch_prefix(batch_id)}/source/{_safe_object_name(source_filename)}"

    def _batch_prefix(self, batch_id: str) -> str:
        prefix = self.settings.table_artifact_prefix.strip().strip("/")
        return f"{prefix}/{batch_id}" if prefix else batch_id

    def _sheet_prefix(self, batch_id: str, table_id: str) -> str:
        return f"{self._batch_prefix(batch_id)}/sheets/{table_id}"

    def _legacy_table_prefix(self, table_id: str) -> str:
        prefix = self.settings.table_artifact_prefix.strip().strip("/")
        return f"{prefix}/{table_id}" if prefix else table_id


def _normalize_minio_endpoint(endpoint: str) -> str:
    return endpoint.replace("https://", "").replace("http://", "").strip("/")


def _safe_object_name(filename: str) -> str:
    cleaned = re.sub(r"[^\w.\-()\u4e00-\u9fff]+", "_", filename.strip())
    return cleaned or "uploaded-file"
