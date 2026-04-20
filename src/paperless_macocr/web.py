"""Web UI routes for document browsing, OCR preview, and approval."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from paperless_macocr.auth import (
    _SESSION_COOKIE,
    _SESSION_MAX_AGE,
    _Signer,
    verify_basic,
)
from paperless_macocr.pdf import (
    pdf_embed_text_layer,
    pdf_page_count,
    pdf_page_to_png,
)

if TYPE_CHECKING:
    from authlib.integrations.starlette_client import OAuth

    from paperless_macocr.config import Settings
    from paperless_macocr.ocr import MacOCRClient, OcrPageData
    from paperless_macocr.paperless import PaperlessClient

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()

# These are set by register_web_ui() at startup
_settings: Settings | None = None
_paperless: PaperlessClient | None = None
_macocr: MacOCRClient | None = None
_signer: _Signer | None = None
_oauth: OAuth | None = None
_tag_cache: dict[int, str] = {}


def register_web_ui(
    settings: Settings,
    paperless: PaperlessClient,
    macocr: MacOCRClient,
    signer: _Signer,
    oauth: OAuth | None,
) -> None:
    """Inject dependencies into the web UI module."""
    global _settings, _paperless, _macocr, _signer, _oauth
    _settings = settings
    _paperless = paperless
    _macocr = macocr
    _signer = signer
    _oauth = oauth


async def _get_tag_map() -> dict[int, str]:
    """Fetch and cache the tag-id → tag-name mapping."""
    global _tag_cache
    if not _tag_cache and _paperless:
        tags = await _paperless.list_tags()
        _tag_cache = {t["id"]: t["name"] for t in tags}
    return _tag_cache


def _user(request: Request) -> str:
    return getattr(request.state, "user", "anonymous")


def _require(*names: str) -> None:
    """Raise 503 if any of the named module-level dependencies are None."""
    g = globals()
    missing = [n for n in names if g.get(n) is None]
    if missing:
        raise HTTPException(status_code=503, detail="Web UI not initialised")


# ─── Auth routes ────────────────────────────────────────────────────


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/ui"):
    _require("_settings")
    if _settings.web_ui_auth == "none":  # type: ignore[union-attr]
        return RedirectResponse("/ui")
    if _settings.web_ui_auth == "oidc" and _oauth is not None:  # type: ignore[union-attr]
        redirect_uri = _settings.oidc_redirect_uri or str(request.url_for("oidc_callback"))  # type: ignore[union-attr]
        return await _oauth.oidc.authorize_redirect(request, redirect_uri, state=next)
    return templates.TemplateResponse(request, "login.html", {"next_url": next})


@router.post("/auth/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/ui"),
):
    _require("_settings", "_signer")
    if not verify_basic(
        username,
        password,
        _settings.web_ui_username,
        _settings.web_ui_password,  # type: ignore[union-attr]
    ):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next_url": next, "error": "Invalid credentials"},
            status_code=401,
        )
    token = _signer.sign({"user": username})  # type: ignore[union-attr]
    response = RedirectResponse(next, status_code=303)
    response.set_cookie(_SESSION_COOKIE, token, max_age=_SESSION_MAX_AGE, httponly=True, samesite="lax")
    return response


@router.get("/auth/callback")
async def oidc_callback(request: Request):
    _require("_oauth", "_signer")
    token = await _oauth.oidc.authorize_access_token(request)  # type: ignore[union-attr]
    userinfo = token.get("userinfo", {})
    user = userinfo.get("preferred_username") or userinfo.get("email") or "oidc-user"
    session_token = _signer.sign({"user": user})  # type: ignore[union-attr]
    next_url = request.query_params.get("state", "/ui")
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(_SESSION_COOKIE, session_token, max_age=_SESSION_MAX_AGE, httponly=True, samesite="lax")
    return response


@router.get("/auth/logout")
async def logout():
    response = RedirectResponse("/auth/login")
    response.delete_cookie(_SESSION_COOKIE)
    return response


# ─── Document list ──────────────────────────────────────────────────


@router.get("/ui", response_class=HTMLResponse)
async def document_list(request: Request, page: int = 1, search: str = ""):
    _require("_settings", "_paperless")
    exclude_tags = _settings.get_exclude_tag_ids()  # type: ignore[union-attr]
    tag_map = await _get_tag_map()

    data = await _paperless.list_documents(  # type: ignore[union-attr]
        page=page,
        page_size=20,
        search=search,
        tags_id_none=exclude_tags or None,
    )

    documents = data.get("results", [])
    total = data.get("count", 0)
    total_pages = (total + 19) // 20

    # Enrich documents with tag names and paperless URL
    paperless_base = str(_settings.paperless_url).rstrip("/")  # type: ignore[union-attr]
    for doc in documents:
        doc["tag_names"] = [tag_map.get(tid, f"#{tid}") for tid in doc.get("tags", [])]
        doc["paperless_link"] = f"{paperless_base}/documents/{doc['id']}/details"
        doc["has_content"] = bool(doc.get("content", "").strip())

    return templates.TemplateResponse(
        request,
        "documents.html",
        {
            "documents": documents,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "search": search,
            "user": _user(request),
            "auth_mode": _settings.web_ui_auth,
            "exclude_tags": [tag_map.get(t, f"#{t}") for t in exclude_tags],
        },
    )


# ─── OCR Preview ────────────────────────────────────────────────────


@router.get("/ui/ocr/{document_id}", response_class=HTMLResponse)
async def ocr_preview(request: Request, document_id: int):
    _require("_settings", "_paperless", "_macocr")

    doc_meta = await _paperless.get_document(document_id)  # type: ignore[union-attr]
    file_bytes = await _paperless.download_document(document_id, original=True)  # type: ignore[union-attr]
    mime_type = doc_meta.get("mime_type", "")
    is_pdf = mime_type == "application/pdf"

    # OCR all pages
    page_results: list[OcrPageData] = []
    page_previews: list[str] = []

    if is_pdf:
        num_pages = pdf_page_count(file_bytes)
        for page_idx in range(num_pages):
            png_bytes = pdf_page_to_png(file_bytes, page_idx, dpi=_settings.ocr_dpi)  # type: ignore[union-attr]
            result = await _macocr.ocr_image(  # type: ignore[union-attr]
                png_bytes, filename=f"page_{page_idx + 1:04d}.png"
            )
            page_results.append(result)
            # Low-res preview
            preview_png = pdf_page_to_png(file_bytes, page_idx, dpi=100)
            page_previews.append(base64.b64encode(preview_png).decode())
    else:
        result = await _macocr.ocr_image(file_bytes, filename="document.png")  # type: ignore[union-attr]
        page_results.append(result)
        page_previews.append(base64.b64encode(file_bytes).decode())

    combined_text = "\n\n".join(r.text.strip() for r in page_results if r.text.strip())
    existing_text = doc_meta.get("content", "").strip()
    has_existing = bool(existing_text)
    tag_map = await _get_tag_map()
    correspondents, doc_types, all_tags = await _gather_meta_options()

    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "doc": doc_meta,
            "document_id": document_id,
            "combined_text": combined_text,
            "existing_text": existing_text,
            "has_existing": has_existing,
            "page_previews": page_previews,
            "page_texts": [r.text.strip() for r in page_results],
            "num_pages": len(page_results),
            "is_pdf": is_pdf,
            "tag_names": [tag_map.get(t, f"#{t}") for t in doc_meta.get("tags", [])],
            "user": _user(request),
            "auth_mode": _settings.web_ui_auth,  # type: ignore[union-attr]
            # Metadata options for autocomplete
            "correspondents": correspondents,
            "document_types": doc_types,
            "all_tags": all_tags,
            # Store serialised OCR data in a hidden field for approval
            "ocr_data_json": _serialize_ocr_data(page_results),
        },
    )


@router.post("/ui/ocr/{document_id}/approve")
async def ocr_approve(request: Request, document_id: int):
    """Apply the OCR results and metadata edits to the document in Paperless."""
    _require("_settings", "_paperless", "_macocr")

    form = await request.form()
    combined_text = str(form.get("combined_text", ""))
    build_pdf = form.get("replace_pdf") == "on"

    if not combined_text.strip():
        raise HTTPException(status_code=422, detail="No text to approve")

    # ── Resolve (and auto-create) metadata fields ─────────────────────
    title = str(form.get("title", "")).strip() or None
    created = str(form.get("created", "")).strip() or None

    correspondents, doc_types, all_tags = await _gather_meta_options()
    corr_name = str(form.get("correspondent", "")).strip()
    dtype_name = str(form.get("document_type", "")).strip()
    tag_names_raw = str(form.get("tags", "")).strip()

    corr_id: int | None = None
    if corr_name:
        match = next((c for c in correspondents if c["name"] == corr_name), None)
        if match:
            corr_id = match["id"]
        else:
            created_corr = await _paperless.create_correspondent(corr_name)  # type: ignore[union-attr]
            corr_id = created_corr["id"]

    dtype_id: int | None = None
    if dtype_name:
        match = next((d for d in doc_types if d["name"] == dtype_name), None)
        if match:
            dtype_id = match["id"]
        else:
            created_dt = await _paperless.create_document_type(dtype_name)  # type: ignore[union-attr]
            dtype_id = created_dt["id"]

    tag_ids: list[int] | None = None
    if tag_names_raw:
        name_to_id = {t["name"]: t["id"] for t in all_tags}
        tag_ids = []
        for name in (n.strip() for n in tag_names_raw.split(",") if n.strip()):
            if name in name_to_id:
                tag_ids.append(name_to_id[name])
            else:
                new_tag = await _paperless.create_tag(name)  # type: ignore[union-attr]
                tag_ids.append(new_tag["id"])

    # ── Patch document ────────────────────────────────────────────────
    await _paperless.update_document_metadata(  # type: ignore[union-attr]
        document_id,
        title=title,
        created=created,
        correspondent=corr_id,
        document_type=dtype_id,
        tags=tag_ids,
        content=combined_text,
    )
    logger.info("Web UI: approved OCR + metadata for document %d", document_id)

    # ── Optionally rebuild searchable PDF and re-upload ───────────────
    if build_pdf:
        await _rebuild_and_replace_pdf(document_id, combined_text)

    return RedirectResponse(f"/ui?approved={document_id}", status_code=303)


_TASK_POLL_INTERVAL = 2  # seconds
_TASK_POLL_MAX = 60  # max attempts


async def _rebuild_and_replace_pdf(document_id: int, combined_text: str) -> None:
    """Build a searchable PDF and upload it to Paperless, replacing the original.

    Mirrors _replace_with_searchable_pdf in app.py but operates on the
    web-UI module-level paperless/macocr/settings singletons.
    """
    # Lazy import to avoid circular dependency at module level
    from paperless_macocr.app import _replacing_titles

    doc_meta = await _paperless.get_document(document_id)  # type: ignore[union-attr]
    mime = doc_meta.get("mime_type", "")
    if mime != "application/pdf":
        logger.warning("Web UI: rebuild_pdf skipped — document %d is not a PDF (%s)", document_id, mime)
        return

    file_bytes = await _paperless.download_document(document_id, original=True)  # type: ignore[union-attr]
    num_pages = pdf_page_count(file_bytes)
    page_results: list[OcrPageData] = []
    for page_idx in range(num_pages):
        png_bytes = pdf_page_to_png(file_bytes, page_idx, dpi=_settings.ocr_dpi)  # type: ignore[union-attr]
        result = await _macocr.ocr_image(  # type: ignore[union-attr]
            png_bytes, filename=f"page_{page_idx + 1:04d}.png"
        )
        page_results.append(result)

    searchable_pdf = pdf_embed_text_layer(file_bytes, page_results)
    logger.info("Web UI: built searchable PDF for document %d (%d bytes)", document_id, len(searchable_pdf))

    title = doc_meta.get("title") or f"doc-{document_id}"
    _replacing_titles.add(title)
    try:
        task_uuid = await _paperless.upload_document(  # type: ignore[union-attr]
            searchable_pdf,
            f"{title}.pdf",
            title=title,
            correspondent=doc_meta.get("correspondent"),
            document_type=doc_meta.get("document_type"),
            storage_path=doc_meta.get("storage_path"),
            tags=doc_meta.get("tags"),
            archive_serial_number=doc_meta.get("archive_serial_number"),
        )

        new_doc_id = None
        for _ in range(_TASK_POLL_MAX):
            await asyncio.sleep(_TASK_POLL_INTERVAL)
            task = await _paperless.get_task(task_uuid)  # type: ignore[union-attr]
            status = task.get("status", "")
            if status == "SUCCESS":
                new_doc_id = task.get("related_document")
                break
            if status == "FAILURE":
                logger.error(
                    "Web UI: consumption failed for document %d: %s",
                    document_id,
                    task.get("result", "unknown error"),
                )
                return

        if new_doc_id is None:
            logger.error("Web UI: consumption timed out for document %d (task %s)", document_id, task_uuid)
            return

        await _paperless.update_document_content(int(new_doc_id), combined_text)  # type: ignore[union-attr]

        # Remove inbox / auto-added tags from the new document
        remove_entries = _settings.get_replace_pdf_remove_tags()  # type: ignore[union-attr]
        if remove_entries:
            await _paperless.remove_tags_from_document(int(new_doc_id), remove_entries)  # type: ignore[union-attr]

        await _paperless.delete_document(document_id)  # type: ignore[union-attr]
        logger.info("Web UI: replaced document %d with searchable PDF (new id: %d)", document_id, new_doc_id)
    finally:
        _replacing_titles.discard(title)


# ─── Thumbnail proxy ────────────────────────────────────────────────


@router.get("/ui/thumb/{document_id}")
async def thumbnail(document_id: int):
    _require("_paperless")
    data = await _paperless.get_thumbnail(document_id)  # type: ignore[union-attr]
    return Response(content=data, media_type="image/webp")


# ─── Metadata options (autocomplete) ────────────────────────────────


@router.get("/ui/meta-options")
async def meta_options():
    """Return all correspondents, document types, and tags for autocomplete."""
    _require("_paperless")
    correspondents, doc_types, tags = await _gather_meta_options()
    return JSONResponse(
        {
            "correspondents": [{"id": c["id"], "name": c["name"]} for c in correspondents],
            "document_types": [{"id": d["id"], "name": d["name"]} for d in doc_types],
            "tags": [{"id": t["id"], "name": t["name"]} for t in tags],
        }
    )


async def _gather_meta_options() -> tuple[list, list, list]:
    correspondents = await _paperless.list_correspondents()  # type: ignore[union-attr]
    doc_types = await _paperless.list_document_types()  # type: ignore[union-attr]
    tags = await _paperless.list_tags()  # type: ignore[union-attr]
    return correspondents, doc_types, tags


# ─── Helpers ────────────────────────────────────────────────────────


def _serialize_ocr_data(page_results: list[OcrPageData]) -> str:
    """Serialize OCR results to a JSON string for form submission."""
    import json

    return json.dumps(
        [
            {
                "text": r.text,
                "boxes": r.boxes,
                "image_width": r.image_width,
                "image_height": r.image_height,
            }
            for r in page_results
        ]
    )
