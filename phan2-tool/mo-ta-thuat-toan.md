# Mô tả thuật toán (ngôn ngữ tự nhiên) — input cho AI viết code

Tài liệu này ghi lại logic mong muốn **trước khi** nhờ AI sinh `pve_balance.py`. Dùng để đối chiếu: code chạy đúng ý hay lệch.

---

## Bài toán cần giải

Đọc một file JSON snapshot cluster Proxmox (`nodes` + `vms`). Tool phải:

1. Tính mức tải từng node (CPU thực dùng, RAM thực dùng, overcommit vCPU).
2. Đánh dấu node **quá tải** theo ngưỡng có thể chỉnh CLI.
3. Đề xuất live-migrate: VM nào từ node nào sang node nào, sao cho sau chuyển không đẩy node đích vào quá tải.
4. In ra bảng terminal hoặc JSON; dữ liệu bẩn thì **cảnh báo**, không crash.

Không gọi API Proxmox. Chỉ phân tích file tĩnh. Chỉ VM đang `running` mới góp vào tải.

---

## Ngưỡng mặc định (giả định vì đề không chốt)

- CPU quá tải khi: tổng “core tương đương” dùng trên node / số core vật lý ≥ **85%**.
- RAM quá tải khi: tổng `mem_used` của VM running / `mem_total` node ≥ **85%**.
- Overcommit quá tải khi: tổng `vcpus` của VM running / `cpu_cores` ≥ **2.0**.

Người dùng có thể đổi ba ngưỡng này bằng tham số dòng lệnh.

---

## Cách hiểu từng con số

### CPU thực dùng của một VM trên host

Theo đề, `cpu_usage` từ 0 đến 1 là phần sử dụng trên số vCPU đã cấp. Đóng góp lên host ≈ `cpu_usage × vcpus` (đơn vị core tương đương).

**Dữ liệu bẩn gặp trong snapshot:** một số bản ghi ghi `cpu_usage` dạng phần trăm (31, 98, 20.8…). Quy ước:

- Nếu giá trị nằm trong `[0, 1]` → dùng nguyên.
- Nếu `> 1` và `≤ 100` → coi là %, chia 100, **ghi warning**.
- Ngoài khoảng đó hoặc không phải số → **bỏ VM khỏi phép tính tải**, ghi warning (không ném exception).

### RAM và overcommit

- Cộng `mem_used` của mọi VM running trên node.
- Overcommit = tổng `vcpus` running / `cpu_cores` (không nhân với `cpu_usage`).

### Node không đủ điều kiện nhận migrate

- `status != online` (ví dụ `offline`) → bỏ qua khi chọn đích; không tính overload kiểu “cần giải phóng bằng migrate”.
- `mem_total` nhỏ bất thường (**< 1 GiB**, ví dụ `262144`) → coi metrics **không tin cậy**: warning, không nhận VM migrate, phần trăm RAM có thể hiện n/a.
- Node không tồn tại trong danh sách `nodes` nhưng VM vẫn trỏ tới (ví dụ `hcm-c3-n07`) → warning `unknown_node`, bỏ VM đó khỏi tính tải / migrate.

Tóm lại: **không migrate sang n04 (offline), n06 (mem lỗi), n07 (không tồn tại), hay bất kỳ node không online + không tin cậy.**

---

## Luồng xử lý mong muốn (từ trên xuống)

### Bước 1 — Đọc JSON an toàn

Đọc file → parse JSON. Root phải là object, có `nodes` và `vms` là array. Sai thì thoát với thông báo rõ, không traceback khó đọc.

### Bước 2 — Lập chỉ mục node

Với mỗi node: lấy tên, status, `cpu_cores`, `mem_total`. Thiếu trường / kiểu sai → warning. Đánh dấu `mem_sane` khi mem ≥ 1 GiB và cores > 0.

### Bước 3 — Cộng dồn tải từ VM

Duyệt từng VM:

