from __future__ import annotations

import io
import logging
from json import JSONDecodeError
from decimal import Decimal
from datetime import date, datetime, time
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from openai import APITimeoutError
from openpyxl import load_workbook

from app.core.settings import Settings, get_settings
from app.services.table2tree_enhanced import LangChainEnhancedTableParser

router = APIRouter(prefix="/table2tree", tags=["table2tree"])
logger = logging.getLogger(__name__)


@router.post("/enhanced")
async def parse_table_enhanced(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
):
    filename = file.filename or ""
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=400,
            detail="Enhanced table parsing currently supports .xlsx and .xlsm files.",
        )

    try:
        content = await file.read()
        workbook = load_workbook(io.BytesIO(content), data_only=True)
        sheet = workbook.active
        parser = LangChainEnhancedTableParser(settings)
        result = parser.parse_sheet(sheet)
    except APITimeoutError as exc:
        logger.exception("Enhanced table parsing timed out")
        raise HTTPException(
            status_code=504,
            detail=(
                "LLM request timed out while parsing the table. "
                "Increase LLM_TIMEOUT_SECONDS or try a smaller sheet."
            ),
        ) from exc
    except JSONDecodeError as exc:
        logger.exception("Enhanced table parsing returned invalid JSON")
        raise HTTPException(
            status_code=422,
            detail=(
                "The LLM returned invalid JSON for the final tree. "
                f"{exc.msg} at line {exc.lineno}, column {exc.colno}. "
                f"Nearby output: {_json_error_snippet(exc)}"
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Enhanced table parsing failed")
        raise HTTPException(
            status_code=500,
            detail=f"{exc.__class__.__name__}: {exc}",
        ) from exc

    return {
        "filename": filename,
        "sheet_name": sheet.title,
        "table_title": result.table_title,
        "mode": "enhanced",
        "markdown_table": result.markdown_table,
        "normalized_headers": result.normalized_headers,
        "hierarchy_definition": result.hierarchy_definition,
        "summary_text": result.summary_text,
        "final_json_tree": result.final_json_tree,
        "tree_with_cell_refs": _json_safe(result.tree_with_cell_refs),
        "tree": _json_safe(result.tree),
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


def _json_error_snippet(exc: JSONDecodeError, radius: int = 300) -> str:
    start = max(0, exc.pos - radius)
    end = min(len(exc.doc), exc.pos + radius)
    return exc.doc[start:end].replace("\n", "\\n")
