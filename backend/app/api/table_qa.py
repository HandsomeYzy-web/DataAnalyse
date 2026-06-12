from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.settings import Settings, get_settings
from app.services.table_qa import TableQAService

router = APIRouter(prefix="/table-qa", tags=["table-qa"])
logger = logging.getLogger(__name__)


class TableQARequest(BaseModel):
    question: str = Field(min_length=1)
    tree: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=12, ge=1, le=50)
    use_llm: bool = True


@router.post("/answer")
def answer_table_question(
    request: TableQARequest,
    settings: Settings = Depends(get_settings),
):
    try:
        service = TableQAService(settings)
        return service.answer(
            question=request.question,
            tree=request.tree,
            metadata=request.metadata,
            limit=request.limit,
            use_llm=request.use_llm,
        )
    except Exception as exc:
        logger.exception("Table QA failed")
        raise HTTPException(
            status_code=500,
            detail=f"{exc.__class__.__name__}: {exc}",
        ) from exc
