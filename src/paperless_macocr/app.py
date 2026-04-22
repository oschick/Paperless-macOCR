"""FastAPI application - webhook receiver and OCR orchestration."""

import asyncio
import hashlib
import hmac
import logging
import re
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from paperless_macocr.config import Settings, get_settings
from paperless_macocr.ocr import MacOCRClient, OcrPageData
from paperless_macocr.paperless import PaperlessClient
from paperless_macocr.pdf import (
    image_to_searchable_pdf,
    pdf_embed_text_layer,
    pdf_has_text,
    pdf_page_count,
    pdf_page_to_png,
)

logger = logging.getLogger(__name__)

# Titles of documents currently being replaced with searchable PDFs.
# Checked by process_document() to break webhook loops.  The title is
# added *before* uploading so it is already in the set when the
# webhook for the new document fires.
_replacing_titles: set[str] = set()

# Signer and OAuth client for the web UI - populated at module level so they
# are available before the app starts (middleware registration requirement).
_web_signer = None
_web_oauth = None


# ---------------------------------------------------------------------------
# Shared state populated during lifespan
# ---------------------------------------------------------------------------
class AppState:
    settings: Settings
    paperless: PaperlessClient
    macocr: MacOCRClient


state = AppState()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    state.settings = settings
    state.paperless = PaperlessClient(settings)
    state.macocr = MacOCRClient(settings)

    # Set up web UI if enabled
    if settings.web_ui_enabled:
        from paperless_macocr.web import register_web_ui

        register_web_ui(settings, state.paperless, state.macocr, _web_signer, _web_oauth)

    logger.info("Paperless-macOCR service started")
    yield
    await state.paperless.close()
    await state.macocr.close()
    logger.info("Paperless-macOCR service stopped")


app = FastAPI(
    title="Paperless-macOCR",
    description="Re-OCR Paperless-NGX documents via macOCR (Apple Vision Framework)",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Web UI — middleware and router must be registered BEFORE the app starts.
# (Starlette raises RuntimeError if add_middleware is called post-startup.)
# Wrapped in try/except so the module can be imported in test environments
# where the required env vars are not set.
# ---------------------------------------------------------------------------
try:
    _boot_settings = get_settings()
    if _boot_settings.web_ui_enabled:
        from paperless_macocr.auth import setup_auth
        from paperless_macocr.web import router as web_router

        _web_signer, _web_oauth = setup_auth(app, _boot_settings)
        app.include_router(web_router)
except Exception:  # noqa: S110
    pass  # env vars absent (e.g. test environment) — lifespan will validate


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class WebhookPayload(BaseModel):
    """Flexible model for Paperless-NGX workflow webhook payloads.

    Paperless-NGX sends different payload shapes depending on the
    workflow trigger.  We only require the document ID.

    Supports:
      - Direct ID fields: document_id, id, doc
      - doc_url: Paperless-NGX document URL like
        http://paperless:8000/documents/42/ from which the ID is extracted.
    """

    document_id: int | None = None
    # Alternative field names used by different Paperless versions / configs
    id: int | None = None
    doc: int | None = None
    # Paperless-NGX workflow {{doc_url}} placeholder
    doc_url: str | None = None

    def resolve_document_id(self) -> int:
        """Return the document ID from whichever field is populated."""
        for candidate in (self.document_id, self.id, self.doc):
            if candidate is not None:
                return candidate
        if self.doc_url:
            match = re.search(r"/documents/(\d+)", self.doc_url)
            if match:
                return int(match.group(1))
        raise ValueError("No document ID found in payload")


class BatchRequest(BaseModel):
    document_ids: list[int]


class StatusResponse(BaseModel):
    status: str
    message: str


class HealthResponse(BaseModel):
    status: str
    service: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _verify_webhook_secret(payload_body: bytes, signature: str | None, secret: str) -> None:
    """Verify HMAC-SHA256 webhook signature when a secret is configured."""
    if not secret:
        return
    if not signature:
        raise HTTPException(status_code=401, detail="Missing webhook signature")
    expected = hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


_SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/webp",
    "image/gif",
    "image/bmp",
}


async def process_document(document_id: int) -> None:
    """Core OCR pipeline for a single document."""
    settings = state.settings
    paperless = state.paperless
    macocr = state.macocr

    logger.info("Processing document %d", document_id)

    # 1. Fetch metadata
    doc_meta = await paperless.get_document(document_id)
    mime_type: str = doc_meta.get("mime_type", "")
    original_name: str = doc_meta.get("original_file_name", f"doc-{document_id}")
    title: str = doc_meta.get("title", "")

    # Guard: skip documents we are currently replacing to avoid infinite loops.
    # _replacing_titles is populated before the upload, so it is always set
    # by the time the webhook for the newly consumed document arrives.
    if title and title in _replacing_titles:
        logger.info("Document %d (%s) is being replaced by us - skipping", document_id, title)
        return

    if mime_type not in _SUPPORTED_MIME_TYPES:
        logger.info("Document %d has unsupported type (%s) - skipping", document_id, mime_type)
        return

    # 2. Download original file (not the Paperless archive/OCR'd version)
    file_bytes = await paperless.download_document(document_id, original=True)
    logger.info("Downloaded document %d (%s, %d bytes)", document_id, original_name, len(file_bytes))

    is_pdf = mime_type == "application/pdf"

    # 3. Optionally skip if PDF already has text
    if is_pdf and settings.skip_if_text_present and pdf_has_text(file_bytes):
        logger.info("Document %d already has extractable text - skipping", document_id)
        return

    # 4. OCR via macOCR -- collect per-page results
    page_results: list[OcrPageData] = []

    if is_pdf:
        num_pages = pdf_page_count(file_bytes)
        logger.info("Document %d has %d page(s)", document_id, num_pages)

        for page_idx in range(num_pages):
            logger.debug("Rendering page %d/%d at %d DPI", page_idx + 1, num_pages, settings.ocr_dpi)
            png_bytes = pdf_page_to_png(file_bytes, page_idx, dpi=settings.ocr_dpi)

            result = await macocr.ocr_image(png_bytes, filename=f"page_{page_idx + 1:04d}.png")
            page_results.append(result)
            logger.debug("Page %d: %d chars extracted", page_idx + 1, len(result.text))
    else:
        # Image file - send directly to macOCR
        logger.info("Document %d is an image (%s), sending directly to macOCR", document_id, mime_type)
        ext = mime_type.split("/")[-1]
        result = await macocr.ocr_image(file_bytes, filename=f"document.{ext}", content_type=mime_type)
        page_results.append(result)

    combined_text = "\n\n".join(r.text.strip() for r in page_results if r.text.strip())

    if not combined_text:
        logger.warning("No text extracted from document %d", document_id)
        return

    # 5. Update Paperless-NGX document content
    await paperless.update_document_content(document_id, combined_text)
    logger.info("Document %d OCR complete (%d chars)", document_id, len(combined_text))

    # 6. Optionally replace the document with a searchable PDF
    if settings.replace_pdf:
        await _replace_with_searchable_pdf(
            document_id, doc_meta, file_bytes, page_results, is_pdf, mime_type, combined_text
        )


