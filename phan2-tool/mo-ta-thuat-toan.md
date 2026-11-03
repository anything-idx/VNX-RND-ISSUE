# Mô tả thuật toán (ngôn ngữ tự nhiên) — đối chiếu với `pve_balance.py`

Tài liệu ghi logic mong muốn và đã cập nhật theo policy dữ liệu bẩn (`duplicate_vmid`, `null_mem_used`). Dùng để đối chiếu code.

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
- RAM quá tải khi: (theo **tải quyết định**, xem bên dưới) / `mem_total` node ≥ **85%**.
- Overcommit quá tải khi: tổng `vcpus` (quyết định) / `cpu_cores` ≥ **2.0**.

Người dùng có thể đổi ba ngưỡng này bằng tham số dòng lệnh.

---

## Cách hiểu từng con số

### CPU thực dùng của một VM trên host

Theo đề, `cpu_usage` từ 0 đến 1 là phần sử dụng trên số vCPU đã cấp. Đóng góp lên host ≈ `cpu_usage × vcpus` (đơn vị core tương đương).

**Dữ liệu bẩn gặp trong snapshot:** một số bản ghi ghi `cpu_usage` dạng phần trăm (31, 98, 20.8…). Quy ước:

- Nếu giá trị nằm trong `[0, 1]` → dùng nguyên.
- Nếu `> 1` và `≤ 100` → coi là %, chia 100, **ghi warning**.
- Ngoài khoảng đó hoặc không phải số → **bỏ VM khỏi phép tính tải**, ghi warning (không ném exception).

### RAM và overcommit — hai lớp số

| Lớp | Dùng để | Cách tính |
|---|---|---|
| **Báo cáo** (cột bảng CPU%/MEM%/OVC) | Người đọc | Chỉ số đo được / first-valid; **không** đội bản dup chưa cộng; **không** cộng `mem_max` thay cho `mem_used=null` |
| **Quyết định** (OVERLOAD, chọn đích, residual) | An toàn node | Báo cáo + `Σ mem_max` của VM null trên node + **dirty pessimistic** (dirty vmid conflict chạm node nhưng chưa first-valid trên node đó) |

### Node không đủ điều kiện nhận migrate

- `status != online` → bỏ qua khi chọn đích.
- `mem_total` < **1 GiB** → metrics không tin cậy: warning, không nhận migrate, MEM% = n/a.
- Node không có trong `nodes` → warning `unknown_node`, bỏ VM.

---

## Dữ liệu bẩn đặc biệt (có trong snapshot đề)

### A. `duplicate_vmid` (cùng ID trên ≥2 node)

- PVE: VMID unique trong cluster. Trùng = data bẩn; tool **không** gọi API chốt ownership.
- Warn `duplicate_vmid`; reason `vmid_conflict` trên **mọi** node trong tập conflict.
- **First-valid:** mỗi vmid chỉ cộng tải **một lần** sau khi bản ghi qua được kiểm tra kiểu. Bản lỗi **không** khóa bản tốt phía sau.
- Cấm migrate mọi vmid dirty.
- Pessimistic: khi đánh overload / nhận migrate vào node conflict chưa mang bản first-valid, cộng thêm load dirty (kèm `mem_reserve` nếu dirty từng null).

### B. `null_mem_used` (M6)

- `mem_used is null` → warn; **không** suy ra `0`.
- Có `mem_max > 0`: cộng CPU/vCPU; **không** cộng RAM vào báo cáo; cộng `mem_max` vào `mem_reserve_null` (chỉ lớp quyết định); cấm migrate VM đó; reason `mem_metrics_partial`.
- Không `mem_max` hợp lệ → warn `null_mem_used_no_max`, bỏ VM khỏi tải/migrate.

Các case bẩn khác vẫn giữ: `cpu_usage` %, `unknown_node`, `suspicious_mem_total`, JSON lỗi.

---

## Luồng xử lý mong muốn (từ trên xuống)

### Bước 1 — Đọc JSON an toàn

Đọc file → parse JSON. Root phải là object, có `nodes` và `vms` là array. Sai thì thoát với thông báo rõ.

### Bước 2 — Lập chỉ mục node + phát hiện dup

Lập chỉ mục node (`mem_sane`, status…).  
Quét `vms[]`: vmid xuất hiện trên ≥2 node khác nhau → `dirty_vmids` + `conflict_nodes` + warning.

### Bước 3 — Cộng dồn tải từ VM (báo cáo)

Duyệt từng VM running:

- Lỗi / unknown node / cpu|vcpus|mem kiểu sai → warn, bỏ qua (**chưa** đánh dấu counted).
- Đã có `counted_on[vmid]` → bỏ qua (không cộng đôi).
- `mem_used is null` → nhánh M6 ở trên.
- Cộng CPU/RAM/vCPU theo báo cáo; ghi `counted_on` **sau** khi cộng thành công; nếu dirty → lưu `dirty_loads`.
- `migrate_allowed = false` nếu dirty hoặc null mem.

Gắn `vmid_conflict` cho mọi node trong conflict set.

### Bước 4 — Đánh overload

- Cột hiển thị: % từ tải báo cáo.
- Cờ overloaded: so ngưỡng với **tải quyết định** (báo cáo + reserve null + dirty_extra).

### Bước 5 — Lập kế hoạch migrate (greedy một vòng)

1. Lấy node overloaded **lúc đầu**, nặng nhất trước (theo mức quyết định).
2. Chỉ xét VM `migrate_allowed`.
3. Đích: online + mem_sane; giả lập nhận bằng **tải quyết định đích + VM**; phải dưới cả 3 ngưỡng; chọn headroom RAM lớn nhất.
4. Cập nhật bản sao tải báo cáo khi chốt migrate (`mem_reserve_null` không đi theo VM sạch).
5. Không mở vòng lặp mới cho node vừa phát sinh overload — còn dư thì residual / `cannot_fully_balance`.

### Bước 6 — Residual

Áp migrations lên bản sao; so ngưỡng với tải quyết định (có dirty_extra). Liệt kê node còn overload.

### Bước 7 — Output

Bảng (trước migrate) + migrations + residual + warnings; hoặc JSON cùng schema.

---

## Ràng buộc cứng (checklist đối chiếu)

- [x] Normalize `cpu_usage` % → warning, không crash.
- [x] `mem_total` lỗi → warning + không nhận migrate; tool vẫn chạy.
- [x] Không đề xuất migrate sang node offline / unknown / metrics_untrusted.
- [x] Chỉ VM `running` vào tải; chỉ VM sạch vào kế hoạch migrate.
- [x] Detect + warn `duplicate_vmid` / `null_mem_used`; first-valid; pessimistic quyết định.
- [x] Greedy một vòng: nguồn nặng → VM nặng → đích headroom RAM lớn nhất không vượt ngưỡng.
- [x] Unit test: normalize, overload, đích xấu, JSON lỗi, dup, null mem.
- [x] Chạy được trên `cluster-pve-hcm-c3.json` và in warning case bẩn đã biết.

---

## Việc cố ý chưa làm (ngoài scope)

- Chi phí live-migrate, local disk, affinity / anti-affinity.
- Tối ưu ILP / min-cost flow / vòng lặp cân bằng đến hết.
- Gọi API Proxmox để thực thi migrate hoặc reconcile ownership VMID.
- Ưu tiên first-valid trên node online hơn offline (cạnh nhỏ còn lại).
