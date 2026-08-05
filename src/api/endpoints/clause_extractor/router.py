import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from src.services.clause_extractor.clause_builder import cached_build_clause_tree
from src.services.clause_extractor.doc_parser import parse_docx

router = APIRouter()


@router.post("/clauses")
async def extract_clauses(file: UploadFile = File(...)) -> Any:  # noqa: B008

    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only .docx files are supported")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / file.filename

        # Save the upload to disk — python-docx needs a real file path/stream.
        with tmp_path.open("wb") as out_f:
            shutil.copyfileobj(file.file, out_f)
        await file.close()

        try:
            paras = await run_in_threadpool(parse_docx, str(tmp_path))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to parse docx: {exc}") from exc

        if not paras:
            raise HTTPException(status_code=422, detail="No text paragraphs found in document")

        para_dicts = [asdict(p) for p in paras]

        try:
            result = await cached_build_clause_tree(para_dicts)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Clause classification failed: {exc}") from exc

    return result
