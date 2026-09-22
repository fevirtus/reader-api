# API contract hiện tại

Tài liệu chung cho `reader`, `reader-app` và `reader-api`, được giữ tại backend.
Nội dung mô tả implementation hiện tại; không coi quy ước chưa triển khai là
cam kết của API. Chi tiết query/body nằm trong [app/main.py](app/main.py) và
OpenAPI `/docs`, `/openapi.json` khi service đang chạy.

## Base path và tương thích

Endpoint dùng `/api/*`, chưa có version prefix. Giữ các field/endpoint mà client
đang dùng; nếu đổi ý nghĩa hoặc loại bỏ phải có kế hoạch chuyển đổi client.
Response hiện khác nhau theo endpoint, không có envelope thống nhất toàn API.

## Auth

`POST /api/auth/mobile-login` được cả web và mobile sử dụng, nhận:

```json
{"googleIdToken": "<Google ID token>"}
```

Response gồm `accessToken`, `refreshToken`, `expiresIn`, `user`.
Access token là JWT HS256, thời hạn hiện tại 7 ngày. Dù response có
`refreshToken`, backend chưa có endpoint refresh token.

- Mobile gửi `Authorization: Bearer <accessToken>`.
- Web proxy luồng login và đặt cookie HttpOnly `reader_access_token`.
- Backend còn nhận tên cookie session cũ và tra bảng `Session` để tương thích.
- `GET /api/user/profile` trả object user trực tiếp.
- `GET /api/auth/session` trả `{"user": {...}}`, cần đăng nhập.
- MOD/ADMIN được kiểm tra role tại backend.

Route `/api/auth/[...nextauth]` cũ ở web đã bị vô hiệu hóa (`410`).

## Lỗi

Handler backend dùng `HTTPException`, ví dụ:

```json
{"detail": "Unauthorized"}
```

Validation mặc định của FastAPI trả `422` với `detail` là danh sách lỗi.
Các lỗi nghiệp vụ dùng `400`, `401`, `403`, `404`, `409` tùy handler;
lỗi xác minh Google do kết nối có thể trả `503`.
Không có handler chung chuyển lỗi sang `{code, message, details}`.
Proxy web có thể trả lỗi riêng; client cần dựa vào HTTP status và kiểm tra kiểu
payload, không giả định mọi lỗi đều có một chuỗi `detail` hoặc đều là JSON.

## Danh sách và phân trang

`GET /api/novels/browse` nhận `q`, `genre`, `status`, `sort`, `page`, `limit`.
`page` bắt đầu từ 1, `limit` mặc định 20 và tối đa 500. Response:

```json
{"items": [], "totalCount": 0, "totalPages": 0, "currentPage": 1}
```

`GET /api/truyen/{novel_id}/chapters` nhận `page`, `limit` (mặc định 100,
tối đa 500), trả:

```json
{"chapters": [], "totalChapters": 0, "totalPages": 0, "currentPage": 1}
```

`GET /api/genres` trả mảng trực tiếp. Không áp dụng một cấu trúc `pagination`
giả định cho các endpoint này.

## Đọc và tiến độ

- Web đọc qua `GET /api/truyen/{novel_id}/chapters/by-number/{chapter_number}`.
- Mobile đọc qua `GET /api/chapters/{chapter_id}`.
- Cả hai lấy mục lục từ `/api/truyen/{novel_id}/chapters`.
- `POST /api/user/bookmarks` hỗ trợ `markAsRead`, `unmarkAsRead`, `updateProgress`.
  Web gửi `novelId`, `lastChapterId`, `lastChapterNumber` với `updateProgress`.
- Mobile gọi `POST /api/user/reading-progress` với `novelId`, `chapterId`,
  `chapterNumber` và có thể gửi `progress`.
- Hai luồng cập nhật dùng chung logic tiến độ. `progress` được nhận nhưng hiện
  không lưu; không coi API này là đồng bộ vị trí cuộn.
- `DELETE /api/user/bookmarks/{novel_id}` trả `{"status": "removed"}`.

## Rating và settings

`GET /api/truyen/{novel_id}/rate` trả `userRating` của user hiện tại.
`POST` cùng đường dẫn nhận `score` trên thang 1–10, cập nhật một rating cho mỗi
cặp user/truyện và trả `rating`, `ratingCount`, `userRating`.
Client hiển thị bằng 5 sao, có nửa sao.

