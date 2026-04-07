---
name: "Regression API Newman"
description: "Use when: cần tạo hoặc cập nhật regression suite cho API bằng Postman/Newman sau khi sửa endpoint hoặc auth flow"
argument-hint: "Nêu endpoint/flow cần cover, env chạy test, và tiêu chí pass/fail"
agent: "SDET Agent"
---
Thiết kế và triển khai regression API test theo chuẩn Postman/Newman cho phạm vi người dùng cung cấp.

Yêu cầu thực hiện:
- Ưu tiên Postman collection + environment + Newman command.
- Bám theo cấu trúc endpoint và auth flow hiện có của workspace đang mở.
- Bao phủ cả happy path, validation errors, auth errors và regression cases đã biết.
- Nếu thiếu dữ liệu test, đề xuất seed/mock tối thiểu và cách chạy lại ổn định.

Kết quả bắt buộc:
- Danh sách file đã tạo/cập nhật cho Postman/Newman.
- Lệnh chạy Newman cụ thể.
- Bảng pass/fail theo từng test case.
- Rủi ro còn lại và test nên thêm tiếp.
