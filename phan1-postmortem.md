# Postmortem: web-hcm-07 — 502/504 hàng loạt (20/10/2026)

## 1. Tóm tắt sự cố (Executive Summary)

Rạng sáng 20/10/2026, hàng loạt website trên node shared hosting `web-hcm-07` (Nginx + PHP-FPM 8.1 + MySQL 8) trả 502/504 qua LB `lb-hcm-01`. Snapshot on-call 02:23 cho thấy `/` đầy 100% (`snapshot/df-h.txt:6`). Root cause: typo `dailly` trong logrotate Nginx từ 13/10 (`snapshot/logrotate-nginx.conf:3`) → access log không rotate, phình ~37G (`snapshot/du-var.txt:10`). Job backup 01:00 đêm 20/10 đẩy dung lượng về 0; MySQL không ghi binlog (errno 28); PHP-FPM kẹt → LB 502/504 diện rộng.

## 2. Timeline sự cố

| Thời điểm | Sự kiện | Bằng chứng (file:dòng) |
|---|---|---|
| 13/10 17:42–17:44 | `hieunv` sửa logrotate Nginx, reload | `syslog:37` vim `/etc/logrotate.d/nginx`; `syslog:38` reload nginx |
| 14/10 00:00 | logrotate lỗi `dailly`, skip toàn bộ nginx log | `syslog:43–44` (`dailly` / skipping); lặp `syslog:67–68,85–86,105–106,125–126,145–146,165–166` |
| 14→19/10 | Disk 57%→82% (WARN) | `syslog:48` 57%; `syslog:149` WARN 82% |
| 20/10 00:30 | Disk WARN 87% | `syslog:169` |
| 20/10 01:00–01:32 | Backup bắt đầu; tar fail “Wrote only 4096 of 10240 bytes” | `syslog:171` start; `syslog:173–175` tar fail + keep old |
| 20/10 01:32:59–01:33:01 | Nginx ENOSPC body/access.log | `nginx/error.log:2–3` |
| 20/10 01:33:04 | MySQL disk full ghi `binlog.000187` | `syslog:176`; `mysql/error.log:2` |
| 20/10 01:33:05 | 5xx đầu trên LB (504) | `lb/access-lb-hcm-01.log:1569` |
| 20/10 01:33:06+ | PHP session ENOSPC; `pm.max_children=50`; mysqli greeting lỗi | `php/php8.1-fpm.log:2–4` |
| 20/10 01:41:03 | AhrefsBot crawl `tintuc24h.net` — **sau** disk full | `lb/access-lb-hcm-01.log:1863` (1259 req Ahrefs, toàn 5xx) |
| 20/10 02:21–02:23 | On-call SSH; `/` 100%; access.log 37G; 49 thread “Waiting for” | `syslog:183`; `snapshot/df-h.txt:6`; `du-var.txt:10`; `ss-summary.txt:13–14` |

## 3. Phân tích nguyên nhân gốc & chuỗi hệ quả

**Root cause:** typo logrotate Nginx → log không rotate → `access.log` ~37G → `/` cạn khi backup chạy.

```text
# snapshot/logrotate-nginx.conf:2–3
/var/log/nginx/*.log {
	dailly          # ← sai; đúng là "daily"
```

```text
# syslog:43–44 (lặp mỗi ngày 14→20/10)
logrotate: error: /etc/logrotate.d/nginx:3 unknown option 'dailly' -- ignoring line
logrotate: error: found error in /var/log/nginx/*.log , skipping
```

```text
# snapshot/du-var.txt:10,17–18
37G  access.log          (mtime Oct 20 01:33; access.log.1 dừng Oct 13 — :11)
19G  sites-20261019.tar.gz
16G  sites-20261020.tar.gz   # bản dở + giữ bản cũ vì chỉ rm khi success
```

**Chuỗi hệ quả:**

