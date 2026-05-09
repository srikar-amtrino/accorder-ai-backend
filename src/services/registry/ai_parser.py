import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from docx.document import Document

from src.config.logging import Logger
from src.config.settings import get_settings
from src.exceptions.parser_exceptions import (
    DocxCleaningException,
    DocxParagraphExtractionException,
)
from src.schemas.playbook_review import Clause
from src.schemas.registry import Chunk, ParseResult
from src.services.registry.base_parser import BaseParser
from src.services.session_manager import SessionData


class AIParser(BaseParser, Logger):
    """AI-based parser that uses LLM to extract clauses and chunk them."""

    def __init__(self) -> None:
        """Initialize the parser."""
        super().__init__()
        self.settings = get_settings()
        from src.dependencies import get_service_container

        self.service_container = get_service_container()
        self.llm_model = self.service_container.azure_openai_model
        self.embedding_service = self.service_container.embedding_service

    @staticmethod
    def _clean_text(text: str) -> str:
        """Clean the text to remove unwanted characters."""

        if not text or not text.strip():
            return ""

        # Replace special whitespace chars
        text = text.replace("\u00a0", " ")
        text = text.replace("\u200b", "")
        text = text.replace("\ufeff", "")
        text = text.replace("\r", "")
        text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", text)

        # Normalize whitespace
        text = re.sub(r"\s+", " ", text).strip()

        return text

    async def clean_document(self, document: Document) -> None:
        """Clean the document by removing trailing spaces and extra chars."""

        try:
            for paragraph in document.paragraphs:
                if paragraph.text:
                    paragraph.text = " ".join(paragraph.text.split())
        except Exception as e:
            raise DocxCleaningException(str(e)) from e

    async def _extract_text(self, document: Document) -> str:
        """Extract all text from the document — every paragraph, nothing skipped."""

        try:
            paragraphs = []
            for p in document.paragraphs:
                raw = p.text.strip()
                if raw:
                    cleaned = self._clean_text(raw)
                    if cleaned:
                        paragraphs.append(cleaned)

            full_text = "\n".join(paragraphs)

            self.logger.info(f"Total paragraphs: {len(paragraphs)} | Total chars: {len(full_text)}")

            return full_text
        except Exception as e:
            raise DocxParagraphExtractionException(str(e)) from e

    async def _extract_clauses(self, text: str) -> List[Clause]:
        """Use the LLM to extract clauses from the document text."""

        self.logger.info(f"Input text length (chars): {len(text)}")

        if not text.strip():
            self.logger.warning("Empty text — returning no clauses")
            return []

        # prompt = self._build_prompt(text)
        prompt = Path(r"src\services\prompts\v1\ai_parser_prompt.mustache").read_text(encoding="utf-8")
        prompt = prompt.replace("{{text}}", text)

        try:
            client = self.llm_model.client
            deployment = self.llm_model.deployment_name

            response = client.chat.completions.create(
                model=deployment,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=15000,
                temperature=0,
                response_format={"type": "json_object"},
            )

            finish_reason = response.choices[0].finish_reason
            completion_tokens = response.usage.completion_tokens
            prompt_tokens = response.usage.prompt_tokens
            self.logger.info(f"finish_reason={finish_reason} | " f"prompt_tokens={prompt_tokens} | " f"completion_tokens={completion_tokens}")

            if finish_reason == "length":
                self.logger.error("Output was TRUNCATED by token limit! " "Increase max_tokens or split the document.")

            raw_content = response.choices[0].message.content

            clean_content = re.sub(r"^```(?:json)?\s*", "", raw_content.strip())
            clean_content = re.sub(r"\s*```$", "", clean_content)

            data = json.loads(clean_content)
            clauses = [Clause(**c) for c in data.get("clauses", [])]

            self.logger.info(f"Clauses extracted: {len(clauses)}")
            # if clauses:
            #     self.logger.info(f"[_extract_clauses] First clause: '{clauses[0].title}'")
            #     self.logger.info(f"[_extract_clauses] Last clause:  '{clauses[-1].title}'")

            if len(clauses) < 15 and finish_reason != "length":
                self.logger.warning(f"Only {len(clauses)} clauses extracted — " "expected 15+. Running two-pass fallback.")
                clauses = await self._extract_clauses_two_pass(text)

            return clauses

        except json.JSONDecodeError as e:
            self.logger.error(f"JSON parse failed: {e}")
            return []
        except Exception as e:
            self.logger.error(f"API call failed: {e}")
            return []

    async def _extract_clauses_two_pass(self, text: str) -> List[Clause]:
        """split the document in half, extract each half independently, then merge and deduplicate by title."""

        self.logger.info("Starting two-pass extraction")

        mid = len(text) // 2
        # Find the nearest newline to avoid cutting mid-sentence
        split_point = text.rfind("\n", 0, mid) or mid

        first_half = text[:split_point].strip()
        second_half = text[split_point:].strip()

        self.logger.info(f"Split: first={len(first_half)} chars, " f"second={len(second_half)} chars")

        first_clauses = await self._extract_clauses_raw(first_half)
        second_clauses = await self._extract_clauses_raw(second_half)

        # Deduplicate: if the same title appears in both halves keep the longer content
        seen: dict[str, Clause] = {}
        for clause in first_clauses + second_clauses:
            key = clause.title.strip().lower()
            if key not in seen or len(clause.content) > len(seen[key].content):
                seen[key] = clause

        merged = list(seen.values())
        self.logger.info(f"Merged clause count: {len(merged)}")
        return merged

    async def _extract_clauses_raw(self, text: str) -> List[Clause]:
        """Internal helper — same as _extract_clauses but no fallback recursion, used by the two-pass strategy."""

        if not text.strip():
            return []
        try:
            client = self.llm_model.client
            deployment = self.llm_model.deployment_name
            # prompt = self._build_prompt(text)
            prompt = Path(r"src\services\prompts\v1\ai_parser_prompt.mustache").read_text(encoding="utf-8")
            prompt = prompt.replace("{{text}}", text)

            response = client.chat.completions.create(
                model=deployment,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=15000,
                temperature=0,
                response_format={"type": "json_object"},
            )

            finish_reason = response.choices[0].finish_reason
            self.logger.info(f"finish_reason={finish_reason} | " f"completion_tokens={response.usage.completion_tokens}")

            raw_content = response.choices[0].message.content
            clean_content = re.sub(r"^```(?:json)?\s*", "", raw_content.strip())
            clean_content = re.sub(r"\s*```$", "", clean_content)
            data = json.loads(clean_content)
            return [Clause(**c) for c in data.get("clauses", [])]

        except Exception as e:
            self.logger.error(f"Failed: {e}")
            return []

    async def _chunk_clauses(self, clauses: List[Clause]) -> List[str]:
        """Return clause contents as chunks — one chunk per clause."""

        return [f"{clause.title}: {clause.content}" for clause in clauses]

    async def parse_document(self, document: Document, session_data: Optional["SessionData"] = None) -> ParseResult:
        """Parse the document using AI to extract clauses and chunk them."""

        start = time.time()

        try:
            await self.clean_document(document)
            text = await self._extract_text(document)

            self.logger.info(f"Extracted text length: {len(text)}")

            if not text:
                return ParseResult(
                    success=False,
                    chunks=[],
                    metadata={},
                    error_message="No text found in document",
                    processing_time=time.time() - start,
                )

            clauses = await self._extract_clauses(text)
            self.logger.info(f"Total clauses parsed: {len(clauses)}")

            chunks_text = await self._chunk_clauses(clauses)

            vector_store = session_data.vector_store if session_data else self.service_container.faiss_store

            chunks: List[Chunk] = []
            document_id = str(uuid.uuid4())

            for i, (clause, chunk_text) in enumerate(zip(clauses, chunks_text)):
                vector = await self.embedding_service.generate_embeddings(text=chunk_text, task="text-matching")
                await vector_store.index_embedding(vector)

                chunk = Chunk(
                    chunk_id=str(uuid.uuid4()),
                    document_id=document_id,
                    chunk_index=i,
                    content=chunk_text,
                    embedding_model=self.embedding_service.model_name,
                    embedding_vector=None,
                    metadata={
                        "chunk_type": "ai_clause_chunk",
                        "section_heading": clause.title,
                    },
                    created_at=datetime.utcnow().isoformat(),
                )
                chunks.append(chunk)

            return ParseResult(
                success=True,
                chunks=chunks,
                metadata={
                    "document_id": document_id,
                    "num_clauses": len(clauses),
                    "num_chunks": len(chunks),
                    "source": "ai_parser",
                },
                processing_time=time.time() - start,
            )

        except Exception as e:
            self.logger.error(f"Unhandled error: {e}")
            return ParseResult(
                success=False,
                chunks=[],
                metadata={},
                error_message=str(e),
                processing_time=time.time() - start,
            )

    async def parse_data(self, data: Any, session_data: Optional["SessionData"] = None) -> ParseResult:
        """parse_data is not implemented for AI parser."""

        return ParseResult(
            success=False,
            chunks=[],
            metadata={},
            error_message="parse_data not implemented for AI parser",
            processing_time=0.0,
        )

    def is_healthy(self) -> Any:
        """Get the health status of the parser."""

        return {"status": "healthy", "parser_type": "ai"}