- Không phải object / thiếu field quan trọng → warning, bỏ qua.
- `node` không có trong chỉ mục → warning unknown_node, bỏ qua.
- `status != running` → bỏ qua (không cộng tải).
- Normalize `cpu_usage` như trên; `vcpus` / `mem_used` lỗi → warning, bỏ qua.
- Cộng đóng góp CPU/RAM/vCPU vào accumulator của node nguồn; giữ danh sách VM running đã chuẩn hóa trên node đó (phục vụ bước migrate).

### Bước 4 — Đánh overload từng node

Với mỗi node online và tin cậy:

- `cpu_pct = cpu_used_cores / cpu_cores`
- `mem_pct = mem_used / mem_total`
- `overcommit = vcpu_alloc / cpu_cores`

Node **overloaded** nếu bất kỳ chỉ số nào ≥ ngưỡng tương ứng. Ghi rõ lý do (cpu / mem / overcommit).

Node offline hoặc metrics không tin cậy: không coi là ứng viên nguồn/đích migrate kiểu bình thường; gắn cờ SKIP / lý do tương ứng trên báo cáo.

### Bước 5 — Lập kế hoạch migrate (greedy)

Đây là phần “thuật toán cân bằng” — **không** tối ưu toàn cục, chỉ greedy đủ dùng snapshot vận hành.

1. Lấy các node nguồn đang overloaded, **xử lý node nặng nhất trước** (theo `max(mem_pct, cpu_pct)` giảm dần).
2. Trên mỗi nguồn: sắp VM running **nặng trước** (ưu tiên `mem_used` cao, rồi CPU đóng góp cao).
3. Với từng VM, tìm node đích tốt nhất thỏa **tất cả**:
   - Khác node nguồn.
   - Online và metrics tin cậy.
   - Sau khi **giả lập nhận** VM: `cpu_pct`, `mem_pct`, `overcommit` đều **strict dưới ngưỡng** (không đẩy đích vào overload).
4. Nếu nhiều đích hợp lệ: chọn đích còn **nhiều headroom RAM nhất** sau khi nhận  
   (`(mem_threshold × mem_total − mem_sau) / mem_total`).
5. Không tìm được đích → bỏ VM đó, thử VM khác trên cùng nguồn.
6. Khi chốt một migrate: cập nhật bản sao tải (trừ nguồn, cộng đích) rồi tiếp tục. Nếu nguồn đã hết overload thì chuyển sang nguồn khác.
7. Nếu sau khi vét VM mà nguồn vẫn overload → warning kiểu “thiếu headroom cluster, không cân bằng đủ”.

### Bước 6 — Mô phỏng residual

Áp lại toàn bộ migration đã đề xuất lên bản sao tải; liệt kê node nào **vẫn** còn trên ngưỡng (residual). Giúp người đọc biết kế hoạch greedy chưa xong việc.

### Bước 7 — Output

- Bảng: cluster, ngưỡng, từng node + cờ OVERLOAD/SKIP/ok, danh sách migrate, danh sách warning.
- JSON: cùng thông tin, ổn định để pipe script khác.

---

## Ràng buộc cứng khi AI viết code (checklist đối chiếu)

- [ ] Normalize `cpu_usage` % → warning, không crash.
- [ ] `mem_total` lỗi → warning + không nhận migrate; tool vẫn chạy.
- [ ] Không đề xuất migrate sang node offline / unknown / metrics_untrusted.
- [ ] Chỉ VM `running` vào tải và vào kế hoạch migrate.
- [ ] Greedy: nguồn nặng → VM nặng → đích headroom RAM lớn nhất mà không vượt 3 ngưỡng.
- [ ] Có unit test cho normalize, overload, không chọn đích xấu, JSON lỗi.
- [ ] Chạy được trên `cluster-pve-hcm-c3.json` thật và in warning cho các case bẩn đã biết.

---

## Việc cố ý chưa làm (ngoài scope lần này)

- Chi phí live-migrate, local disk, affinity / anti-affinity.
- Tối ưu ILP / min-cost flow toàn cục.
- Gọi API Proxmox để thực thi migrate.
