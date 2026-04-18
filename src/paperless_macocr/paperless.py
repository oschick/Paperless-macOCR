"""Paperless-NGX REST API client."""

import logging
from typing import Any

import httpx

from paperless_macocr.config import Settings

logger = logging.getLogger(__name__)


class PaperlessClient:
    """Async client for the Paperless-NGX REST API."""

    def __init__(self, settings: Settings) -> None:
        base_url = str(settings.paperless_url).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Token {settings.paperless_token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def get_document(self, document_id: int) -> dict[str, Any]:
        """Fetch document metadata by ID."""
        response = await self._client.get(f"/api/documents/{document_id}/")
        response.raise_for_status()
        return response.json()

    async def download_document(self, document_id: int) -> bytes:
        """Download the original document file."""
        response = await self._client.get(f"/api/documents/{document_id}/download/")
        response.raise_for_status()
        return response.content

    async def update_document_content(self, document_id: int, content: str) -> dict[str, Any]:
        """Update the OCR content text of a document."""
        response = await self._client.patch(
            f"/api/documents/{document_id}/",
            json={"content": content},
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        logger.info("Updated content for document %d (%d chars)", document_id, len(content))
        return result
