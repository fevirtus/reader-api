# Đối chiếu API giữa web và mobile

Bản đối chiếu được giữ tại `reader-api`, dựa trên các call site trong code.
“Đã dùng” chỉ xác nhận tích hợp trong source, không thay thế kiểm thử end-to-end.

## Luồng chung đã tích hợp

- Login: cả hai dùng `POST /api/auth/mobile-login`; web đi qua route adapter
  `/api/auth/login` và dùng cookie, mobile dùng Bearer JWT.
- Session/profile: web dùng `GET /api/auth/session`; mobile hydrate user qua
  `GET /api/user/profile`.
- Khám phá/tìm kiếm: `/api/genres`, `/api/novels/browse`,
  `/api/novels/{id_or_slug}`.
- Mục lục: `GET /api/truyen/{novel_id}/chapters`.
- Tủ sách: `GET/POST /api/user/bookmarks`,
  `DELETE /api/user/bookmarks/{novel_id}`; có đánh dấu đã đọc.
- Rating: `GET/POST /api/truyen/{novel_id}/rate`, thang điểm 1–10.

## Khác nhau theo client

Web đọc chương theo số chương qua
`/api/truyen/{novel_id}/chapters/by-number/{chapter_number}`;
mobile đọc theo ID qua `/api/chapters/{chapter_id}`.

Web cập nhật tiến độ bằng action `updateProgress` trên bookmarks;
mobile dùng `/api/user/reading-progress`. Backend dùng chung logic ghi tiến độ
chương cho hai cách gọi. Vị trí cuộn của mobile chỉ lưu local.

Web dùng `/api/user/settings` và `/api/truyen/suggest`.
Mobile chưa gọi hai endpoint này; settings lưu local, tìm kiếm qua browse API.
Đây là hai khoảng trống so với mục tiêu tương đương tính năng người đọc.

Mobile có TTS, cache Drift/SQLite và tải nội dung để đọc offline.
Các khả năng này là phần riêng của app, không đồng nghĩa toàn bộ dữ liệu và
thao tác offline đã có cơ chế đồng bộ lại lên server.

## Chỉ có trên web theo phạm vi sản phẩm

MOD/ADMIN quản lý truyện, chương, thể loại, ảnh bìa qua `/api/mod/*`.
Import dùng `POST /api/import/uploads/preview`, `POST /api/mod/epub/ai-suggest`
và `POST /api/mod/epub`. Mobile không có giao diện quản trị/import.

## Khi thay đổi tính năng

Đối chiếu request/response, auth, lỗi và state của các client liên quan.
Không yêu cầu mọi client gọi cùng một endpoint nếu cùng nghiệp vụ đã được
backend xử lý nhất quán. Không mở lại comments hoặc recommendations dựa trên
tài liệu cũ; hai nhóm này đã bị loại khỏi phạm vi hiện tại.

Xem [contract](CONTRACT.md), [web](../reader/README.md) và
[mobile](../reader-app/README.md). Các liên kết sang client giả định checkout
ba repo cạnh nhau.
