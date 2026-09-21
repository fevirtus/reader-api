# Reader API

Backend FastAPI dùng chung cho web `reader` và ứng dụng Flutter `reader-app`.
Mục tiêu là dùng chung tài khoản, kho truyện, tủ sách và tiến độ đọc;
nghiệp vụ và phân quyền được xử lý tập trung ở API.

## Phạm vi và cấu trúc

- `app/main.py`: endpoint đọc truyện, tìm kiếm, rating, user và quản lý nội dung.
- `app/auth.py`: xác minh Google ID token, cấp JWT và xác thực cookie/Bearer token.
- `app/database.py`: PostgreSQL qua SQLAlchemy async và asyncpg.
- `app/storage.py`: đọc/ghi nội dung chương trong thư mục local hoặc NAS mount.
- `app/epub_parser.py`: phân tích EPUB; `app/deepseek.py`: gợi ý metadata bằng AI.

PostgreSQL lưu user, truyện, thể loại, rating, bookmarks, settings,
`ChapterMeta` và `ChapterContentRef`. Nội dung chương được lưu dạng text/HTML
trên file storage; ảnh bìa upload qua Cloudflare R2.

Web cung cấp giao diện MOD/ADMIN: quản lý truyện/chương/thể loại, sửa nội dung,
upload bìa và import EPUB. Mobile chỉ phục vụ người đọc.
Bình luận, đề cử và các endpoint SourceAsset/import-job cũ đã bị gỡ.

## Chạy local

Cần Python 3.11+, uv và PostgreSQL có schema phù hợp.

```bash
cp .env.example .env
uv sync
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Các cấu hình chính trong `.env`:

- `DATABASE_URL`: PostgreSQL của hệ thống Reader.
- `MOBILE_JWT_SECRET`: khóa ký access token; code dùng `NEXTAUTH_SECRET` làm fallback.
- `GOOGLE_CLIENT_ID`: danh sách OAuth client ID được phép, phân cách bằng dấu phẩy.
- `CORS_ORIGINS`: các origin gọi API trực tiếp.
- `NAS_CONTENT_ROOT`: nơi lưu nội dung chương, mặc định `./data/content`.
- `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`: cho gợi ý metadata EPUB.
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`,
  `R2_PUBLIC_BASE_URL`: cho ảnh bìa.

Healthcheck: `GET /api/health`. FastAPI cung cấp `/docs` và `/openapi.json` khi
service khởi động; chữ ký handler và Pydantic model trong `app/main.py` là nguồn
chi tiết cho query/body. Một số response chưa khai báo schema đầy đủ trong OpenAPI.

## Database và migration

Repo có [Prisma schema](prisma/schema.prisma), [Prisma migrations](prisma/migrations)
và [SQL migrations](migrations). Web hiện còn giữ một bản schema/migrations tương ứng.
SQL migrations chứa cả lịch sử của pipeline import đã bỏ; không xem tất cả các
bảng trong đó là tính năng đang được hỗ trợ.

Database mới cần cả các bảng trong Prisma schema và `ChapterMeta` /
`ChapterContentRef` của phần SQL. Hiện chưa có một lệnh bootstrap hoàn chỉnh,
an toàn cho mọi trạng thái database. Khởi động API hoặc container PostgreSQL
không tự tạo toàn bộ schema ứng dụng.

Startup luôn chạy DDL bảo đảm bảng/index `NovelRating` tồn tại.
`AUTO_SCHEMA_BOOTSTRAP` mặc định `false`. Nếu bật, startup còn chạy DDL lịch sử,
bao gồm xóa bảng/cột cũ và nhân đôi rating trong khoảng nhất định; không bật
thường trực hoặc dùng như lệnh migration lặp lại. Kiểm tra schema đang có và
chọn migration cần thiết trước khi triển khai.

## Docker và storage

Checkout `reader-api` và `reader` cạnh nhau, cấu hình `.env`, sau đó:

```bash
docker compose up -d --build api web
```

API mở cổng 8000, web mở cổng 3000. Web gọi API qua `http://api:8000`.
`WEB_GOOGLE_CLIENT_ID` trong `.env` được compose truyền sang web; backend vẫn cần
danh sách `GOOGLE_CLIENT_ID` của chính nó. Compose còn truyền các biến NextAuth
và Google client secret cũ, nhưng luồng đăng nhập web hiện tại không dùng chúng.

Để chạy API với PostgreSQL container local:

```bash
docker compose --profile localdb up -d --build api-local postgres
```

API local mở cổng 8001, PostgreSQL mở cổng 5432. Vẫn cần chuẩn bị schema như trên.
Service `web` trong compose trỏ vào `api`, không trỏ vào `api-local`; khi chạy web
ngoài Docker với cấu hình này, đặt `READER_API_ORIGIN=http://localhost:8001`.

Volume `nas_chapter_content` được mount vào `/data/content`. Có thể thay bằng
bind mount hoặc volume NFS phù hợp với hạ tầng. Compose cũng giữ mount
`/data/epub-source`, nhưng import hiện tại nhận file multipart từ web, không có
API quét thư viện SourceAsset. Không có script backfill chương được cung cấp
trong repo hiện tại.

## Luồng import EPUB

1. MOD/ADMIN chọn hoặc tạo thể loại qua `/api/mod/the-loai`.
2. Upload file tới `POST /api/import/uploads/preview` để xem metadata/ảnh bìa.
3. Có thể gọi `POST /api/mod/epub/ai-suggest` để nhận gợi ý metadata.
4. Gọi `POST /api/mod/epub` để preview cách tách chương, rồi áp dụng import.

Import ghi nội dung file, refs chương và metadata vào storage/database.
Các tham số multipart cụ thể nằm trong handler và `/docs`.

## Tài liệu tích hợp

- [API contract hiện tại](CONTRACT.md)
- [Đối chiếu web/mobile](CROSS_REPO_ENDPOINT_MATRIX.md)
- [Web](../reader/README.md) và [mobile](../reader-app/README.md), khi checkout cạnh nhau

Khi đổi nghiệp vụ hoặc contract, kiểm tra các client sử dụng trước khi release.
Giữ tương thích với client đang triển khai; thay đổi phá vỡ tương thích cần có
kế hoạch chuyển đổi rõ ràng.
