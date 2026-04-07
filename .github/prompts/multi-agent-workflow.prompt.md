---
name: "Multi-Agent Workflow"
description: "Use when: vận hành chu kỳ Planner -> Coder -> Tester -> Debugger cho tính năng mới hoặc bugfix"
argument-hint: "Nêu tính năng/bug cần xử lý, phạm vi web-mobile-api, và tiêu chí hoàn tất"
agent: "System Architect Agent"
---
Điều phối quy trình Multi-Agent theo chu kỳ sau, bám sát phạm vi người dùng cung cấp.

Quy trình bắt buộc:
1. Planner: dùng tư duy @workspace + plan-first để thiết kế feature/bugfix scope và các mốc triển khai.
2. Coder: triển khai đồng bộ phần Backend API và phần Web/Mobile liên quan theo contract hiện hành.
3. Tester: gọi SDET Agent để tạo/chạy test tự động.
- API: ưu tiên Postman/Newman.
- Mobile: ưu tiên Flutter integration_test.
4. Debugger: nếu crash/lỗi runtime, gọi Sleuth Debugger Agent phân tích log và đề xuất patch nhỏ nhất.

Ràng buộc:
- API phải REST-only.
- Contract chuẩn dùng duy nhất reader-api/docs/openapi.yaml.
- Báo cáo rõ tác động tới web và mobile sau mỗi thay đổi contract.

Kết quả bắt buộc:
- Kế hoạch triển khai theo bước.
- Danh sách file thay đổi theo từng pha.
- Kết quả test pass/fail.
- Root cause + patch nếu có lỗi.
- Danh sách việc còn lại trước khi merge.