1. Disk full → Nginx không ghi temp body / access log — `nginx/error.log:2–3` (errno 28).
2. MySQL block binlog → connection treo — `mysql/error.log:2`; `ss-summary.txt:14` (=49 Waiting for).
3. PHP session ENOSPC — `php/php8.1-fpm.log:2`.
4. FPM hết worker — `php/php8.1-fpm.log:3` (`pm.max_children`); Nginx `Resource temporarily unavailable` / `upstream timed out` — `nginx/error.log:4–5` → 502/504.
5. LB: trước 01:33 hầu hết 200; sau đó 502=1231 + 504=1544 / ~4718 dòng cửa sổ log. First 5xx: `lb/access-lb-hcm-01.log:1569`.

`backup-sites.sh` là **cò súng** (trigger), không phải root: chỉ `rm` bản cũ khi tar OK (`snapshot/backup-sites.sh:8–13`) → fail vẫn giữ bản cũ + partial → xấu thêm đĩa (`syslog:175`).

## 4. Các giả thuyết đã loại trừ

| Giả thuyết | Vì sao loại | Bằng chứng |
|---|---|---|
| DDoS / AhrefsBot là gốc | Bot **sau** disk full; trước 01:33 không có Ahrefs | First 5xx `:1569` @01:33:05; first Ahrefs `:1863` @01:41:03 |
| PHP-FPM thiếu `max_children` | Triệu chứng worker block; CPU/RAM dư | `uptime-top.txt:7` idle ~95%; `free-m.txt:3` avail ~22G; FPM warn chỉ sau 01:33 (`php8.1-fpm.log:3`) |
| MySQL hỏng / query chậm là gốc | Rõ errno 28 disk full; processlist chờ free space | `mysql/error.log:2`; `ss-summary.txt:14` |
| Brute-force SSH / scan port | Có từ nhiều ngày trước, không khớp cửa sổ 502 | `syslog` Invalid user / UFW BLOCK rải 12–20/10 (vd `:41`, `:179`) |
| OOM / thiếu RAM | available ~22G, swap gần trống | `free-m.txt:3–4`; `uptime-top.txt:7–9` |
| Chỉ riêng site `shop` chậm | Hệ quả MySQL treo; 502/504 đa site | `nginx/error.log:4+` (site42, tintuc24h, noithatmoc…); LB host đa dạng |

## 5. Khắc phục tức thời (tại 02:23)

**Nguyên tắc:** không xóa/sửa data, database, source code khách (`/home`, `/var/lib/mysql` data dir).

```bash
# 1) Xác nhận lại
df -h /
du -xh --max-depth=1 /var | sort -h

# 2) Giải phóng đĩa an toàn (log + backup dở hệ thống)
:> /var/log/nginx/access.log
# hoặc: truncate -s 0 /var/log/nginx/access.log
rm -f /var/backups/sites-20261020.tar.gz
# Nếu vẫn thiếu chỗ: cân nhắc xóa sites-20261019.tar.gz (RPO −1 ngày; backup hệ thống, không phải data khách)

# 3) Sửa logrotate
sed -i 's/\bdailly\b/daily/' /etc/logrotate.d/nginx
logrotate -d /etc/logrotate.d/nginx
logrotate -f /etc/logrotate.d/nginx

# 4) MySQL tự resume sau khi có chỗ; reload FPM nếu worker còn kẹt
mysqladmin processlist | head
systemctl reload php8.1-fpm

# 5) Xác nhận
df -h /
curl -sI https://<site> | head
```

**Không làm:** tăng `pm.max_children` khi đĩa vẫn đầy; `rm` trong `/home`; force recovery InnoDB trừ khi có approve; restart MySQL “cho chắc” khi chỉ cần free space.

## 6. Phương án phòng ngừa

1. **Giám sát đĩa:** alert `/` ≥ 80% WARN / ≥ 90% CRITICAL; riêng `/var/log`, `/var/backups`. Zabbix item `vfs.fs.size[/,pfree]` fail khi hết đĩa (`syslog:177`) — cần alert khi agent item unsupported / mất heartbeat.
2. **Kiểm tra logrotate:** cron nightly `logrotate -d` + alert exit ≠ 0; validate config trong pipeline CM.
3. **Tách volume:** mount riêng `/var/log` và `/var/backups` (hoặc backup ra NFS/object storage).
4. **Backup script:** preflight `df`; staging rồi `mv`; dọn partial khi fail.
5. **Change management:** sửa `/etc/logrotate.d/*` cần review + dry-run trước merge.

---