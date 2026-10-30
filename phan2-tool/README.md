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

- Thuật toán migrate là greedy (ưu tiên VM nặng RAM trên node overload) — đủ cho snapshot vận hành, chưa tối ưu toàn cục / chưa xét affinity, local disk, live-migrate cost.
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


HÀM phân_tích(data):
  nodes ← lập_chỉ_mục_node(data.nodes)      // tên → {cores, mem, status, mem_sane}
  tải ← {}                                  // cộng dồn theo node

  VỚI MỖI node TRONG nodes:
      tải[node] ← {cpu: 0, mem: 0, vcpu: 0, ds_vm_running: []}

  VỚI MỖI vm TRONG data.vms:
      NẾU vm không hợp lệ / thiếu node / node lạ:
          cảnh_báo; BỎ_QUA
      NẾU vm.status ≠ "running":
          BỎ_QUA                               // stopped không tính tải

      cpu_usage ← chuẩn_hóa_về_[0,1](vm.cpu_usage)
      NẾU cpu_usage = null HOẶC vcpus lỗi:
          đánh_dấu_skipped; BỎ_QUA

      đóng_góp_cpu ← cpu_usage × vm.vcpus
      tải[vm.node].cpu  += đóng_góp_cpu
      tải[vm.node].mem  += vm.mem_used
      tải[vm.node].vcpu += vm.vcpus
      thêm vm (đã chuẩn hóa) vào tải[vm.node].ds_vm_running

  ds_NodeLoad ← []
  VỚI MỖI node TRONG nodes:
      cpu%  ← tải[node].cpu  / cores
      mem%  ← tải[node].mem  / mem_total
      ovc   ← tải[node].vcpu / cores
      valid ← (online VÀ mem_sane)

      overloaded ← False
      NẾU valid:
          NẾU cpu% ≥ CPU_NGƯỠNG  HOẶC  mem% ≥ MEM_NGƯỠNG  HOẶC  ovc ≥ OVC_NGƯỠNG:
              overloaded ← True

      thêm NodeLoad(node, cpu%, mem%, ovc, overloaded, ...) vào ds_NodeLoad

  migrations ← lập_kế_hoạch_migrate(ds_NodeLoad, tải, nodes)
  residual   ← mô_phỏng_sau_migrate(tải, migrations, nodes)
  TRẢ VỀ AnalysisResult(nodes, migrations, residual, warnings, ...)


HÀM lập_kế_hoạch_migrate(ds_NodeLoad, tải, nodes):
  // Bản sao để giả lập — không sửa dữ liệu gốc
  work ← sao_chép(tải)
  migrations ← []

  nguồn_quá_tải ← [n | n.overloaded]
  SẮP_XẾP nguồn_quá_tải GIẢM_DẦN theo max(mem%, cpu%)   // nặng nhất trước

  VỚI MỖI src TRONG nguồn_quá_tải:
      ds_vm ← SẮP_XẾP work[src].ds_vm_running
               GIẢM_DẦN theo (mem_used, cpu_used)       // VM nặng trước

      VỚI MỖI vm TRONG ds_vm:
          NẾU src đã hết overload:
              DỪNG vòng VM                              // sang node nguồn khác

          đích_tốt_nhất ← null
          headroom_max  ← -∞

          VỚI MỖI dst TRONG mọi node:
              NẾU dst = src: BỎ_QUA
              NẾU dst offline HOẶC không tin_cậy: BỎ_QUA

              // Giả lập: dst nhận vm
              cpu'  ← work[dst].cpu  + vm.cpu
              mem'  ← work[dst].mem  + vm.mem
              vcpu' ← work[dst].vcpu + vm.vcpus

              NẾU cpu'/cores ≥ CPU_NGƯỠNG: BỎ_QUA
              NẾU mem'/mem_total ≥ MEM_NGƯỠNG: BỎ_QUA
              NẾU vcpu'/cores ≥ OVC_NGƯỠNG: BỎ_QUA

              // Còn bao nhiêu % RAM trước ngưỡng sau khi nhận
              headroom ← (MEM_NGƯỠNG × mem_total − mem') / mem_total
              NẾU headroom > headroom_max:
                  headroom_max ← headroom
                  đích_tốt_nhất ← dst

          NẾU đích_tốt_nhất = null:
              BỎ_QUA vm                                 // không chỗ nhận an toàn

          // Chốt migrate + cập nhật work
          thêm Migration(vm, từ=src, đến=đích_tốt_nhất)
          trừ tài nguyên vm khỏi work[src]
          cộng tài nguyên vm vào work[đích_tốt_nhất]

      NẾU src vẫn overload:
          cảnh_báo("không cân bằng đủ — thiếu headroom cluster")

  TRẢ VỀ migrations


HÀM mô_phỏng_sau_migrate(tải, migrations, nodes):
  work ← sao_chép(tải)
  VỚI MỖI m TRONG migrations:
      chuyển tài nguyên VM từ m.from → m.to trên work

  residual ← []
  VỚI MỖI node online + tin_cậy:
      NẾU work[node] vẫn ≥ ngưỡng (cpu/mem/ovc):
          thêm node vào residual
  TRẢ VỀ residual
```

**Ý chính:** chỉ VM `running` tính tải → đánh overload theo ngưỡng → greedy giải phóng node nặng nhất (VM nặng RAM trước) sang đích còn nhiều headroom RAM nhất mà không vượt ngưỡng → mô phỏng lại để liệt kê residual.
