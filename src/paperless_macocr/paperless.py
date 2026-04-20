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

    async def download_document(self, document_id: int, *, original: bool = False) -> bytes:
        """Download a document file.

        Args:
            document_id: Paperless document ID.
            original: If True, fetch the original upload instead of the
                      archive (Paperless-OCR'd) version.
        """
        params = {"original": "true"} if original else {}
        response = await self._client.get(
            f"/api/documents/{document_id}/download/",
            params=params,
        )
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

    async def upload_document(
        self,
        file_bytes: bytes,
        filename: str,
        *,
        title: str | None = None,
        correspondent: int | None = None,
        document_type: int | None = None,
        storage_path: int | None = None,
        tags: list[int] | None = None,
        archive_serial_number: int | None = None,
    ) -> str:
        """Upload a document for consumption. Returns the task UUID."""
        data: dict[str, Any] = {}
        if title is not None:
            data["title"] = title
        if correspondent is not None:
            data["correspondent"] = str(correspondent)
        if document_type is not None:
            data["document_type"] = str(document_type)
        if storage_path is not None:
            data["storage_path"] = str(storage_path)
        if archive_serial_number is not None:
            data["archive_serial_number"] = str(archive_serial_number)
        if tags:
            data["tags"] = [str(t) for t in tags]

        response = await self._client.post(
            "/api/documents/post_document/",
            files={"document": (filename, file_bytes, "application/pdf")},
            data=data,
        )
        response.raise_for_status()
        task_id: str = response.text.strip().strip('"')
        logger.info("Uploaded %s, task %s", filename, task_id)
        return task_id

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Return the task status for a consumption task."""
        response = await self._client.get(f"/api/tasks/?task_id={task_id}")
        response.raise_for_status()
        results = response.json()
        if results:
            return results[0]
        return {}

    async def delete_document(self, document_id: int) -> None:
        """Delete a document by ID."""
        response = await self._client.delete(f"/api/documents/{document_id}/")
        response.raise_for_status()
        logger.info("Deleted document %d", document_id)

    async def list_documents(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        ordering: str = "-added",
        search: str = "",
        tags_id_all: list[int] | None = None,
        tags_id_none: list[int] | None = None,
    ) -> dict[str, Any]:
        """List documents with optional filtering.

        Returns the raw Paperless-NGX paginated response with
        ``count``, ``next``, ``previous``, and ``results`` keys.
        """
        params: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "ordering": ordering,
        }
        if search:
            params["query"] = search
        if tags_id_all:
            params["tags__id__all"] = ",".join(str(t) for t in tags_id_all)
        if tags_id_none:
            params["tags__id__none"] = ",".join(str(t) for t in tags_id_none)
        response = await self._client.get("/api/documents/", params=params)
        response.raise_for_status()
        return response.json()

    async def list_tags(self) -> list[dict[str, Any]]:
        """Return all tags from Paperless-NGX."""
        results: list[dict[str, Any]] = []
        url = "/api/tags/?page_size=100"
        while url:
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()
            results.extend(data.get("results", []))
            url = data.get("next", "")
            if url:
                # next is an absolute URL; strip the base to use relative
                base = str(self._client.base_url).rstrip("/")
                if url.startswith(base):
                    url = url[len(base) :]
        return results

    async def get_thumbnail(self, document_id: int) -> bytes:
        """Download the thumbnail image for a document."""
        response = await self._client.get(f"/api/documents/{document_id}/thumb/")
        response.raise_for_status()
        return response.content
