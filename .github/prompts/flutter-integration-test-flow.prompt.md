---
name: "Flutter Integration Test Flow"
description: "Use when: cần viết hoặc mở rộng Flutter integration_test cho user flow quan trọng và chống regression mobile"
argument-hint: "Nêu flow cần test, dữ liệu đầu vào, và tiêu chí thành công"
agent: "SDET Agent"
---
Tạo hoặc cập nhật Flutter integration_test cho user flow được chỉ định.

Yêu cầu thực hiện:
- Ưu tiên `integration_test` theo cấu trúc hiện có của dự án.
- Thiết kế test theo hành vi người dùng thật (đăng nhập, điều hướng, thao tác chính, trạng thái mong đợi).
- Bao gồm ít nhất một nhánh lỗi hoặc edge case có giá trị.
- Tránh test phụ thuộc dữ liệu không ổn định; thêm setup/teardown cần thiết.

Kết quả bắt buộc:
- Danh sách file test đã tạo/cập nhật.
- Lệnh chạy cụ thể (`flutter test integration_test` hoặc lệnh tương đương của repo).
- Kết quả pass/fail và nguyên nhân nếu fail.
- Đề xuất mở rộng coverage cho vòng kế tiếp.
