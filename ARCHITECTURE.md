# Reader Suite Architecture (API Backend)

Tai lieu nay mo ta `reader-api` la backend dung chung cho Web (`reader`) va Android (`reader-app`).

## Vai tro trung tam

- `reader-api` la single source of truth cho:
  - API contract
  - domain rule
  - auth mapping web/mobile
  - data orchestration: PostgreSQL + file/NAS chapter storage (khong con MongoDB)
- Moi thay doi contract phai uu tien backward-compatible cho 2 client.

## Domain ownership

- User domain: profile, settings, bookmarks, reading progress, recommendations.
- Content domain: genres, novels, chapter list, chapter content.
- Interaction domain: comments, ratings.

## Data strategy

- PostgreSQL:
  - user, novel metadata, genres, comments, ratings, bookmarks, progress, chapter metadata (`ChapterMeta`), refs noi dung (`ChapterContentRef`).
- Chapter body:
  - file/NAS (qua `app.storage`) — txt/html theo href trong `ChapterContentRef`.

## Auth and identity

- Web auth: session cookie (NextAuth token va secure variants).
- Mobile auth: JWT tu `/api/auth/mobile-login`.
- Backend map ca 2 co che vao cung identity va permission model.

## API compatibility policy

- Khong xoa field dang duoc client dung khi chua co migration plan.
- Them field moi theo huong optional tru khi co versioning.
- Error response phai on dinh theo format chung (code, message, details).
- Versioning uu tien uri prefix hoac contract evolution, tranh breaking ngay.

## Definition of Done (API)

- Endpoint moi co docs request/response + auth requirement.
- Da verify voi web + mobile happy path va auth edge cases.
- Healthcheck va monitoring khong bi anh huong.
- Docker/local dev van chay voi huong dan README.

## Import (MOD)

Chi con endpoint import preview `POST /api/import/uploads/preview` va nhom `/api/mod/epub*`. Cac endpoint SourceAsset/job legacy da go khoi service.
