# reader-api (FastAPI + UV)

Shared backend API for both:
- Web app: reader
- Mobile app: reader-app

This project is Python-first (FastAPI), with production-focused Docker setup and healthcheck.

## Stack

- Python 3.11+
- FastAPI
- UV (package manager / runner)
- PostgreSQL (metadata, user data, chapter refs; chapter text stored via NAS/files — see `NAS_CONTENT_ROOT` / storage layer)

## API Base URL

- Local dev: http://localhost:8000
- Healthcheck: GET /api/health

## Environment

Create `.env` from `.env.example`.

Required keys:

```env
DATABASE_URL=postgresql://reader:reader@localhost:5432/reader
NEXTAUTH_SECRET=replace-with-strong-secret
MOBILE_JWT_SECRET=replace-with-strong-secret
# Comma-separated allowed Google OAuth client IDs
GOOGLE_CLIENT_ID=web-client-id.apps.googleusercontent.com,android-client-id.apps.googleusercontent.com
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
APP_ENV=development
```

## Dev Setup (UV)

1. Install UV

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Sync dependencies

```bash
uv sync
```

3. Run API in dev mode

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

4. Verify health

```bash
curl http://localhost:8000/api/health
```

## Docker Compose

Current `docker-compose.yml` supports a unified deployment for both web + API.

### Web + API (use external DBs)

```bash
docker compose up -d --build api web
```

Required env for web OAuth in `.env`:

```env
WEB_GOOGLE_CLIENT_ID=web-client-id.apps.googleusercontent.com
WEB_GOOGLE_CLIENT_SECRET=replace-with-web-google-client-secret
```

### Full local stack (API local + Postgres)

```bash
docker compose --profile localdb up -d --build api-local postgres
```

Notes:
- `api` listens on port `8000` and is intended for external DB deployments.
- `api-local` listens on port `8001` and automatically points to `postgres` container.
- `web` listens on port `3000` and calls API internally through `http://api:8000`.

### NAS mount points (chapter content + EPUB source)

API containers now reserve two mount folders:

- `/data/content`: converted chapter files (`txt` + `raw_html`)
- `/data/epub-source`: source EPUB library

Default env mapping (already wired in compose):

```env
NAS_CONTENT_ROOT=/data/content
EPUB_SOURCE_ROOT=/data/epub-source
```

If you want to bind to host folders for local testing:

```yaml
services:
  api:
    volumes:
      - /absolute/local/path/content:/data/content
      - /absolute/local/path/epub-source:/data/epub-source
```

If you want to use NFS-backed docker volumes, define them under `volumes:`. Example:

```yaml
volumes:
  nas_chapter_content:
    driver: local
    driver_opts:
      type: nfs
      o: addr=100.93.79.10,nolock,soft,rw
      device: ":/volume2/apps/reader-content"

  nas_epub_source:
    driver: local
    driver_opts:
      type: nfs
      o: addr=100.93.79.10,nolock,soft,rw
      device: ":/volume2/apps/reader-epub"
```

For your EPUB structure (folder per novel, multiple `.epub` parts inside), mount the parent folder to `/data/epub-source`.

## Implemented Endpoints (snapshot)

**Public / user**

- `GET /api/health`
- `GET /api/genres`, `GET /api/genres/{slug}`
- `GET /api/novels/browse`, `GET /api/novels/{idOrSlug}`
- `GET /api/truyen` (query `slug`), `GET /api/truyen/{novel_id}/chapters`, `GET /api/truyen/{novel_id}/chapters/by-number/{n}`
- `GET /api/chapters/{chapter_id}`
- `GET /api/truyen/suggest`
- `GET/POST /api/truyen/{novel_id}/comments`, `POST /api/truyen/{novel_id}/rate`

**Auth**

- `POST /api/auth/mobile-login` (JWT cho mobile)
- `GET /api/auth/session` (session bridge cho web — xem handler trong `main.py`)

**User (login)**

- `GET /api/user/profile`
- `GET/POST /api/user/bookmarks`, `DELETE /api/user/bookmarks/{novel_id}`
- `POST /api/user/reading-progress`
- `GET/POST /api/user/settings`
- `GET/POST/DELETE /api/user/recommendations`

**MOD / ADMIN** — prefix `/api/mod/*` (thể loại, truyện, chương, overview, đề cử, upload bìa, EPUB…). Liệt kê đầy đủ trong `app/main.py`.

**Import (MOD — web)**

- `POST /api/import/uploads/preview` — upload EPUB multipart để lấy preview metadata/cover gợi ý.
- `POST /api/mod/epub`, `POST /api/mod/epub/ai-suggest` — luồng import EPUB chính.
- `GET/POST/PUT/DELETE /api/mod/the-loai` — quản lý thể loại trong wizard.

Luồng SourceAsset / `/api/import/assets/*` và job pipeline cũ đã **gỡ khỏi codebase** (không còn endpoint).

## NAS Migration Ops

### 1) Apply SQL migration manually

Run SQL in `migrations/2026_04_nas_content_storage.sql` against PostgreSQL.

### 2) Backfill existing chapter content to NAS + ChapterContentRef

Dry-run first:

```bash
python scripts/backfill_chapter_content_refs.py --limit 1000 --dry-run
```

Then execute:

```bash
python scripts/backfill_chapter_content_refs.py --limit 1000
```

You can run multiple batches by increasing/changing `--limit`.

Checkpoint/resume mode:

```bash
python scripts/backfill_chapter_content_refs.py --limit 1000 --state-file .backfill_state.json
```

## Chapter Read Cutover Flag

Set in `.env`:

```env
CHAPTER_CONTENT_MODE=nas_first
```

Values:
- `nas_first` (default): read NAS ref first.

## Notes

- Web session auth is supported via NextAuth session cookies (next-auth.session-token and secure variants).
- Mobile auth is supported via Bearer JWT from /api/auth/mobile-login.
