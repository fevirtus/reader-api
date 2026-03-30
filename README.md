# reader-api (FastAPI + UV)

Shared backend API for both:
- Web app: reader
- Mobile app: reader-app

This project is Python-first (FastAPI), with production-focused Docker setup and healthcheck.

## Stack

- Python 3.11+
- FastAPI
- UV (package manager / runner)
- PostgreSQL (structured data)
- MongoDB (chapter content + user recommendations)

## API Base URL

- Local dev: http://localhost:8000
- Healthcheck: GET /api/health

## Environment

Create `.env` from `.env.example`.

Required keys:

```env
DATABASE_URL=postgresql://reader:reader@localhost:5432/reader
MONGODB_URI=mongodb://localhost:27017/reader
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

### Full local stack (API local + Postgres + Mongo)

```bash
docker compose --profile localdb up -d --build api-local postgres mongo
```

Notes:
- `api` listens on port `8000` and is intended for external DB deployments.
- `api-local` listens on port `8001` and automatically points to `postgres` + `mongo` containers.
- `web` listens on port `3000` and calls API internally through `http://api:8000`.

## Implemented Endpoints

- GET /api/health
- POST /api/auth/mobile-login
- GET /api/user/profile
- GET/POST /api/user/bookmarks
- DELETE /api/user/bookmarks/{novelId}
- POST /api/user/reading-progress
- GET/POST /api/user/settings
- GET/POST/DELETE /api/user/recommendations
- GET /api/genres
- GET /api/novels/browse
- GET /api/novels/{idOrSlug}
- GET /api/truyen/{id}/chapters
- GET/POST /api/truyen/{id}/comments
- POST /api/truyen/{id}/rate
- GET /api/truyen/suggest
- GET /api/chapters/{chapterId}

## Notes

- Web session auth is supported via NextAuth session cookies (next-auth.session-token and secure variants).
- Mobile auth is supported via Bearer JWT from /api/auth/mobile-login.
