# Project Guidelines

## Code Style
- Backend này là Python-first (FastAPI). Ưu tiên sửa/chạy bằng `uv` và toolchain trong `pyproject.toml`.
- Tuân thủ format/lint với Ruff: line-length 100, import order rõ ràng.
- Viết async đúng chuẩn FastAPI/SQLAlchemy async; tránh trộn sync blocking call trong request path.
- Khi thêm endpoint, luôn trả lỗi có chủ đích bằng `HTTPException` và message rõ nghĩa.

## Architecture
- `app/main.py`: khởi tạo FastAPI app, middleware/CORS, health và endpoint user-facing.
- `app/auth.py`: xác thực JWT/session cookie và kiểm tra role (`USER`/`MOD`/`ADMIN`).
- `app/database.py`: kết nối PostgreSQL (SQLAlchemy async) + MongoDB (motor).
- `app/routers/mod.py`: nhóm route cho MOD/ADMIN.
- `prisma/schema.prisma`: schema dữ liệu quan hệ chia sẻ với web.
- Dữ liệu chia 2 lớp: PostgreSQL cho metadata quan hệ, MongoDB cho chapter content/recommendation content.

## Build and Test
- Cài dependencies: `uv sync`
- Chạy dev server: `uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
- Health check nhanh: `curl http://localhost:8000/api/health`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`
- Docker stack: xem `docker-compose.yml` (`api`, `api-local`, `postgres`, `mongo`, `web`)

## Conventions
- Ưu tiên dependency injection qua `Depends(...)` cho auth/db session.
- Kiểm tra ownership/role trước thao tác ghi dữ liệu, đặc biệt ở mod routes.
- Với nghiệp vụ ghi đồng thời nhiều nguồn (PostgreSQL + MongoDB), xử lý lỗi từng bước rõ ràng để tránh trạng thái nửa thành công.
- Repository này có `package.json` không phản ánh backend Python hiện tại; dùng lệnh trong `README.md` và `pyproject.toml` làm chuẩn.

## Pitfalls
- `DATABASE_URL` từ Prisma có query params; cần normalize đúng cho asyncpg (đã xử lý trong `app/database.py`).
- Thiếu `.env` hoặc secret (`NEXTAUTH_SECRET`, `MOBILE_JWT_SECRET`, `GOOGLE_CLIENT_ID`) sẽ làm auth flow lỗi.
- Sai CORS origin sẽ gây lỗi cross-origin cho web/mobile local.
- Khi chạy local stack bằng Docker profile, lưu ý khác biệt cổng giữa `api` (8000) và `api-local` (8001).

## Key References
- Setup, env, endpoint matrix: `README.md`
- Debug chapter save: `CHAPTER_SAVE_DEBUG.md`
- Các bản vá quan trọng: `FIXES_APPLIED.md`
- App bootstrap: `app/main.py`
- Auth/session logic: `app/auth.py`
- DB wiring: `app/database.py`
- Mod routes: `app/routers/mod.py`
