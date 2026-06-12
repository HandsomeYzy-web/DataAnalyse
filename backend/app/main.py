from fastapi import FastAPI

from app.api.table_pipeline import router as table_pipeline_router
from app.api.table_parse_plan import router as table_parse_plan_router
from app.api.table_qa import router as table_qa_router
from app.api.table2tree import router as table2tree_router
from app.core.settings import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name)
app.include_router(table2tree_router, prefix="/api")
app.include_router(table_parse_plan_router, prefix="/api")
app.include_router(table_qa_router, prefix="/api")
app.include_router(table_pipeline_router, prefix="/api")


@app.get("/health")
def health_check():
    return {"status": "ok"}
