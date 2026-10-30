# Nghiên cứu: Chặn / rate-limit bot crawl — L4 (nftables) vs L7 (Nginx `limit_req`)

**Bài toán:** máy shared hosting bị bot crawl dồn dập làm chậm cả hệ thống. So sánh chặn/rate-limit ở tầng L4 (`nftables`) với tầng L7 (Nginx `limit_req` hoặc tương đương) và đề xuất mô hình triển khai.

**Giả định:** không có lab benchmark riêng trên cùng hardware; kết luận dựa trên tài liệu official + mô hình vận hành hosting nhiều site trên một Nginx.

## Tiêu chí so sánh

| Tiêu chí | L4 — nftables | L7 — Nginx `limit_req` (+ `limit_conn`) |
|---|---|---|
| Đơn vị giới hạn | Gói / kết nối TCP (SYN mới, packet/s, byte/s, số conn theo `ct`) | Request HTTP theo key (thường IP), có burst / delay |
| Nhìn thấy gì | IP, port, flags, conntrack — **không** thấy URI, User-Agent, vhost | URI, vhost, header, status — khớp hành vi crawl theo path |
| Chi phí CPU trước app | Drop sớm ở kernel, PHP-FPM/MySQL chưa chạy | Request đã vào Nginx worker; vẫn rẻ hơn mở PHP nhưng đắt hơn drop L4 |
| Độ chính xác với bot “lịch sự” | Dễ nhầm NAT / CDN / IP dùng chung nhiều site | Fine-tune theo `location`, zone per-IP + per-server |
| Phù hợp kiểu tấn công | SYN flood, mở quá nhiều conn, flood packet | Crawl HTTP dày request trên ít/conn ổn định |

## Bằng chứng từ docs (đã đọc lại dòng confirm)

### Nginx L7 — `limit_req` (leaky bucket theo request)

Docs official xác nhận module giới hạn **tốc độ xử lý request theo key** (ví dụ một IP), thuật toán leaky bucket:

> The `ngx_http_limit_req_module` module … is used to limit the request processing rate per a defined key, in particular, the processing rate of requests coming from a single IP address. The limitation is done using the “leaky bucket” method.

Nguồn: [nginx.org — ngx_http_limit_req_module](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)

Ví dụ cấu hình chuẩn trong cùng trang docs (`limit_req_zone` + `limit_req` với `burst`):

```nginx
http {
    limit_req_zone $binary_remote_addr zone=one:10m rate=1r/s;
    server {
        location /search/ {
            limit_req zone=one burst=5;
        }
    }
}
```

Docs cũng cho phép **nhiều zone cùng lúc** (per-IP + per-vhost) — hữu ích trên shared hosting để một site/crawl không nuốt hết ngân sách toàn máy:

> … limit the processing rate of requests coming from a single IP address and, at the same time, the request processing rate by the virtual server

Default khi reject: `limit_req_status 503` (có thể đổi, ví dụ 429).

Bổ sung đồng thời: [ngx_http_limit_conn_module](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html) giới hạn **số kết nối đang xử lý request** theo key (không tính idle keepalive):

> The `ngx_http_limit_conn_module` module is used to limit the number of connections per the defined key, in particular, the number of connections from a single IP address.  
> Not all connections are counted. A connection is counted only if it has a request being processed by the server and the whole request header has already been read.

### L4 — nftables meter / `limit rate`

Wiki nftables xác nhận meter + dynamic set gắn **ratelimit theo source IP** (thay hashlimit/connlimit của iptables):

> If you specify the dynamic flag to your set/map declaration, you can add elements to your set/map from the packet path. You can also attach a ratelimit per byte/packet/connection…  
> … flexible replacement for the hashlimit and connlimit matches in iptables.

Ví dụ official rate-limit kết nối mới theo IP:

```nft
ct state new tcp dport 22 update @my_ssh_ratelimit { ip saddr limit rate 3/minute } accept
```

