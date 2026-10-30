# Nhật ký làm bài

**Họ tên:** Nguyễn Hữu Tài
**Nhận đề lúc:** 27/10/2026, 16:26
**Nộp bài lúc:** 30/10/2026, 08:00
**Tổng thời gian làm thật (ước tính):** ~6 giờ — một phiên duy nhất dưới đây

## Các phiên làm việc

Một phiên duy nhất: **02:00 → 08:00 ngày 30/10/2026**.

| Thời điểm | Thời lượng | Làm phần nào | Ghi chú (kẹt ở đâu, đã thử gì) |
|---|---|---|---|
| 02:00 – 02:45 | ~45 phút | Phần 1 | Đọc syslog, nginx/php/mysql log, LB access log và snapshot on-call. Kết luận root cause **chỉ dựa trên bằng chứng trong log**: typo `dailly` trong logrotate → access log không rotate → disk full → cascade 502/504. AhrefsBot và `max_children` là nhiễu/hệ quả (5xx sớm hơn crawl). AI chỉ dùng để **verify ý nghĩa từng dòng log**, không nhờ AI suy nguyên nhân hộ. |
| 02:45 – 05:45 | ~3 giờ | Phần 2 | Research + spec ngôn ngữ tự nhiên (`phan2-tool/mo-ta-thuat-toan.md`): normalize `cpu_usage`, bỏ node offline/unknown/mem lỗi, greedy VM nặng từ node overload → đích còn headroom CPU/MEM/overcommit. Viết mã giả (README) rồi nhờ AI code; đối chiếu unittest + `cluster-pve-hcm-c3.json`, sửa lệch ý (warning khi `mem_total` lỗi, không migrate sang n04/n07). |
| 05:45 – 07:30 | ~1 giờ 45 phút | Phần 3 — **Đề B** | Đổi sang Đề B (L4 nftables vs L7 Nginx `limit_req`) vì sát vận hành shared hosting hơn Đề A. Dùng AI để **lọc / trích docs** (nginx.org `limit_req`/`limit_conn`, nftables wiki Meters & Rate limiting); **tự mở URL và đọc lại** các câu confirm trước khi đưa vào bài (leaky bucket theo key IP; meter `limit rate` per `ip saddr`; `limit rate over … drop`; `limit_conn` chỉ đếm connection đang xử lý request). Chốt khuyến nghị mô hình 2 tầng: L7 chính chống crawl, L4 trần conn/SYN. |
| 07:30 – 08:00 | ~30 phút | Phần 4 + chốt | Viết nhật ký theo mẫu, cập nhật README (Đề B, mốc 02:00–08:00), rà nhanh liên kết docs đã trích. |

## Giả định đã đặt ra

- Phần 1: timezone log là `+07` thống nhất; LB access log phản ánh đúng upstream `web-hcm-07`.
- Phần 1: truncate `access.log` và dọn backup hệ thống trong `/var/backups` là chấp nhận được khi khôi phục (không đụng data khách trong `/home` hay DB).
- Phần 2: `cpu_usage ∈ (1,100]` hiểu là phần trăm; `mem_total < 1GiB` là dữ liệu lỗi/không tin cậy.
- Phần 2: ngưỡng overload mặc định CPU/MEM 85%, overcommit 2.0x (chỉnh được qua CLI).
- Phần 3: chưa lab benchmark riêng; ngưỡng r/s và conn cụ thể phải calibrate từ access log production; sau LB phải cấu hình `real_ip` đúng trước khi key theo `$binary_remote_addr`.

## Câu hỏi đã gửi cho người ra đề

- (Chưa gửi — tự đặt giả định ở trên và ghi chú trong README/postmortem/nghiên cứu.)

## Dùng AI và công cụ

| Bước | Công cụ | Dùng để làm gì | Bạn đã kiểm tra, sửa lại gì |
|---|---|---|---|
| Điều tra Phần 1 | Cursor Agent / LLM | **Chỉ** hỏi/verify nghĩa các dòng log | Tự `grep`/đọc file gốc; tự chốt root cause; đối timeline 5xx vs Ahrefs |
| Viết postmortem | Soạn thảo có hỗ trợ AI | Cấu trúc 6 mục theo đề | Giữ trích dẫn path/log đã tự xác minh |
| Research + thiết kế Phần 2 | `mo-ta-thuat-toan.md` + mã giả README | Spec ngôn ngữ tự nhiên cho AI viết code | Checklist trong spec; so code với luồng đã mô tả |
| Code Phần 2 | LLM hỗ trợ khung CLI + test | Sinh skeleton `pve_balance.py` | Unittest + JSON thật; chỉnh normalize/warning/migrate |
| Research Phần 3 (Đề B) | AI search/trích docs + tự đọc trang official | Tìm nhanh trang nginx.org / nftables wiki và đoạn mô tả cơ chế | **Đọc verify từng dòng confirm** trước khi viết khuyến nghị; bỏ claim không có trong docs |
| Viết nhật ký / README | Soạn thảo | Ghi mốc 02:00–08:00 và cách dùng AI | Khớp với file Phần 1–3 đã nộp |

## Phần chưa xong hoặc chưa hài lòng

- Thuật toán migrate Phần 2 còn greedy; chưa mô phỏng chi phí live-migrate / local-disk / affinity.
- Phần 3 chưa có số liệu lab cá nhân (chỉ docs + mô hình triển khai).
- Chưa wire tool vào cron/API Proxmox thật (ngoài phạm vi đề).

## Nếu làm lại, bạn sẽ làm khác điều gì?

- Phần 1: giữ cách “log quyết định, AI chỉ giải nghĩa dòng”.
- Phần 2: mã giả đầy đủ hơn trước khi nhờ AI code.
