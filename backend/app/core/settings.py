from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Excel Data Assistant"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.2
    llm_timeout_seconds: int = 300
    large_table_cell_threshold: int = 800
    large_table_row_threshold: int = 80
    large_table_column_threshold: int = 30
    large_table_merged_cell_threshold: int = 20
    large_table_markdown_threshold: int = 30000
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = 0
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "table-summaries"
    elasticsearch_verify_certs: bool = True
    elasticsearch_api_key: str = ""
    elasticsearch_username: str = ""
    elasticsearch_password: str = ""
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "excel-files"
    minio_secure: bool = False
    table_artifact_prefix: str = "table-artifacts"
    table_pipeline_workers: int = 1
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