Nguồn: [nftables wiki — Meters](https://wiki.nftables.org/wiki-nftables/index.php/Meters)

Giới hạn packet vượt ngưỡng bằng `limit rate over … drop`:

> `% nft add rule filter input icmp type echo-request limit rate over 10/second drop`  
> … `over` specifies that the rule is matching packets over the rate limit, and those packets will be dropped.

Nguồn: [nftables wiki — Rate limiting matchings](https://wiki.nftables.org/wiki-nftables/index.php/Rate_limiting_matchings)

`ct count over N` (cùng trang Meters) giới hạn số connection đang track theo IP — lớp bảo vệ conn, không phải “request HTTP/giây”.

## Phân tích cho shared hosting

1. **Bot crawl điển hình** giữ TCP ổn định rồi bắn nhiều GET: L4 theo conn/packet có thể **không đủ** hoặc phải đặt ngưỡng lỏng → vẫn làm đầy PHP-FPM. L7 `limit_req` đúng đơn vị “request”.
2. **IP dùng chung / NAT / VPN / cloud egress:** L4 cứng dễ ảnh hưởng khách lành trên cùng IP; L7 có thể nới theo `location` (ví dụ siết `/wp-login.php`, `/xmlrpc.php`, feed) và dùng `limit_req_dry_run` để đo trước khi enforce.
3. **Flood kết nối / SYN / mở hàng nghìn conn:** L7 muộn — worker đã accept. L4 meter + `ct count` (và synproxy/syncookies nếu cần) bảo vệ tài nguyên kernel/conntrack trước.
4. **Chi phí vận hành:** `limit_req` nằm sẵn stack Nginx hosting; nftables cần quy trình thay rule an toàn (tránh lock mình), và **không** map được “bot theo User-Agent / path” nếu chỉ đứng L4.

## Khuyến nghị mô hình triển khai

**Chọn mô hình 2 tầng, L7 là lớp chính chống crawl; L4 là lớp bảo vệ nền.**

| Lớp | Vai trò | Gợi ý cấu hình khởi điểm (cần tune theo traffic thật) |
|---|---|---|
| L7 (bắt buộc) | Rate-limit request + concurrent request | `limit_req_zone` theo `$binary_remote_addr` (vd. 8–20 r/s + burst); thêm zone theo `$server_name` để trần per-vhost; `limit_conn` per-IP; siết mạnh `location` nhạy cảm; bật `limit_req_dry_run` vài ngày, đọc `$limit_req_status` / log trước khi tắt dry-run |
| L4 (bắt buộc nhẹ) | Trần conn / SYN mới per-IP vào :80/:443 | nftables meter `ct state new` + `limit rate` vừa phải; `ct count over` để chặn IP mở quá nhiều conn; **không** đặt packet/s quá thấp trên IP dùng chung |
| Bổ sung (tuỳ chọn) | Giảm tải app | Cache tĩnh, chặn UA bot đã biết (chỉ khi chắc), Fail2ban/crowdsec gắn log `limit_req`, CDN/WAF trước máy nếu bot phân tán nhiều IP |

**Trade-offs / rủi ro:**

- Chỉ L7: máy vẫn có thể ngộp conntrack/SYN trước khi Nginx kịp trả 503.
- Chỉ L4: khó phân biệt crawl HTTP “đúng giao thức”; dễ false-positive NAT; không bảo vệ đủ khi bot giữ ít conn nhưng request cực dày.
- Shared hosting sau LB: key L7 phải dùng IP thật (`$binary_remote_addr` sau `real_ip` / `X-Forwarded-For` tin cậy) — nếu không, mọi khách sau LB bị gộp một key.
- Chưa có số liệu lab cá nhân: ngưỡng cụ thể phải calibrate từ access log (p95 request/s per IP hợp lệ) trước khi siết production.

## Nguồn đã đối chiếu

- Nginx `limit_req`: https://nginx.org/en/docs/http/ngx_http_limit_req_module.html
- Nginx `limit_conn`: https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html
- nftables Meters: https://wiki.nftables.org/wiki-nftables/index.php/Meters
- nftables Rate limiting: https://wiki.nftables.org/wiki-nftables/index.php/Rate_limiting_matchings
