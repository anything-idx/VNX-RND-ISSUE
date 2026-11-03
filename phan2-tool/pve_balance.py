#!/usr/bin/env python3
"""Phân tích tải cluster Proxmox VE và gợi ý live-migrate cân bằng tài nguyên."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# Ngưỡng mặc định (ghi rõ trong README / --help)
DEFAULT_CPU_PCT = 0.85
DEFAULT_MEM_PCT = 0.85
DEFAULT_OVERCOMMIT = 2.0


@dataclass
class WarningItem:
    code: str
    message: str
    ref: str | None = None


@dataclass
class NodeLoad:
    node: str
    status: str
    cpu_cores: int
    mem_total: int
    cpu_used_cores: float
    cpu_pct: float
    mem_used: int
    mem_pct: float
    vcpu_alloc: int
    overcommit: float
    overloaded: bool
    reasons: list[str] = field(default_factory=list)
    skipped_vms: list[str] = field(default_factory=list)
    valid: bool = True


@dataclass
class Migration:
    vmid: int
    name: str
    from_node: str
    to_node: str
    mem_used: int
    cpu_used_cores: float
    reason: str


@dataclass
class AnalysisResult:
    cluster: str
    collected_at: str | None
    thresholds: dict[str, float]
    warnings: list[WarningItem]
    nodes: list[NodeLoad]
    migrations: list[Migration]
    residual_overloaded: list[str]


def _warn(warnings: list[WarningItem], code: str, message: str, ref: str | None = None) -> None:
    warnings.append(WarningItem(code=code, message=message, ref=ref))


def normalize_cpu_usage(raw: Any, vmid: Any, warnings: list[WarningItem]) -> float | None:
    """Chuẩn hóa cpu_usage về [0, 1] theo định nghĩa đề bài.

    Một số bản ghi trong snapshot dùng thang % (ví dụ 31.0, 98.0).
    """
    if raw is None:
        _warn(warnings, "missing_cpu_usage", "Thiếu cpu_usage", ref=f"vmid={vmid}")
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _warn(warnings, "bad_cpu_usage", f"cpu_usage không phải số: {raw!r}", ref=f"vmid={vmid}")
        return None
    if 0.0 <= value <= 1.0:
        return value
    if 1.0 < value <= 100.0:
        _warn(
            warnings,
            "cpu_usage_percent",
            f"cpu_usage={value} ngoài [0,1]; giả định thang % và chia 100",
            ref=f"vmid={vmid}",
        )
        return value / 100.0
    _warn(
        warnings,
        "cpu_usage_out_of_range",
        f"cpu_usage={value} không hợp lệ; bỏ qua VM khi tính tải",
        ref=f"vmid={vmid}",
    )
    return None


def load_cluster(path: Path) -> tuple[dict[str, Any], list[WarningItem]]:
    warnings: list[WarningItem] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Không đọc được file: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON không hợp lệ: {exc}") from exc

    if not isinstance(data, dict):
        raise SystemExit("Root JSON phải là object")

    if "nodes" not in data or "vms" not in data:
        raise SystemExit("Thiếu trường bắt buộc: nodes và/hoặc vms")

    if not isinstance(data["nodes"], list) or not isinstance(data["vms"], list):
        raise SystemExit("nodes và vms phải là array")

    return data, warnings


def build_node_index(
    nodes_raw: list[Any], warnings: list[WarningItem]
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for i, item in enumerate(nodes_raw):
        if not isinstance(item, dict):
            _warn(warnings, "bad_node", f"nodes[{i}] không phải object", ref=str(i))
            continue
        name = item.get("node")
        if not name:
            _warn(warnings, "bad_node", f"nodes[{i}] thiếu node", ref=str(i))
            continue
        required = ("status", "cpu_cores", "mem_total")
        missing = [k for k in required if k not in item]
        if missing:
            _warn(
                warnings,
                "incomplete_node",
                f"Node {name} thiếu trường: {', '.join(missing)}",
                ref=name,
            )
        try:
            cores = int(item.get("cpu_cores") or 0)
            mem_total = int(item.get("mem_total") or 0)
        except (TypeError, ValueError):
            _warn(warnings, "bad_node_metrics", f"Node {name} cpu_cores/mem_total lỗi kiểu", ref=name)
            cores, mem_total = 0, 0
        if cores <= 0:
            _warn(warnings, "bad_cpu_cores", f"Node {name} cpu_cores={cores} bất thường", ref=name)
        if mem_total < 1_073_741_824:  # < 1 GiB — gần như chắc sai đơn vị / corrupt
            _warn(
                warnings,
                "suspicious_mem_total",
                f"Node {name} mem_total={mem_total} (<1GiB); đánh dấu node không đủ tin cậy để nhận migrate",
                ref=name,
            )
        index[str(name)] = {
            "node": str(name),
            "status": str(item.get("status", "unknown")),
            "cpu_cores": cores,
            "mem_total": mem_total,
            "note": item.get("note"),
            "mem_sane": mem_total >= 1_073_741_824 and cores > 0,
        }
    return index


def detect_duplicate_vmids(
    vms_raw: list[Any], warnings: list[WarningItem]
) -> tuple[set[Any], dict[Any, set[str]]]:
    """VMID xuất hiện trên ≥2 node khác nhau → dirty (invariant cluster PVE)."""
    locations: dict[Any, list[str]] = {}
    for vm in vms_raw:
        if not isinstance(vm, dict):
            continue
        vmid = vm.get("vmid")
        node = vm.get("node")
        if vmid is None or node is None:
            continue
        locations.setdefault(vmid, []).append(str(node))

    dirty: set[Any] = set()
    conflict_nodes: dict[Any, set[str]] = {}
    for vmid, nodes in locations.items():
        uniq = sorted(set(nodes))
        if len(uniq) < 2:
            continue
        dirty.add(vmid)
        conflict_nodes[vmid] = set(uniq)
        _warn(
            warnings,
            "duplicate_vmid",
            f"VMID {vmid} xuất hiện trên nhiều node: {', '.join(uniq)} "
            f"(PVE yêu cầu unique trong cluster; first-valid cộng tải, cấm migrate ID này, "
            f"node conflict dùng tải pessimistic khi overload/nhận migrate)",
            ref=str(vmid),
        )
    return dirty, conflict_nodes


def dirty_extra_for_node(
    node: str,
    conflict_nodes: dict[Any, set[str]],
    counted_on: dict[Any, str],
    dirty_loads: dict[Any, dict[str, Any]],
) -> tuple[float, int, int]:
    """Pessimistic: dirty chạm node nhưng chưa cộng vào báo cáo node → cộng thêm.

    mem = mem_used báo cáo của dirty + mem_reserve (mem_max nếu dirty từng null).
    """
    extra_cpu = 0.0
    extra_mem = 0
    extra_vcpu = 0
    for vmid, nodes in conflict_nodes.items():
        if node not in nodes:
            continue
        if counted_on.get(vmid) == node:
            continue
        dl = dirty_loads.get(vmid)
        if not dl:
            continue
        extra_cpu += float(dl["cpu_used_cores"])
        extra_mem += int(dl["mem_used"]) + int(dl.get("mem_reserve", 0))
        extra_vcpu += int(dl["vcpus"])
    return extra_cpu, extra_mem, extra_vcpu


def analyze(
    data: dict[str, Any],
    *,
    cpu_threshold: float = DEFAULT_CPU_PCT,
    mem_threshold: float = DEFAULT_MEM_PCT,
    overcommit_threshold: float = DEFAULT_OVERCOMMIT,
) -> AnalysisResult:
    warnings: list[WarningItem] = []
    nodes = build_node_index(data.get("nodes", []), warnings)
    dirty_vmids, conflict_nodes = detect_duplicate_vmids(data.get("vms", []), warnings)

    # Accumulator per node
    acc: dict[str, dict[str, Any]] = {
        name: {
            "cpu_used_cores": 0.0,
            "mem_used": 0,  # báo cáo: chỉ mem đo được
            "mem_reserve_null": 0,  # M6: Σ mem_max của VM mem_used=null
            "vcpu_alloc": 0,
            "skipped": [],
            "running": [],
            "has_null_mem": False,
            "has_vmid_conflict": False,
        }
        for name in nodes
    }
    counted_on: dict[Any, str] = {}  # vmid → node first-valid (đã cộng tải)
    dirty_loads: dict[Any, dict[str, Any]] = {}

    for i, vm in enumerate(data.get("vms", [])):
        if not isinstance(vm, dict):
            _warn(warnings, "bad_vm", f"vms[{i}] không phải object", ref=str(i))
            continue

        vmid = vm.get("vmid", f"idx{i}")
        name = vm.get("name", "?")
        node = vm.get("node")
        status = vm.get("status", "unknown")

        if node is None:
            _warn(warnings, "vm_missing_node", f"VM {vmid} ({name}) thiếu node", ref=str(vmid))
            continue

        node = str(node)
        if node not in nodes:
            _warn(
                warnings,
                "unknown_node",
                f"VM {vmid} ({name}) trỏ node không tồn tại: {node}",
                ref=str(vmid),
            )
            continue

        if status != "running":
            continue

        if nodes[node]["status"] != "online":
            _warn(
                warnings,
                "running_on_offline",
                f"VM {vmid} running trên node {node} đang {nodes[node]['status']}",
                ref=str(vmid),
            )

        # Dup: đã có bản valid → không cộng lại (first-valid wins)
        if vmid in counted_on:
            continue

        try:
            vcpus = int(vm.get("vcpus"))
        except (TypeError, ValueError):
            _warn(warnings, "bad_vcpus", f"VM {vmid} vcpus lỗi", ref=str(vmid))
            acc[node]["skipped"].append(str(vmid))
            continue
        if vcpus <= 0:
            _warn(warnings, "bad_vcpus", f"VM {vmid} vcpus={vcpus}", ref=str(vmid))
            acc[node]["skipped"].append(str(vmid))
            continue

        cpu_usage = normalize_cpu_usage(vm.get("cpu_usage"), vmid, warnings)
        if cpu_usage is None:
            acc[node]["skipped"].append(str(vmid))
            continue

        raw_mem_used = vm.get("mem_used")
        mem_null = raw_mem_used is None
        try:
            if mem_null:
                mem_max = int(vm.get("mem_max") or 0)
                mem_used = 0
            else:
                mem_used = int(raw_mem_used)
                mem_max = int(vm.get("mem_max") or 0)
        except (TypeError, ValueError):
            _warn(warnings, "bad_mem", f"VM {vmid} mem_* lỗi kiểu", ref=str(vmid))
            acc[node]["skipped"].append(str(vmid))
            continue

        # M6: mem_used=null
        if mem_null:
            _warn(
                warnings,
                "null_mem_used",
                f"VM {vmid} ({name}) trên {node}: mem_used=null",
                ref=str(vmid),
            )
            if mem_max <= 0:
                _warn(
                    warnings,
                    "null_mem_used_no_max",
                    f"VM {vmid} mem_used=null và mem_max không hợp lệ; bỏ VM khỏi tải/migrate",
                    ref=str(vmid),
                )
                acc[node]["skipped"].append(str(vmid))
                continue

        if not mem_null and mem_max > 0 and mem_used > mem_max * 1.05:
            _warn(
                warnings,
                "mem_used_gt_max",
                f"VM {vmid} mem_used > mem_max",
                ref=str(vmid),
            )

        cpu_used_cores = cpu_usage * vcpus
        is_dirty = vmid in dirty_vmids
        migrate_allowed = (not mem_null) and (not is_dirty)
        norm = {
            "vmid": int(vmid) if str(vmid).isdigit() else vmid,
            "name": name,
            "node": node,
            "vcpus": vcpus,
            "cpu_usage": cpu_usage,
            "cpu_used_cores": cpu_used_cores,
            "mem_used": 0 if mem_null else mem_used,
            "mem_max": mem_max,
            "mem_null": mem_null,
            "migrate_allowed": migrate_allowed,
        }
        acc[node]["cpu_used_cores"] += cpu_used_cores
        if not mem_null:
            acc[node]["mem_used"] += mem_used
        else:
            acc[node]["mem_reserve_null"] += mem_max
            acc[node]["has_null_mem"] = True
        acc[node]["vcpu_alloc"] += vcpus
        acc[node]["running"].append(norm)
        counted_on[vmid] = node  # chỉ sau khi cộng tải thành công
        if is_dirty:
            dirty_loads[vmid] = {
                "cpu_used_cores": cpu_used_cores,
                "mem_used": 0 if mem_null else mem_used,
                "mem_reserve": mem_max if mem_null else 0,
                "vcpus": vcpus,
            }

    # Gắn cờ conflict trên mọi node trong tập (không chỉ first-valid)
    for _vmid, cnodes in conflict_nodes.items():
        for n in cnodes:
            if n in acc:
                acc[n]["has_vmid_conflict"] = True

    node_loads: list[NodeLoad] = []
    for name, meta in nodes.items():
        a = acc[name]
        cores = meta["cpu_cores"] or 1
        mem_total = meta["mem_total"] or 1
        ex_cpu, ex_mem, ex_vcpu = dirty_extra_for_node(
            name, conflict_nodes, counted_on, dirty_loads
        )
        cpu_pct_display = a["cpu_used_cores"] / cores if cores else 0.0
        mem_pct_display = a["mem_used"] / mem_total if mem_total else 0.0
        overcommit_display = a["vcpu_alloc"] / cores if cores else 0.0
        # Quyết định: báo cáo + reserve null + dirty pessimistic (nếu chưa nằm trên node)
        cpu_pct_decision = (a["cpu_used_cores"] + ex_cpu) / cores if cores else 0.0
        mem_pct_decision = (
            (a["mem_used"] + a["mem_reserve_null"] + ex_mem) / mem_total if mem_total else 0.0
        )
        overcommit_decision = (a["vcpu_alloc"] + ex_vcpu) / cores if cores else 0.0

        reasons: list[str] = []
        valid = bool(meta["mem_sane"]) and meta["status"] == "online"
        if meta["status"] != "online":
            reasons.append(f"status={meta['status']}")
        if not meta["mem_sane"]:
            reasons.append("metrics_untrusted")
        if a["has_null_mem"]:
            reasons.append("mem_metrics_partial")
        if a["has_vmid_conflict"]:
            reasons.append("vmid_conflict")
        overloaded = False
        if valid:
            if cpu_pct_decision >= cpu_threshold:
                reasons.append(f"cpu>={cpu_threshold:.0%}")
                overloaded = True
            if mem_pct_decision >= mem_threshold:
                reasons.append(f"mem>={mem_threshold:.0%}")
                overloaded = True
            if overcommit_decision >= overcommit_threshold:
                reasons.append(f"overcommit>={overcommit_threshold:.1f}x")
                overloaded = True

        node_loads.append(
            NodeLoad(
                node=name,
                status=meta["status"],
                cpu_cores=meta["cpu_cores"],
                mem_total=meta["mem_total"],
                cpu_used_cores=round(a["cpu_used_cores"], 3),
                cpu_pct=round(cpu_pct_display, 4),
                mem_used=a["mem_used"],
                mem_pct=round(mem_pct_display, 4) if meta["mem_sane"] else -1.0,
                vcpu_alloc=a["vcpu_alloc"],
                overcommit=round(overcommit_display, 3),
                overloaded=overloaded,
                reasons=reasons,
                skipped_vms=a["skipped"],
                valid=valid,
            )
        )

    migrations = plan_migrations(
        node_loads,
        acc,
        nodes,
        cpu_threshold=cpu_threshold,
        mem_threshold=mem_threshold,
        overcommit_threshold=overcommit_threshold,
        warnings=warnings,
        conflict_nodes=conflict_nodes,
        counted_on=counted_on,
        dirty_loads=dirty_loads,
    )

    residual = simulate_after(
        node_loads,
        acc,
        migrations,
        nodes,
        cpu_threshold,
        mem_threshold,
        overcommit_threshold,
        conflict_nodes=conflict_nodes,
        counted_on=counted_on,
        dirty_loads=dirty_loads,
    )

    return AnalysisResult(
        cluster=str(data.get("cluster", "unknown")),
        collected_at=data.get("collected_at"),
        thresholds={
            "cpu_pct": cpu_threshold,
            "mem_pct": mem_threshold,
            "overcommit": overcommit_threshold,
        },
        warnings=warnings,
        nodes=sorted(node_loads, key=lambda n: n.node),
        migrations=migrations,
        residual_overloaded=residual,
    )


def _headroom(
    load: NodeLoad,
    *,
    cpu_threshold: float,
    mem_threshold: float,
    overcommit_threshold: float,
) -> tuple[float, int, float]:
    """Trả về (cpu_cores_free_to_threshold, mem_bytes_free, vcpu_free_to_overcommit)."""
    if not load.valid:
        return 0.0, 0, 0.0
    cpu_free = max(0.0, cpu_threshold * load.cpu_cores - load.cpu_used_cores)
    mem_free = max(0, int(mem_threshold * load.mem_total) - load.mem_used)
    vcpu_free = max(0.0, overcommit_threshold * load.cpu_cores - load.vcpu_alloc)
    return cpu_free, mem_free, vcpu_free


def plan_migrations(
    node_loads: list[NodeLoad],
    acc: dict[str, dict[str, Any]],
    nodes_meta: dict[str, dict[str, Any]],
    *,
    cpu_threshold: float,
    mem_threshold: float,
    overcommit_threshold: float,
    warnings: list[WarningItem],
    conflict_nodes: dict[Any, set[str]] | None = None,
    counted_on: dict[Any, str] | None = None,
    dirty_loads: dict[Any, dict[str, Any]] | None = None,
) -> list[Migration]:
    conflict_nodes = conflict_nodes or {}
    counted_on = counted_on or {}
    dirty_loads = dirty_loads or {}

    work = {
        name: {
            "cpu_used_cores": float(acc[name]["cpu_used_cores"]),
            "mem_used": int(acc[name]["mem_used"]),
            "mem_reserve_null": int(acc[name]["mem_reserve_null"]),
            "vcpu_alloc": int(acc[name]["vcpu_alloc"]),
            "running": list(acc[name]["running"]),
        }
        for name in acc
    }

    migrations: list[Migration] = []

    def decision_totals(name: str) -> tuple[float, int, int]:
        """CPU/MEM/vCPU quyết định = báo cáo + reserve null + dirty pessimistic."""
        w = work[name]
        ex_cpu, ex_mem, ex_vcpu = dirty_extra_for_node(
            name, conflict_nodes, counted_on, dirty_loads
        )
        return (
            w["cpu_used_cores"] + ex_cpu,
            w["mem_used"] + w["mem_reserve_null"] + ex_mem,
            w["vcpu_alloc"] + ex_vcpu,
        )

    def current_load(name: str) -> NodeLoad:
        meta = nodes_meta[name]
        cores = meta["cpu_cores"] or 1
        mem_total = meta["mem_total"] or 1
        w = work[name]
        cpu_d, mem_d, vcpu_d = decision_totals(name)
        cpu_pct = cpu_d / cores
        mem_pct = mem_d / mem_total if meta["mem_sane"] else 999.0
        overcommit = vcpu_d / cores
        valid = bool(meta["mem_sane"]) and meta["status"] == "online"
        overloaded = False
        reasons: list[str] = []
        if valid:
            if cpu_pct >= cpu_threshold:
                overloaded = True
                reasons.append("cpu")
            if mem_pct >= mem_threshold:
                overloaded = True
                reasons.append("mem")
            if overcommit >= overcommit_threshold:
                overloaded = True
                reasons.append("overcommit")
        return NodeLoad(
            node=name,
            status=meta["status"],
            cpu_cores=meta["cpu_cores"],
            mem_total=meta["mem_total"],
            cpu_used_cores=w["cpu_used_cores"],
            cpu_pct=cpu_pct,
            mem_used=w["mem_used"],
            mem_pct=mem_pct if meta["mem_sane"] else -1.0,
            vcpu_alloc=w["vcpu_alloc"],
            overcommit=overcommit,
            overloaded=overloaded,
            reasons=reasons,
            valid=valid,
        )

    overloaded_names = [n.node for n in node_loads if n.overloaded]
    overloaded_names.sort(
        key=lambda n: max(current_load(n).mem_pct, current_load(n).cpu_pct),
        reverse=True,
    )

    for src in overloaded_names:
        candidates = sorted(
            [v for v in work[src]["running"] if v.get("migrate_allowed", True)],
            key=lambda v: (v["mem_used"], v["cpu_used_cores"]),
            reverse=True,
        )
        for vm in list(candidates):
            src_load = current_load(src)
            if not src_load.overloaded:
                break

            best: tuple[float, str] | None = None
            for dst in sorted(work.keys()):
                if dst == src:
                    continue
                dst_meta = nodes_meta[dst]
                if dst_meta["status"] != "online" or not dst_meta["mem_sane"]:
                    continue
                dst_cpu0, dst_mem0, dst_vcpu0 = decision_totals(dst)
                dst_cpu = dst_cpu0 + vm["cpu_used_cores"]
                dst_mem = dst_mem0 + vm["mem_used"]
                dst_vcpu = dst_vcpu0 + vm["vcpus"]
                cores = dst_meta["cpu_cores"] or 1
                mem_total = dst_meta["mem_total"] or 1
                if dst_cpu / cores >= cpu_threshold:
                    continue
                if dst_mem / mem_total >= mem_threshold:
                    continue
                if dst_vcpu / cores >= overcommit_threshold:
                    continue
                headroom = (mem_threshold * mem_total - dst_mem) / mem_total
                if best is None or headroom > best[0]:
                    best = (headroom, dst)

            if best is None:
                continue

            dst = best[1]
            reason = f"giải phóng {src} ({','.join(src_load.reasons) or 'overload'})"
            migrations.append(
                Migration(
                    vmid=vm["vmid"],
                    name=vm["name"],
                    from_node=src,
                    to_node=dst,
                    mem_used=vm["mem_used"],
                    cpu_used_cores=round(vm["cpu_used_cores"], 3),
                    reason=reason,
                )
            )
            work[src]["cpu_used_cores"] -= vm["cpu_used_cores"]
            work[src]["mem_used"] -= vm["mem_used"]
            work[src]["vcpu_alloc"] -= vm["vcpus"]
            work[src]["running"].remove(vm)
            work[dst]["cpu_used_cores"] += vm["cpu_used_cores"]
            work[dst]["mem_used"] += vm["mem_used"]
            work[dst]["vcpu_alloc"] += vm["vcpus"]
            work[dst]["running"].append(vm)

        if current_load(src).overloaded:
            _warn(
                warnings,
                "cannot_fully_balance",
                f"Không migrate đủ để đưa {src} xuống dưới ngưỡng (thiếu headroom cluster)",
                ref=src,
            )

    return migrations


def simulate_after(
    node_loads: list[NodeLoad],
    acc: dict[str, dict[str, Any]],
    migrations: list[Migration],
    nodes_meta: dict[str, dict[str, Any]],
    cpu_threshold: float,
    mem_threshold: float,
    overcommit_threshold: float,
    *,
    conflict_nodes: dict[Any, set[str]] | None = None,
    counted_on: dict[Any, str] | None = None,
    dirty_loads: dict[Any, dict[str, Any]] | None = None,
) -> list[str]:
    conflict_nodes = conflict_nodes or {}
    counted_on = counted_on or {}
    dirty_loads = dirty_loads or {}
    work = {
        name: {
            "cpu": float(acc[name]["cpu_used_cores"]),
            "mem": int(acc[name]["mem_used"]) + int(acc[name]["mem_reserve_null"]),
            "vcpu": int(acc[name]["vcpu_alloc"]),
        }
        for name in acc
    }
    vm_index: dict[Any, dict[str, Any]] = {}
    for name in acc:
        for vm in acc[name]["running"]:
            vm_index[vm["vmid"]] = vm
    for m in migrations:
        vm = vm_index.get(m.vmid)
        if not vm:
            continue
        work[m.from_node]["cpu"] -= vm["cpu_used_cores"]
        work[m.from_node]["mem"] -= vm["mem_used"]
        work[m.from_node]["vcpu"] -= vm["vcpus"]
        work[m.to_node]["cpu"] += vm["cpu_used_cores"]
        work[m.to_node]["mem"] += vm["mem_used"]
        work[m.to_node]["vcpu"] += vm["vcpus"]

    residual: list[str] = []
    for name, meta in nodes_meta.items():
        if meta["status"] != "online" or not meta["mem_sane"]:
            continue
        cores = meta["cpu_cores"] or 1
        mem_total = meta["mem_total"] or 1
        w = work[name]
        ex_cpu, ex_mem, ex_vcpu = dirty_extra_for_node(
            name, conflict_nodes, counted_on, dirty_loads
        )
        if (
            (w["cpu"] + ex_cpu) / cores >= cpu_threshold
            or (w["mem"] + ex_mem) / mem_total >= mem_threshold
            or (w["vcpu"] + ex_vcpu) / cores >= overcommit_threshold
        ):
            residual.append(name)
    return residual


def format_bytes(n: int) -> str:
    if n < 0:
        return "n/a"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{n}B"


def render_table(result: AnalysisResult) -> str:
    lines: list[str] = []
    lines.append(f"Cluster: {result.cluster}  collected_at: {result.collected_at or '-'}")
    lines.append(
        "Thresholds: "
        f"cpu>={result.thresholds['cpu_pct']:.0%}  "
        f"mem>={result.thresholds['mem_pct']:.0%}  "
        f"overcommit>={result.thresholds['overcommit']:.1f}x"
    )
    lines.append("")
    hdr = (
        f"{'NODE':<14} {'ST':<8} {'CPU%':>7} {'MEM%':>7} {'OVC':>6} "
        f"{'CPU_USED':>10} {'MEM_USED':>10} {'FLAG':<12} REASONS"
    )
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for n in result.nodes:
        flag = "OVERLOAD" if n.overloaded else ("SKIP" if not n.valid else "ok")
        mem_pct = f"{n.mem_pct*100:6.1f}%" if n.mem_pct >= 0 else "   n/a"
        lines.append(
            f"{n.node:<14} {n.status:<8} {n.cpu_pct*100:6.1f}% {mem_pct:>7} {n.overcommit:5.2f}x "
            f"{n.cpu_used_cores:8.1f}c {format_bytes(n.mem_used):>10} {flag:<12} {','.join(n.reasons) or '-'}"
        )

    lines.append("")
    lines.append(
        "Migrations đề xuất:  (bảng NODE phía trên = trạng thái TRƯỚC migrate; "
        "xem residual bên dưới cho sau plan)"
    )
    if not result.migrations:
        lines.append("  (không cần / không tìm được phương án)")
    else:
        for m in result.migrations:
            lines.append(
                f"  VM {m.vmid} ({m.name}): {m.from_node} -> {m.to_node} "
                f"[mem={format_bytes(m.mem_used)}, cpu≈{m.cpu_used_cores:.2f}c] — {m.reason}"
            )

    lines.append("")
    if result.residual_overloaded:
        lines.append(
            "Còn overload sau plan: " + ", ".join(result.residual_overloaded)
        )
    else:
        lines.append("Còn overload sau plan: (không)")

    if result.warnings:
        lines.append("")
        lines.append(f"Warnings ({len(result.warnings)}):")
        for w in result.warnings:
            ref = f" [{w.ref}]" if w.ref else ""
            lines.append(f"  - {w.code}: {w.message}{ref}")

    return "\n".join(lines) + "\n"


def result_to_json(result: AnalysisResult) -> dict[str, Any]:
    return {
        "cluster": result.cluster,
        "collected_at": result.collected_at,
        "thresholds": result.thresholds,
        "warnings": [asdict(w) for w in result.warnings],
        "nodes": [asdict(n) for n in result.nodes],
        "migrations": [asdict(m) for m in result.migrations],
        "residual_overloaded": result.residual_overloaded,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phân tích tải & gợi ý migrate Proxmox VE")
    p.add_argument("input", type=Path, help="File JSON snapshot cluster")
    p.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Định dạng output (mặc định: table)",
    )
    p.add_argument("--cpu-threshold", type=float, default=DEFAULT_CPU_PCT)
    p.add_argument("--mem-threshold", type=float, default=DEFAULT_MEM_PCT)
    p.add_argument("--overcommit-threshold", type=float, default=DEFAULT_OVERCOMMIT)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data, _ = load_cluster(args.input)
    result = analyze(
        data,
        cpu_threshold=args.cpu_threshold,
        mem_threshold=args.mem_threshold,
        overcommit_threshold=args.overcommit_threshold,
    )
    if args.format == "json":
        json.dump(result_to_json(result), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_table(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
