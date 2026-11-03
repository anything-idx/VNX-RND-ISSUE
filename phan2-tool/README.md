# pve_balance — phân tích tải & gợi ý migrate Proxmox VE

## Yêu cầu

- Python 3.10+ (chỉ dùng stdlib: không cần `pip install`)

## Chạy

```bash
# Bảng terminal (snapshot kèm trong thư mục này)
python3 pve_balance.py cluster-pve-hcm-c3.json

# JSON (pipe sang jq / script khác)
python3 pve_balance.py cluster-pve-hcm-c3.json --format json

# Tuỳ chỉnh ngưỡng
python3 pve_balance.py cluster-pve-hcm-c3.json --cpu-threshold 0.8 --mem-threshold 0.8 --overcommit-threshold 1.5
```

## Unit test

```bash
python3 -m unittest discover -s tests -v
```

## Giả định & ngưỡng mặc định

| Ngưỡng | Mặc định | Ý nghĩa |
|---|---|---|
| `--cpu-threshold` | `0.85` | Node quá tải CPU khi `sum(cpu_usage * vcpus) / cpu_cores ≥ 85%` |
| `--mem-threshold` | `0.85` | Node quá tải RAM khi `sum(mem_used) / mem_total ≥ 85%` |
| `--overcommit-threshold` | `2.0` | Quá tải overcommit khi `sum(vcpus running) / cpu_cores ≥ 2.0` |

### Vì sao mặc định overcommit = `2.0`?

Đề bài yêu cầu tính tỷ lệ overcommit nhưng **không chốt ngưỡng quá tải** → chọn `2.0` (conservative) và để override bằng CLI.

