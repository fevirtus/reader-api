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