_TASK_POLL_INTERVAL = 2  # seconds
_TASK_POLL_MAX = 60  # max attempts


async def _replace_with_searchable_pdf(
    document_id: int,
    doc_meta: dict,
    file_bytes: bytes,
    page_results: list[OcrPageData],
    is_pdf: bool,
    mime_type: str,
    combined_text: str,
) -> None:
    """Build a searchable PDF and upload it to Paperless, replacing the original."""
    paperless = state.paperless
    title = doc_meta.get("title", f"doc-{document_id}")

    # Build searchable PDF
    if is_pdf:
        searchable_pdf = pdf_embed_text_layer(file_bytes, page_results)
    else:
        ext = mime_type.split("/")[-1]
        searchable_pdf = image_to_searchable_pdf(file_bytes, page_results[0], image_format=ext)

    logger.info("Built searchable PDF for document %d (%d bytes)", document_id, len(searchable_pdf))

    # Mark this title as in-flight BEFORE uploading so the webhook for
    # the newly consumed document will be skipped by process_document().
    _replacing_titles.add(title)
    try:
        task_uuid = await paperless.upload_document(
            searchable_pdf,
            f"{title}.pdf",
            title=title,
            correspondent=doc_meta.get("correspondent"),
            document_type=doc_meta.get("document_type"),
            storage_path=doc_meta.get("storage_path"),
            tags=doc_meta.get("tags"),
            archive_serial_number=doc_meta.get("archive_serial_number"),
        )

        # Wait for consumption to complete
        new_doc_id = None
        for _ in range(_TASK_POLL_MAX):
            await asyncio.sleep(_TASK_POLL_INTERVAL)
            task = await paperless.get_task(task_uuid)
            status = task.get("status", "")
            if status == "SUCCESS":
                new_doc_id = task.get("related_document")
                break
            if status == "FAILURE":
                logger.error(
                    "Consumption failed for document %d: %s",
                    document_id,
                    task.get("result", "unknown error"),
                )
                return

        if new_doc_id is None:
            logger.error("Consumption timed out for document %d (task %s)", document_id, task_uuid)
            return

        new_doc_id = int(new_doc_id)

        # Set OCR content on the new document
        await paperless.update_document_content(new_doc_id, combined_text)

        # Delete the original document
        await paperless.delete_document(document_id)
        logger.info(
            "Replaced document %d with searchable PDF (new id: %d)",
            document_id,
            new_doc_id,
        )
    finally:
        _replacing_titles.discard(title)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "paperless-macocr"}


@app.get("/", include_in_schema=False)
async def root():
    """Redirect root to the web UI."""
    return RedirectResponse("/ui")


@app.post("/webhook", response_model=StatusResponse)
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_webhook_signature: str | None = Header(default=None),
) -> dict[str, str]:
    """Receive a webhook from Paperless-NGX workflow and trigger OCR."""
    body = await request.body()
    _verify_webhook_secret(body, x_webhook_signature, state.settings.webhook_secret)

    payload = await request.json()

    # Handle both direct payload and nested payload structures
    if isinstance(payload, dict):
        webhook_data = WebhookPayload(**payload)
    else:
        raise HTTPException(status_code=422, detail="Expected JSON object")

    try:
        document_id = webhook_data.resolve_document_id()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info("Webhook received for document %d", document_id)
    background_tasks.add_task(process_document, document_id)
    return {"status": "accepted", "message": f"OCR queued for document {document_id}"}


@app.post("/ocr/batch", response_model=StatusResponse)
async def trigger_ocr_batch(
    body: BatchRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Trigger OCR for multiple documents."""
    if not body.document_ids:
        raise HTTPException(status_code=422, detail="No document_ids provided")

    for doc_id in body.document_ids:
        background_tasks.add_task(process_document, doc_id)

    return {
        "status": "accepted",
        "message": f"OCR queued for {len(body.document_ids)} document(s)",
    }


@app.post("/ocr/{document_id}", response_model=StatusResponse)
async def trigger_ocr(document_id: int, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Manually trigger OCR for a specific document by ID."""
    logger.info("Manual OCR trigger for document %d", document_id)
    background_tasks.add_task(process_document, document_id)
    return {"status": "accepted", "message": f"OCR queued for document {document_id}"}
