"""Tests for the FastAPI webhook application."""

import hashlib
import hmac
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from paperless_macocr.app import app, state
from paperless_macocr.config import Settings
from paperless_macocr.ocr import OcrPageData


@pytest.fixture(autouse=True)
def _setup_state():
    """Provide minimal state for tests."""
    state.settings = Settings(
        paperless_url="http://localhost:8000",
        paperless_token="test-token",
        macocr_url="http://localhost:8080",
        webhook_secret="",
    )
    state.paperless = AsyncMock()
    state.macocr = AsyncMock()
    yield


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "paperless-macocr"


def test_webhook_with_document_id(client):
    resp = client.post("/webhook", json={"document_id": 42})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert "42" in data["message"]


def test_webhook_with_id_field(client):
    resp = client.post("/webhook", json={"id": 7})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_webhook_missing_id(client):
    resp = client.post("/webhook", json={"foo": "bar"})
    assert resp.status_code == 422


def test_webhook_with_doc_url(client):
    resp = client.post("/webhook", json={"doc_url": "http://paperless:8000/documents/55/"})
    assert resp.status_code == 200
    assert "55" in resp.json()["message"]


def test_webhook_with_doc_url_no_trailing_slash(client):
    resp = client.post("/webhook", json={"doc_url": "http://paperless:8000/documents/123"})
    assert resp.status_code == 200
    assert "123" in resp.json()["message"]


def test_webhook_with_invalid_doc_url(client):
    resp = client.post("/webhook", json={"doc_url": "http://paperless:8000/other/path"})
    assert resp.status_code == 422


def test_webhook_secret_verification(client):
    state.settings.webhook_secret = "my-secret"
    payload = b'{"document_id": 1}'
    sig = hmac.new(b"my-secret", payload, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhook",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
        },
    )
    assert resp.status_code == 200


def test_webhook_secret_invalid(client):
    state.settings.webhook_secret = "my-secret"
    resp = client.post(
        "/webhook",
        json={"document_id": 1},
        headers={"X-Webhook-Signature": "bad-sig"},
    )
    assert resp.status_code == 401


def test_webhook_secret_missing_header(client):
    state.settings.webhook_secret = "my-secret"
    resp = client.post("/webhook", json={"document_id": 1})
    assert resp.status_code == 401


def test_manual_trigger(client):
    resp = client.post("/ocr/99")
    assert resp.status_code == 200
    assert "99" in resp.json()["message"]


def test_batch_trigger(client):
    resp = client.post("/ocr/batch", json={"document_ids": [1, 2, 3]})
    assert resp.status_code == 200
    assert "3" in resp.json()["message"]


def test_batch_trigger_empty(client):
    resp = client.post("/ocr/batch", json={"document_ids": []})
    # Empty list is caught by the endpoint (422)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_process_document_skips_unsupported_type():
    from paperless_macocr.app import process_document

    state.paperless.get_document = AsyncMock(
        return_value={"mime_type": "message/rfc822", "original_file_name": "email.eml"}
    )

    await process_document(1)

    state.paperless.download_document.assert_not_called()


@pytest.mark.asyncio
async def test_process_document_image():
    from paperless_macocr.app import process_document

    state.paperless.get_document = AsyncMock(
        return_value={"mime_type": "image/jpeg", "original_file_name": "photo.jpg"}
    )
    state.paperless.download_document = AsyncMock(return_value=b"fake-image-bytes")
    state.paperless.update_document_content = AsyncMock(return_value={})
    state.macocr.ocr_image = AsyncMock(return_value=OcrPageData(text="Text from image"))

    await process_document(10)

    state.macocr.ocr_image.assert_called_once()
    state.paperless.update_document_content.assert_called_once_with(10, "Text from image")


@pytest.mark.asyncio
async def test_process_document_full_pipeline():
    import pymupdf

    from paperless_macocr.app import process_document

    doc = pymupdf.open()
    doc.new_page(width=100, height=100)
    pdf_bytes = doc.tobytes()
    doc.close()

    state.paperless.get_document = AsyncMock(
        return_value={"mime_type": "application/pdf", "original_file_name": "test.pdf"}
    )
    state.paperless.download_document = AsyncMock(return_value=pdf_bytes)
    state.paperless.update_document_content = AsyncMock(return_value={})
    state.macocr.ocr_image = AsyncMock(return_value=OcrPageData(text="Hello World"))

    await process_document(42)

    state.macocr.ocr_image.assert_called_once()
    state.paperless.update_document_content.assert_called_once_with(42, "Hello World")


@pytest.mark.asyncio
async def test_process_document_replace_pdf():
    """When replace_pdf is enabled, uploads a searchable PDF and deletes the old one."""
    import pymupdf

    from paperless_macocr.app import process_document

    doc = pymupdf.open()
    doc.new_page(width=200, height=200)
    pdf_bytes = doc.tobytes()
    doc.close()

    state.settings.replace_pdf = True

    state.paperless.get_document = AsyncMock(
        return_value={
            "mime_type": "application/pdf",
            "original_file_name": "test.pdf",
            "title": "Test Doc",
            "correspondent": 1,
            "document_type": 2,
            "storage_path": None,
            "tags": [3, 4],
            "archive_serial_number": None,
        }
    )
    state.paperless.download_document = AsyncMock(return_value=pdf_bytes)
    state.paperless.update_document_content = AsyncMock(return_value={})
    state.paperless.upload_document = AsyncMock(return_value="task-uuid-123")
    state.paperless.get_task = AsyncMock(return_value={"status": "SUCCESS", "related_document": 99})
    state.paperless.delete_document = AsyncMock()

    ocr_result = OcrPageData(
        text="OCR text",
        boxes=[{"text": "OCR text", "x": 10, "y": 10, "w": 80, "h": 14}],
        image_width=200.0,
        image_height=200.0,
    )
    state.macocr.ocr_image = AsyncMock(return_value=ocr_result)

    await process_document(42)

    # Should have uploaded a searchable PDF
    state.paperless.upload_document.assert_called_once()
    upload_kwargs = state.paperless.upload_document.call_args
    assert upload_kwargs.kwargs["title"] == "Test Doc"

    # Should update content on the NEW document
    calls = state.paperless.update_document_content.call_args_list
    assert any(c.args == (99, "OCR text") for c in calls)

    # Should delete the old document
    state.paperless.delete_document.assert_called_once_with(42)
