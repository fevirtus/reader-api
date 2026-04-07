---
name: "Debug Triage Checklist"
description: "Use when: cần phân tích nhanh lỗi runtime từ logcat/server logs, xác định root cause và đề xuất patch an toàn"
argument-hint: "Dán log, stack trace, bước tái hiện, và môi trường bị lỗi"
agent: "Sleuth Debugger Agent"
---
Thực hiện debug triage theo checklist chuẩn cho lỗi runtime được cung cấp.

Checklist bắt buộc:
- Trích xuất error signature chính và stack trace quan trọng nhất.
- Xác định giả định tái hiện lỗi và điều kiện kích hoạt.
- Khoanh vùng root cause bằng bằng chứng từ log + code path.
- Đề xuất patch nhỏ nhất có thể áp dụng ngay.
- Đưa kế hoạch verify sau fix với expected behavior rõ ràng.

Kết quả bắt buộc:
- Error signature.
- Root cause theo chuỗi nhân quả.
- File/symbol bị ảnh hưởng.
- Patch proposal cụ thể.
- Verification checklist.
- Guardrails phòng tái phát (test/logging/alerts).
