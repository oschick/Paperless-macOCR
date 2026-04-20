# Paperless-macOCR

A webhook service that re-OCRs [Paperless-NGX](https://docs.paperless-ngx.com/) documents using [macOCR](https://github.com/riddleling/macocr) or [iOS-OCR-Server](https://github.com/riddleling/iOS-OCR-Server) (Apple's Vision Framework) for significantly more accurate text recognition.

## How It Works

```
┌─────────────┐   webhook    ┌──────────────────┐   HTTP upload  ┌──────────────┐
│ Paperless   │─────────────▶│ paperless-macocr │───────────────▶│ macOCR       │
│ NGX         │              │ (this service)   │◀───────────────│ or iOS-OCR   │
│             │◁─────────────│                  │   OCR text     │ Server       │
└─────────────┘  PATCH API   └──────────────────┘                └──────────────┘
```

1. Paperless-NGX consumes a document and sends a **workflow webhook** to this service.
2. The service downloads the **original** document via the Paperless-NGX API (not the archive/OCR'd version).
3. Each page is rendered to a PNG image and sent to a **macOCR** or **iOS-OCR-Server** instance.
4. The combined OCR text replaces the document's content in Paperless-NGX.
5. *(Optional)* When `REPLACE_PDF=true`, the service builds a **searchable PDF** with an invisible text layer from the macOCR bounding boxes, uploads it to Paperless-NGX (preserving all metadata), and deletes the old document. This makes the Paperless PDF preview and text selection use the accurate macOCR results instead of the built-in OCR.
6. *(Optional)* The built-in **Web UI** lets you browse documents, preview OCR results page-by-page, and approve them before writing back to Paperless.

## Prerequisites

- **Paperless-NGX** (v2.x+) with API access
- **One of the following OCR backends:**

  **Option A: macOCR** (macOS CLI/HTTP server, requires macOS 13+):
  ```bash
  # Install on macOS
  cargo install macocr

  # Start HTTP server
  macocr -s -p 8080
  # Or with authentication:
  macocr -s -a admin:password123 -p 8080
  ```

  **Autostart macOCR on boot (launchd):**

  Create `~/Library/LaunchAgents/com.macocr.server.plist`:
  ```xml
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.macocr.server</string>
    <key>ProgramArguments</key>
    <array>
      <string>/Users/YOUR_USERNAME/.cargo/bin/macocr</string>
      <string>-s</string>
      <string>-p</string>
      <string>8080</string>
      <!-- add -a admin:password123 here to enable authentication -->
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/macocr.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/macocr.error.log</string>
  </dict>
  </plist>
  ```

  Then load it:
  ```bash
  # Replace YOUR_USERNAME with your actual macOS username
  launchctl load ~/Library/LaunchAgents/com.macocr.server.plist

  # Verify it's running
  launchctl list | grep macocr
  ```

  **Option B: iOS-OCR-Server** (iOS app, runs on iPhone/iPad):
  1. Install [OCR Server](https://apps.apple.com/us/app/ocr-server/id6749533041) from the App Store
  2. Launch the app — the server starts automatically on port 8000
  3. Note the IP address displayed in the app
  4. Set `MACOCR_URL=http://<iphone-ip>:8000`

  > Both backends expose the same `/upload` API and return identical JSON responses, so they are fully interchangeable.

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
| `GET`  | `/ui` | Web UI — document browser *(when enabled)* |
| `GET`  | `/ui/ocr/{id}` | Web UI — OCR preview for a document |
| `POST` | `/ui/ocr/{id}/approve` | Web UI — approve & write OCR results |
| `GET`  | `/ui/thumb/{id}` | Web UI — document thumbnail proxy |
| `GET`  | `/ui/meta-options` | Web UI — JSON list of all tags, correspondents, document types |

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
| `MACOCR_URL` | *(required)* | OCR server URL (macOCR or iOS-OCR-Server) |
| `MACOCR_AUTH` | `""` | OCR server basic auth (`user:pass`, macOCR only) |
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `9000` | Listen port |
| `WEBHOOK_SECRET` | `""` | HMAC-SHA256 shared secret |
| `LOG_LEVEL` | `INFO` | Logging level |
| `OCR_DPI` | `300` | PDF-to-image rendering DPI |
| `SKIP_IF_TEXT_PRESENT` | `true` | Skip PDFs that already have text |
| `REPLACE_PDF` | `false` | Upload a searchable PDF back to Paperless (see below) |

## Web UI

The service includes a built-in web interface for browsing your Paperless-NGX documents, running OCR interactively, previewing the results page-by-page, and approving them before writing back to Paperless.

### Features

- **Document browser** — paginated list with hover-zoom thumbnails, tag badges, search, and a text-status indicator (has text / no text)
- **OCR preview** — run OCR on any document and see the recognised text side-by-side with the page image; switch between page-by-page, full-text, and compare (new vs existing) tabs
- **Bounding-box overlay** — "Show Boxes" button draws every OCR text box on the page image as a semi-transparent polygon; hovering shows the recognised text for that box
- **Metadata editing** — edit Title, Date created, Correspondent, Document type, and Tags directly on the preview page; autocomplete suggests existing Paperless values; entering a name that doesn't exist **creates it automatically**
- **Approve / reject workflow** — only writes to Paperless after explicit approval; includes an optional **"Rebuild searchable PDF"** checkbox that re-runs OCR, builds a new PDF with an embedded invisible text layer, uploads it to Paperless (preserving all metadata), and deletes the original
- **Tag filtering** — hide sensitive documents by excluding specific tag IDs
- **Authentication** — none, HTTP basic auth, or OpenID Connect (Authentik, Keycloak, etc.)
- **Responsive dark / light mode** — adapts to system preference

### Enabling the Web UI

The Web UI is enabled by default (`WEB_UI_ENABLED=true`). Visit `http://localhost:9000/ui` after starting the service.

### Authentication modes

Set `WEB_UI_AUTH` to one of:

| Mode | Description |
|------|-------------|
| `none` | No authentication (default) |
| `basic` | Username / password — set `WEB_UI_USERNAME` and `WEB_UI_PASSWORD` |
| `oidc` | OpenID Connect — set the `OIDC_*` variables below (works with Authentik, Keycloak, etc.) |

> API endpoints (`/webhook`, `/ocr/*`, `/health`) are never behind auth so webhooks and automation keep working.

### Web UI Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_UI_ENABLED` | `true` | Enable the web UI |
| `WEB_UI_AUTH` | `none` | Auth mode: `none`, `basic`, or `oidc` |
| `WEB_UI_USERNAME` | `admin` | Basic-auth username |
| `WEB_UI_PASSWORD` | `""` | Basic-auth password |
| `OIDC_CLIENT_ID` | `""` | OAuth2 / OIDC client ID |
| `OIDC_CLIENT_SECRET` | `""` | OAuth2 / OIDC client secret |
| `OIDC_DISCOVERY_URL` | `""` | OIDC discovery endpoint (`.well-known/openid-configuration`) |
| `OIDC_REDIRECT_URI` | `""` | OAuth2 redirect URI (auto-detected if empty) |
| `SESSION_SECRET` | `"change-me-in-production"` | Secret key for signing session cookies |
| `WEB_UI_EXCLUDE_TAGS` | `""` | Comma-separated tag IDs to hide from the document list |

### Rebuild Searchable PDF (Web UI)

When the **"Also rebuild searchable PDF"** checkbox is ticked on the approve page, the service:

1. Re-OCRs each page of the original document at the configured DPI
2. Builds a new PDF with an invisible text layer positioned using macOCR's bounding boxes
3. Uploads the searchable PDF to Paperless-NGX (copying all metadata from the original)
4. Waits for Paperless to finish consuming the new document
5. Deletes the original document

This is the same behaviour as the automatic `REPLACE_PDF=true` webhook pipeline.

> **Note:** Set `PAPERLESS_OCR_MODE=skip` in your Paperless-NGX config to prevent the re-uploaded PDF from being re-OCR'd by Paperless's built-in engine.

## Searchable PDF Replacement (Automatic)

When `REPLACE_PDF=true`, the service doesn't just update the text content — it also builds a new PDF with an **invisible text layer** positioned using the bounding boxes returned by macOCR. This searchable PDF is uploaded to Paperless-NGX as a new document (with all metadata copied), and the **original document is deleted**.

**Why?** By default, Paperless-NGX's PDF preview uses its own OCR results for text selection and search highlighting. Replacing the document with a searchable PDF ensures the preview, text selection, and copy-paste all use the more accurate macOCR results.

**Requirements:**

- Set `PAPERLESS_OCR_MODE=skip` in your Paperless-NGX configuration so the re-uploaded PDF is not re-OCR'd by Paperless's built-in engine.
- The service automatically prevents infinite webhook loops — it tracks documents it just uploaded and skips them when the webhook fires for the newly consumed document.

## Architecture Notes

- **macOCR** requires macOS 13.0+; **iOS-OCR-Server** runs on any iPhone/iPad with iOS 16+. Both use Apple's Vision Framework.
- The Docker container runs this Python service only — the OCR backend runs separately on an Apple device.
- The service is stateless. All state lives in Paperless-NGX.
- Background tasks process OCR asynchronously so webhooks respond immediately.
- Images (JPEG, PNG, TIFF, WebP, GIF, BMP) are sent directly to the OCR server; unsupported types (emails, plain text, etc.) are skipped automatically.

## Running Tests

```bash
pip install -e ".[dev]"
pytest -v
```

## License

MIT