`GET/POST /api/user/settings` dùng `fontSize`, `lineHeight`, `letterSpacing`,
`fontFamily`. GET trả `{}` nếu chưa có thiết lập. Web đã dùng API này; mobile
hiện lưu thiết lập local.

## Quản trị và import

Các endpoint `/api/mod/*` và `POST /api/import/uploads/preview` yêu cầu
MOD/ADMIN. Upload EPUB/bìa dùng multipart. Preview upload, gợi ý AI và import
là các request riêng; không có contract job/session theo dõi tiến độ import.
Xem [luồng import](README.md#luồng-import-epub) và handler để biết tham số.

## Endpoint đã gỡ

Không còn comments, user/editor recommendations, `/api/mod/truyen/missing`
hoặc `/api/import/assets/*`. Không dùng những endpoint này cho tính năng mới.

## Đồng bộ ngoại tuyến của mobile

Triển khai API có các endpoint dưới đây trước khi phát hành app hỗ trợ sync.

`POST /api/user/sync` yêu cầu Bearer token và nhận một thao tác:

```json
{
  "eventId": "UUID-cua-thao-tac",
  "kind": "progress",
  "novelId": "novel-id",
  "chapterId": "chapter-id",
  "chapterNumber": 30,
  "occurredAt": "2026-09-22T08:00:00Z",
  "progress": 120.0
}
```

- `kind`: `progress`, `markAsRead`, hoặc `remove`. Hai loại cuối không cần chương.
- Backend lấy số chương chính thức theo ID và xác minh chương thuộc truyện.
- Lịch sử chương đã đọc được hợp nhất dưới khóa transaction theo tài khoản/truyện.
  Vị trí đọc và thao tác tủ sách chọn theo `occurredAt`; cùng thời điểm dùng
  `eventId` để phân định. Không mặc định chọn chương lớn hơn vì có thể đang đọc lại.
- Thời gian tương lai được chặn ở thời gian server. Chính sách này giả định đồng
  hồ thiết bị tương đối đúng; timestamp không chứng minh thứ tự thực ngoài đời.
- `remove` giữ tombstone để cập nhật offline cũ không khôi phục bookmark đã xóa.
  Endpoint online cũ của web cũng ghi clock, nên tham gia cùng quy tắc.
- Response chứa `acknowledgedEventId`, `status`, `bookmark` (nullable).
  Client chỉ xóa thao tác khỏi outbox khi ID được xác nhận chính xác.
- `chapter_deleted` / `novel_deleted` là kết quả cuối cùng được xác nhận;
  client nhận lại bookmark còn hợp lệ. Lỗi transport/HTTP không được coi là ACK.
- Offset cuộn chỉ lưu theo tài khoản trên thiết bị; không áp dụng pixel offset
  giữa các thiết bị có font/kích thước màn hình khác nhau.

Startup tạo bổ sung bảng `ReaderSyncClock` nếu chưa có, không bật
`AUTO_SCHEMA_BOOTSTRAP` và không thay đổi bảng nội dung cũ.

## Phiên bản bản tải

`GET /api/novels/{novel_id}/download-manifest` trả `novelId`, `revision`,
`chapters` gồm `id`, `number`, `title`, `contentHash` (SHA-256 nội dung UTF-8).
Revision bao gồm metadata và checksum nên thay đổi khi thêm, sửa, xóa hoặc đổi
thứ tự chương.

App ưu tiên snapshot đã tải cho cả nội dung và mục lục. Chỉ nút **Cập nhật**
mới thay snapshot: tải vào vùng tạm, kiểm tra checksum từng chương và kiểm tra
lại revision, rồi commit nội dung/điều hướng/trạng thái cùng một transaction.
Có thay đổi trong lúc tải hoặc mất mạng: giữ snapshot cũ và vùng tạm để thử lại.
Không trộn cache đọc online vào snapshot. Bản tải cũ vẫn đọc được nếu chương
bị gỡ trên server; cập nhật thành công sẽ loại chương không còn trong manifest.

Kiểm thử API với PostgreSQL dùng riêng cho test:
```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:password@127.0.0.1:55439/reader_sync_test \
  python -m unittest discover -s tests -v
```
Suite chỉ chấp nhận database local tên `reader_sync_test` và tạo lại bảng test;
không chạy với database có dữ liệu cần giữ.