| Nguồn | Nội dung liên quan |
|---|---|
| [Proxmox VE docs — QEMU CPU](https://pve.proxmox.com/pve-docs/chapter-qm.html#qm_cpu) | Cho phép tổng vCPU mọi VM > số core host; ví dụ **16 vCPU / 8 core (= 2:1)** vẫn được coi là an toàn về mặt scheduler. **Không** quy định ngưỡng “quá tải”. |
| [VMware VCF — ESX design](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/design/design-library/esx-design.html) | Official: over-commitment **vCPU:pCPU ≤ 2:1** cho management domain; workload domain tới **≤ 8:1**, tính theo **physical cores**. |
| [Broadcom KB 414507](https://knowledge.broadcom.com/external/article/414507/calculate-overcommitment-ratio-of-a-host.html) | Công thức: `Ratio = ∑vCPU / pCPU` (VM powered-on) — khớp cách tool tính. |
| [Proxmox forum — field ratios](https://forum.proxmox.com/threads/cpu-cores-and-threads.65923/) | Community: general **4:1–6:1**, DB **2:1 / 1:1**, VDI cao hơn — phụ thuộc workload. |
| [VMware blog (2025) — ratios](https://blogs.vmware.com/cloud-foundation/2025/06/04/vcpu-to-pcpu-ratio-guidelines/) | Không có một ratio chung cho mọi môi trường; ratio cố định chỉ là điểm xuất phát. |

**Kết luận giả định:** `2.0` = mốc cảnh báo sớm / conservative (gần ví dụ PVE 2:1 và khuyến nghị VCF management / workload nhạy CPU). Cluster VPS/shared hosting mật độ cao hơn có thể dùng `--overcommit-threshold 4.0` (hoặc cao hơn) nếu đã giám sát contention thực tế.

**Công thức CPU thực dùng trên node:** theo đề bài, `cpu_usage ∈ [0,1]` tương ứng phần trăm của số vCPU được cấp → đóng góp host ≈ `cpu_usage × vcpus` (đơn vị “core tương đương”).

**Chỉ tính VM `status=running`.** VM `stopped` không góp vào CPU/RAM/overcommit thực dùng.

**Không đề xuất migrate sang** node `offline`, node có `mem_total` bất thường (`< 1 GiB` — xem warning `suspicious_mem_total`), hoặc node không có trong danh sách.

## Xử lý dữ liệu bẩn (có trong snapshot thật)

1. `cpu_usage` > 1 và ≤ 100 → giả định thang % , chia 100, emit warning `cpu_usage_percent`.
2. `cpu_usage` ngoài khoảng hợp lệ → bỏ VM khỏi phép tính tải, warning.
3. VM trỏ `node` không tồn tại → warning `unknown_node`, bỏ qua.
4. `mem_total` cực nhỏ (ví dụ `262144`) → đánh dấu node `metrics_untrusted`, không nhận migrate, `mem_pct` = n/a.
5. JSON lỗi / thiếu `nodes`|`vms` → exit ≠ 0 kèm thông báo rõ, không traceback ẩn.
6. **`duplicate_vmid`** — xem mục riêng bên dưới (ví dụ snapshot: `vmid=2001` trên `hcm-c3-n01` và `hcm-c3-n02`).
7. **`null_mem_used`** — xem mục riêng bên dưới (ví dụ snapshot: VM `2199`, `mem_used: null`).

### Vì sao bổ sung detect `duplicate_vmid` và `null_mem_used`?

Khi chạy tool trên file đề (`cluster-pve-hcm-c3.json`), hai trường hợp dữ liệu bất thường từng bị **tính lặng lẽ, không cảnh báo**:

| Case trong snapshot | Trước đây | Rủi ro |
|---|---|---|
| `vmid` **2001** xuất hiện trên **hai node** khác nhau (cùng tên/metrics) | Cộng tải vào cả hai node | Phóng đại tải cụm; có thể gợi ý migrate dựa trên “bản sao” không tin cậy |
| VM **2199** có `mem_used: null` | `null` bị ép thành `0` (`or 0`) | Under-count RAM node → trông còn headroom → dễ đề xuất nhét thêm VM vào node đang mang guest “mù RAM” |

Theo đề: tool không crash + **log cảnh báo rõ bản ghi bất thường** + ghi giả định. Phạm vi chỉ dùng JSON tĩnh (không API PVE để “đoán” node đúng).

### Policy `duplicate_vmid` (nhiều VMID trùng đa node)

**Giả định:** Trong một cluster PVE, VMID phải unique toàn cụm ([pmxcfs](https://pve.proxmox.com/pve-docs/chapter-pmxcfs.html)). Trùng trên ≥2 node = data bẩn / ghost / lỗi collector — tool **không** gọi API để chốt ownership.

**Cách xử lý đã chọn:**

1. Quét mọi `vmid` xuất hiện trên ≥2 node → warning `duplicate_vmid` (liệt kê các node); reason `vmid_conflict` gắn **mọi** node trong tập conflict.
2. **Báo cáo tải:** mỗi `vmid` chỉ cộng **một lần** (**first-valid** trong `vms[]` — bỏ qua bản ghi lỗi kiểu/thiếu field, không khóa `seen` sớm) — tránh double-count.
3. **Cấm migrate** mọi `vmid` dirty.
4. **Pessimistic trên node conflict (nguồn overload + cửa nhận đích):** với mọi dirty vmid có node `N` trong conflict mà **chưa** được cộng vào báo cáo của `N`, cộng thêm load dirty (kèm `mem_reserve`/`mem_max` nếu dirty từng `mem_used=null`) rồi mới so ngưỡng. Tránh under-count → nhầm còn headroom / bỏ sót overload.
5. Chỉ migrate các VM **sạch** ra khỏi nguồn overload.

**Ghi chú đọc bảng:** cột CPU%/MEM%/OVC = tải **báo cáo** (không đội dirty chưa first-valid). Cờ `OVERLOAD` / quyết định migrate dùng tải **pessimistic**. Bảng NODE = trước migrate; dòng “Còn overload sau plan” = sau gợi ý.

**Không chọn:** fail-hard cả file; cộng cả hai bản vào báo cáo; first-wins rồi nhận đích *không* pessimistic; module API ngoài JSON.

### Policy `null_mem_used` (M6 — tách báo cáo / placement)

**Giả định:** `mem_used: null` = không đo được RAM thực tại snapshot — **không** suy ra `0`. `mem_max` = trần cấp phát, dùng làm **reserve worst-case** khi quyết định quá tải / nhận migrate.

**Cách xử lý đã chọn:**

1. Warning `null_mem_used`.
2. Có `mem_max > 0`: **báo cáo MEM không cộng** RAM null; **CPU/vCPU vẫn cộng**; gắn reason `mem_metrics_partial`.
3. **Cấm migrate** VM null.
4. **Overload nguồn + cửa nhận đích:** `mem_quyết_định = mem_báo_cáo + Σ mem_max` của mọi VM null trên node đó (pessimistic). Cột MEM% trên bảng vẫn là báo cáo — có thể thấp hơn mức dùng để gắn `OVERLOAD`.
5. Không có `mem_max` hợp lệ → warning `null_mem_used_no_max`, bỏ VM khỏi tải/migrate.

**Không chọn:** `null → 0` (kể cả có warn); cộng `mem_max` vào cột MEM báo cáo (đó là policy khác, đội số hiển thị).

## Ví dụ output (rút gọn)

```text
Cluster: pve-hcm-c3  collected_at: 2026-10-27T09:00:00+07:00
Thresholds: cpu>=85%  mem>=85%  overcommit>=2.0x

NODE           ST          CPU%    MEM%    OVC   CPU_USED   MEM_USED FLAG         REASONS
------------------------------------------------------------------------------------------
hcm-c3-n01     online      85.9%   93.0%  1.72x     55.0c   476.2GiB OVERLOAD     cpu>=85%,mem>=85%
hcm-c3-n02     online      ...     ...    ...       ...      ...      ok           -
hcm-c3-n04     offline     ...     ...    ...       ...      ...      SKIP         status=offline
hcm-c3-n06     online      ...      n/a   ...       ...      ...      SKIP         metrics_untrusted
...

Migrations đề xuất:
  VM ... (...): hcm-c3-n01 -> hcm-c3-n0X [...] — giải phóng hcm-c3-n01 (cpu>=85%,mem>=85%)

Warnings (N):
  - cpu_usage_percent: cpu_usage=31.0 ngoài [0,1]; giả định thang % và chia 100 [vmid=2067]
  - unknown_node: VM 2390 (vps-nvme-512) trỏ node không tồn tại: hcm-c3-n07 [vmid=2390]
  - suspicious_mem_total: Node hcm-c3-n06 mem_total=262144 (<1GiB); ...
```

## Giới hạn biết trước

- Thuật toán migrate là greedy **một vòng** trên các node overload lúc đầu (ưu tiên VM nặng RAM) — đủ cho snapshot vận hành; chưa lặp lại đến hết overload, chưa tối ưu toàn cục / chưa xét affinity, local disk, live-migrate cost.
- Không gọi API Proxmox; chỉ phân tích file JSON tĩnh.

## Thiết kế trước khi code

Mô tả thuật toán bằng ngôn ngữ tự nhiên (dùng làm spec nhờ AI viết code, rồi đối chiếu): [`mo-ta-thuat-toan.md`](mo-ta-thuat-toan.md).

## Mã giả thuật toán

```text
HẰNG SỐ:
  CPU_NGƯỠNG = 0.85
  MEM_NGƯỠNG = 0.85
  OVC_NGƯỠNG = 2.0


HÀM chính(file_json):
  data ← đọc_và_kiểm_tra(file_json)
  kết_quả ← phân_tích(data)
  in bảng hoặc JSON(kết_quả)


HÀM dirty_extra(node, conflict_nodes, counted_on, dirty_loads):
  // Pessimistic: dirty chạm node nhưng chưa cộng vào báo cáo node
  extra_cpu, extra_mem, extra_vcpu ← 0
  VỚI MỖI vmid, nodes TRONG conflict_nodes:
      NẾU node ∉ nodes: TIẾP
      NẾU counted_on[vmid] = node: TIẾP          // đã nằm trên báo cáo
      dl ← dirty_loads[vmid]
      extra_cpu  += dl.cpu
      extra_mem  += dl.mem + dl.mem_reserve     // mem_reserve = mem_max nếu null
      extra_vcpu += dl.vcpus
  TRẢ VỀ (extra_cpu, extra_mem, extra_vcpu)


HÀM tải_quyết_định(node, tải, ...):
  (ex_cpu, ex_mem, ex_vcpu) ← dirty_extra(node, ...)
  cpu  ← tải[node].cpu  + ex_cpu
  mem  ← tải[node].mem  + tải[node].mem_reserve_null + ex_mem
  vcpu ← tải[node].vcpu + ex_vcpu
  TRẢ VỀ (cpu, mem, vcpu)


HÀM phân_tích(data):
  nodes ← lập_chỉ_mục_node(data.nodes)
  dirty_vmids, conflict_nodes ← phát_hiện_vmid_trùng_đa_node(data.vms)  // warn duplicate_vmid
  tải ← {}
  counted_on ← {}      // vmid → node first-valid
  dirty_loads ← {}

  VỚI MỖI node TRONG nodes:
      tải[node] ← {cpu: 0, mem: 0, mem_reserve_null: 0, vcpu: 0, ds_vm_running: []}

  VỚI MỖI vm TRONG data.vms:
      NẾU vm không hợp lệ / thiếu node / node lạ: cảnh_báo; BỎ_QUA
      NẾU vm.status ≠ "running": BỎ_QUA
      NẾU vm.vmid ∈ counted_on: BỎ_QUA          // đã có bản hợp lệ — không cộng đôi

      chuẩn_hóa cpu / vcpus / mem
      NẾU lỗi kiểu: cảnh_báo; BỎ_QUA            // CHƯA ghi counted_on

      // M6: mem_used = null
      NẾU mem_used is null:
          cảnh_báo null_mem_used
          NẾU mem_max không hợp lệ: cảnh_báo; BỎ_QUA
          cộng cpu/vcpu; KHÔNG cộng mem báo cáo
          tải[node].mem_reserve_null += mem_max
          migrate_allowed ← False
      NGƯỢC_LẠI:
          cộng cpu, mem, vcpu như thường
          migrate_allowed ← (vm.vmid ∉ dirty_vmids)

      NẾU vm.vmid ∈ dirty_vmids: migrate_allowed ← False

      thêm vm chuẩn hóa vào tải[node].ds_vm_running
      counted_on[vm.vmid] ← node                // chỉ SAU khi cộng thành công
      NẾU dirty: lưu dirty_loads[vmid] (cpu, mem, mem_reserve, vcpus)

  gắn reason vmid_conflict cho MỌI node trong conflict_nodes

  ds_NodeLoad ← []
  VỚI MỖI node TRONG nodes:
      // Hiển thị: cpu%/mem%/ovc từ tải báo cáo (không đội dirty extra)
      // Overload: dùng tải_quyết_định (báo cáo + reserve null + dirty pessimistic)
      (cpu_d, mem_d, vcpu_d) ← tải_quyết_định(node, ...)
      overloaded ← valid VÀ (cpu_d|mem_d|vcpu_d vượt ngưỡng)
      thêm NodeLoad(...)

  migrations ← lập_kế_hoạch_migrate(...)
  residual   ← mô_phỏng_sau_migrate(...)        // cũng cộng dirty_extra khi so ngưỡng
  TRẢ VỀ AnalysisResult(...)


HÀM lập_kế_hoạch_migrate(...):
  work ← sao_chép(tải)                          // gồm mem_reserve_null
  migrations ← []
  nguồn_quá_tải ← [n | n.overloaded]            // list lúc đầu — greedy 1 vòng
  SẮP_XẾP nguồn GIẢM_DẦN theo mức quyết_định

  VỚI MỖI src TRONG nguồn_quá_tải:
      ds_vm ← VM trong work[src] có migrate_allowed
               SẮP GIẢM_DẦN (mem_used, cpu_used)

      VỚI MỖI vm TRONG ds_vm:
          NẾU src hết overload (theo tải_quyết_định): DỪNG vòng VM

          đích_tốt_nhất ← null; headroom_max ← -∞
          VỚI MỖI dst:
              NẾU dst = src / offline / không tin_cậy: BỎ_QUA
              (cpu0, mem0, vcpu0) ← tải_quyết_định(dst, work, ...)
              cpu'  ← cpu0  + vm.cpu
              mem'  ← mem0  + vm.mem
              vcpu' ← vcpu0 + vm.vcpus
              NẾU cpu'|mem'|vcpu' ≥ ngưỡng: BỎ_QUA
              chọn dst còn headroom RAM lớn nhất

          NẾU không có đích: BỎ_QUA vm
          chốt Migration; cập nhật work[src]/work[dst]   // chỉ tải báo cáo; reserve null không theo VM sạch

      NẾU src vẫn overload: cảnh_báo cannot_fully_balance

  TRẢ VỀ migrations
```

**Ý chính:** phát hiện dirty (dup / null) → báo cáo trung thực (first-valid, không bịa mem null) → overload & chọn đích theo tải **pessimistic** → greedy 1 vòng chỉ migrate VM sạch → residual sau plan.
