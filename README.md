# Paperless-macOCR

A webhook service that re-OCRs [Paperless-NGX](https://docs.paperless-ngx.com/) documents using [macOCR](https://github.com/riddleling/macocr) (Apple's Vision Framework) for significantly more accurate text recognition.

## How It Works

```
┌─────────────┐   webhook    ┌──────────────────┐   HTTP upload  ┌─────────┐
│ Paperless   │─────────────▶│ paperless-macocr │───────────────▶│ macOCR  │
│ NGX         │              │ (this service)   │◀───────────────│ server  │
│             │◁─────────────│                  │   OCR text     │ (macOS) │
└─────────────┘  PATCH API   └──────────────────┘                └─────────┘
```

1. Paperless-NGX consumes a document and sends a **workflow webhook** to this service.
2. The service downloads the PDF via the Paperless-NGX API.
3. Each page is rendered to a PNG image and sent to a **macOCR HTTP server** running on macOS.
4. The combined OCR text replaces the document's content in Paperless-NGX.

## Prerequisites

- **Paperless-NGX** (v2.x+) with API access
- **macOCR** running in HTTP server mode on a macOS host:
  ```bash
  # Install on macOS
  cargo install macocr

  # Start HTTP server
  macocr -s -p 8080
  # Or with authentication:
  macocr -s -a admin:password123 -p 8080
  ```

## Quick Start

### Docker Compose (recommended)

1. Copy the environment template:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` with your settings:
   ```env
   PAPERLESS_URL=http://your-paperless:8000
   PAPERLESS_TOKEN=your-api-token
   MACOCR_URL=http://host.docker.internal:8080
   ```

3. Start the service:
   ```bash
   docker compose up -d
   ```

### Docker Run

```bash
docker run -d \
  --name paperless-macocr \
  -p 9000:9000 \
  -e PAPERLESS_URL=http://your-paperless:8000 \
  -e PAPERLESS_TOKEN=your-api-token \
  -e MACOCR_URL=http://host.docker.internal:8080 \
  ghcr.io/OWNER/paperless-macocr:latest
```

### Local Development

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Copy and configure environment
cp .env.example .env
# Edit .env ...

# Run the service
paperless-macocr
```

## Paperless-NGX Webhook Configuration

1. Go to **Paperless-NGX → Manage → Workflows** (in the sidebar)
2. Create a new workflow:
   - **Trigger**: Document Added (consumption finished)
   - **Action**: Webhook
   - **URL**: `http://paperless-macocr:9000/webhook`
   - **Encoding**: JSON
   - **Body** (use key-value params):
     | Key | Value |
     |-----|-------|
     | `doc_url` | `{{doc_url}}` |

   > **Note:** Paperless-NGX does not expose a document ID placeholder directly.
   > The service extracts the ID from the `{{doc_url}}` placeholder
   > (e.g. `http://paperless:8000/documents/42/`).
   > Make sure `PAPERLESS_URL` is set in your Paperless-NGX configuration.

> If using `WEBHOOK_SECRET`, configure an `X-Webhook-Signature` header with an HMAC-SHA256 hex digest of the request body.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Health check |
| `POST` | `/webhook` | Paperless-NGX webhook receiver |
| `POST` | `/ocr/{document_id}` | Manually trigger OCR for one document |
| `POST` | `/ocr/batch` | Trigger OCR for multiple documents |

### Batch OCR Example

```bash
curl -X POST http://localhost:9000/ocr/batch \
  -H "Content-Type: application/json" \
  -d '{"document_ids": [1, 2, 3, 4, 5]}'
```

## Configuration

All settings are configured via environment variables (or a `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `PAPERLESS_URL` | *(required)* | Paperless-NGX base URL |
| `PAPERLESS_TOKEN` | *(required)* | Paperless-NGX API token |
| `MACOCR_URL` | *(required)* | macOCR HTTP server URL |
| `MACOCR_AUTH` | `""` | macOCR basic auth (`user:pass`) |
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `9000` | Listen port |
| `WEBHOOK_SECRET` | `""` | HMAC-SHA256 shared secret |
| `LOG_LEVEL` | `INFO` | Logging level |
| `OCR_DPI` | `300` | PDF-to-image rendering DPI |
| `SKIP_IF_TEXT_PRESENT` | `true` | Skip PDFs that already have text |

## Architecture Notes

- **macOCR runs on macOS only** — it uses Apple's Vision Framework which requires macOS 13.0+. The Docker container runs this Python service, not macOCR itself.
- The service is stateless. All state lives in Paperless-NGX.
- Background tasks process OCR asynchronously so webhooks respond immediately.
- Images (JPEG, PNG, TIFF, WebP, GIF, BMP) are sent directly to macOCR; unsupported types (emails, plain text, etc.) are skipped automatically.

## Running Tests

```bash
pip install -e ".[dev]"
pytest -v
```

## License

MIT
