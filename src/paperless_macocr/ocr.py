"""macOCR HTTP server client."""

import logging

import httpx

from paperless_macocr.config import Settings

logger = logging.getLogger(__name__)


class MacOCRClient:
    """Async client for the macOCR HTTP server."""

    def __init__(self, settings: Settings) -> None:
        base_url = str(settings.macocr_url).rstrip("/")

        auth = None
        if settings.macocr_auth:
            parts = settings.macocr_auth.split(":", 1)
            if len(parts) == 2:
                auth = httpx.BasicAuth(username=parts[0], password=parts[1])

        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def ocr_image(self, image_bytes: bytes, filename: str = "page.png") -> str:
        """Send an image to macOCR and return the recognized text.

        Args:
            image_bytes: PNG image data.
            filename: Filename hint for the upload.

        Returns:
            Extracted text string.
        """
        response = await self._client.post(
            "/upload",
            files={"file": (filename, image_bytes, "image/png")},
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("success"):
            msg = data.get("message", "Unknown macOCR error")
            raise RuntimeError(f"macOCR returned failure: {msg}")

        result: str = data.get("ocr_result", "")
        return result
