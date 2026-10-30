# Bài test RnD Vietnix — Infrastructure & Automation

**Ứng viên:** Nguyễn Hữu Tài
**Email nộp:** rnd@vietnix.vn  

## Cấu trúc

```
bai-nop/
  README.md                 # file này
  phan1-postmortem.md       # điều tra sự cố web-hcm-07
  phan2-tool/               # tool + snapshot JSON + unit test
    cluster-pve-hcm-c3.json
  phan3-nghien-cuu.md       # Đề B: nftables (L4) vs Nginx limit_req (L7)
  phan4-nhat-ky.md          # nhật ký làm bài
```

Snapshot Proxmox Phần 2 nằm trong `phan2-tool/cluster-pve-hcm-c3.json`. Dữ liệu điều tra Phần 1 vẫn tham chiếu từ `../RnD-Test-Vietnix/phan1-su-co/` (không copy lại vào bài nộp).

## Cách chạy tool Phần 2

```bash
cd phan2-tool
python3 pve_balance.py cluster-pve-hcm-c3.json
python3 pve_balance.py cluster-pve-hcm-c3.json --format json
python3 -m unittest discover -s tests -v
```

Chi tiết ngưỡng / giả định: `phan2-tool/README.md`.

## Giả định chung

- Đã nêu chi tiết trong từng file phần và `phan4-nhat-ky.md`.
- Khi đề không chốt ngưỡng overload / đơn vị `cpu_usage` lệch, chọn phương án có warning rõ thay vì im lặng bỏ qua.

## Lời cảm ơn

Bài làm trong khung giờ có hạn nên phần research còn sơ sài. Rất mong nhận góp ý công tâm từ các sếp, các anh khi review để em chỉnh lại cho chắc hơn.

Cảm ơn Cursor AI đã hỗ trợ tra docs và dựng khung code (mọi kết luận vẫn tự đối chiếu bằng chứng). Cảm ơn các sếp, các anh đã dành thời gian xem bài nộp.
