#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pve_balance import analyze, load_cluster, normalize_cpu_usage  # noqa: E402


SAMPLE = {
    "cluster": "test",
    "collected_at": "2026-01-01T00:00:00+07:00",
    "nodes": [
        {"node": "n1", "status": "online", "cpu_cores": 8, "mem_total": 32 * 1024**3},
        {"node": "n2", "status": "online", "cpu_cores": 8, "mem_total": 32 * 1024**3},
        {"node": "n3", "status": "offline", "cpu_cores": 8, "mem_total": 32 * 1024**3},
        {"node": "n4", "status": "online", "cpu_cores": 8, "mem_total": 262144},
    ],
    "vms": [
        {
            "vmid": 1,
            "name": "heavy",
            "node": "n1",
            "status": "running",
            "vcpus": 4,
            "cpu_usage": 0.9,
            "mem_max": 20 * 1024**3,
            "mem_used": 28 * 1024**3,
        },
        {
            "vmid": 2,
            "name": "light",
            "node": "n1",
            "status": "running",
            "vcpus": 2,
            "cpu_usage": 0.1,
            "mem_max": 4 * 1024**3,
            "mem_used": 1 * 1024**3,
        },
        {
            "vmid": 3,
            "name": "pct",
            "node": "n2",
            "status": "running",
            "vcpus": 2,
            "cpu_usage": 50.0,
            "mem_max": 4 * 1024**3,
            "mem_used": 2 * 1024**3,
        },
        {
            "vmid": 4,
            "name": "stopped",
            "node": "n2",
            "status": "stopped",
            "vcpus": 8,
            "cpu_usage": 0.9,
            "mem_max": 16 * 1024**3,
            "mem_used": 0,
        },
        {
            "vmid": 5,
            "name": "ghost",
            "node": "n99",
            "status": "running",
            "vcpus": 2,
            "cpu_usage": 0.5,
            "mem_max": 2 * 1024**3,
            "mem_used": 1 * 1024**3,
        },
        {
            "vmid": 6,
            "name": "on-bad-mem",
            "node": "n4",
            "status": "running",
            "vcpus": 2,
            "cpu_usage": 0.2,
            "mem_max": 2 * 1024**3,
            "mem_used": 1 * 1024**3,
        },
    ],
}


class NormalizeCpuTests(unittest.TestCase):
    def test_fraction(self):
        w = []
        self.assertEqual(normalize_cpu_usage(0.42, 1, w), 0.42)
        self.assertEqual(w, [])

    def test_percent(self):
        w = []
        self.assertAlmostEqual(normalize_cpu_usage(31.0, 1, w), 0.31)
        self.assertEqual(w[0].code, "cpu_usage_percent")

    def test_invalid(self):
        w = []
        self.assertIsNone(normalize_cpu_usage(250, 1, w))
        self.assertEqual(w[0].code, "cpu_usage_out_of_range")


class AnalyzeTests(unittest.TestCase):
    def test_overload_and_warnings(self):
        result = analyze(SAMPLE, cpu_threshold=0.85, mem_threshold=0.85, overcommit_threshold=2.0)
        by = {n.node: n for n in result.nodes}
        self.assertTrue(by["n1"].overloaded)
        self.assertFalse(by["n3"].overloaded)  # offline → không đánh overload theo tải
        self.assertFalse(by["n4"].valid)
        codes = {w.code for w in result.warnings}
        self.assertIn("unknown_node", codes)
        self.assertIn("cpu_usage_percent", codes)
        self.assertIn("suspicious_mem_total", codes)

    def test_stopped_not_counted(self):
        result = analyze(SAMPLE)
        n2 = next(n for n in result.nodes if n.node == "n2")
        # chỉ VM pct (2 vCPU), không tính stopped 8 vCPU
        self.assertEqual(n2.vcpu_alloc, 2)

    def test_migrate_not_to_offline_or_bad_mem(self):
        result = analyze(SAMPLE, cpu_threshold=0.85, mem_threshold=0.85, overcommit_threshold=2.0)
        for m in result.migrations:
            self.assertNotEqual(m.to_node, "n3")
            self.assertNotEqual(m.to_node, "n4")
            self.assertNotEqual(m.to_node, "n99")

    def test_migrate_reduces_source_pressure(self):
        result = analyze(SAMPLE, cpu_threshold=0.85, mem_threshold=0.85, overcommit_threshold=2.0)
        self.assertTrue(any(m.from_node == "n1" for m in result.migrations))
        self.assertNotIn("n2", result.residual_overloaded)

    def test_bad_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not-json")
            path = Path(f.name)
        with self.assertRaises(SystemExit):
            load_cluster(path)

    def test_missing_fields_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"cluster": "x"}, f)
            path = Path(f.name)
        with self.assertRaises(SystemExit):
            load_cluster(path)


class DuplicateVmidTests(unittest.TestCase):
    def test_duplicate_vmid_warn_first_seen_no_migrate(self):
        data = {
            "cluster": "dup",
            "collected_at": "2026-01-01T00:00:00+07:00",
            "nodes": [
                {"node": "n1", "status": "online", "cpu_cores": 8, "mem_total": 64 * 1024**3},
                {"node": "n2", "status": "online", "cpu_cores": 8, "mem_total": 64 * 1024**3},
            ],
            "vms": [
                {
                    "vmid": 2001,
                    "name": "gitlab",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 4,
                    "cpu_usage": 0.5,
                    "mem_max": 32 * 1024**3,
                    "mem_used": 20 * 1024**3,
                },
                {
                    "vmid": 2001,
                    "name": "gitlab",
                    "node": "n2",
                    "status": "running",
                    "vcpus": 4,
                    "cpu_usage": 0.5,
                    "mem_max": 32 * 1024**3,
                    "mem_used": 20 * 1024**3,
                },
                {
                    "vmid": 10,
                    "name": "other",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.9,
                    "mem_max": 16 * 1024**3,
                    "mem_used": 40 * 1024**3,
                },
            ],
        }
        result = analyze(data, cpu_threshold=0.85, mem_threshold=0.85, overcommit_threshold=2.0)
        codes = [w.code for w in result.warnings]
        self.assertIn("duplicate_vmid", codes)
        by = {n.node: n for n in result.nodes}
        # first-valid trên n1: cộng 20GiB; n2 không cộng lần 2
        self.assertEqual(by["n1"].mem_used, 60 * 1024**3)
        self.assertEqual(by["n2"].mem_used, 0)
        self.assertIn("vmid_conflict", by["n1"].reasons)
        self.assertIn("vmid_conflict", by["n2"].reasons)
        self.assertNotIn(2001, {m.vmid for m in result.migrations})

    def test_first_invalid_then_second_valid_wins(self):
        gi = 1024**3
        data = {
            "cluster": "dup-bad-first",
            "collected_at": "2026-01-01T00:00:00+07:00",
            "nodes": [
                {"node": "n1", "status": "online", "cpu_cores": 8, "mem_total": 64 * gi},
                {"node": "n2", "status": "online", "cpu_cores": 8, "mem_total": 64 * gi},
            ],
            "vms": [
                {
                    "vmid": 2001,
                    "name": "bad",
                    "node": "n1",
                    "status": "running",
                    "vcpus": "x",
                    "cpu_usage": 0.5,
                    "mem_max": 10 * gi,
                    "mem_used": 8 * gi,
                },
                {
                    "vmid": 2001,
                    "name": "good",
                    "node": "n2",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.5,
                    "mem_max": 10 * gi,
                    "mem_used": 8 * gi,
                },
            ],
        }
        result = analyze(data)
        by = {n.node: n for n in result.nodes}
        self.assertEqual(by["n1"].mem_used, 0)
        self.assertEqual(by["n2"].mem_used, 8 * gi)
        self.assertAlmostEqual(by["n2"].cpu_used_cores, 1.0)

    def test_dirty_null_mem_pessimistic_on_other_node(self):
        gi = 1024**3
        data = {
            "cluster": "dup-null",
            "collected_at": "2026-01-01T00:00:00+07:00",
            "nodes": [
                {"node": "n1", "status": "online", "cpu_cores": 8, "mem_total": 32 * gi},
                {"node": "n2", "status": "online", "cpu_cores": 8, "mem_total": 32 * gi},
            ],
            "vms": [
                {
                    "vmid": 2001,
                    "name": "dupnull",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.1,
                    "mem_max": 28 * gi,
                    "mem_used": None,
                },
                {
                    "vmid": 2001,
                    "name": "dupnull",
                    "node": "n2",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.1,
                    "mem_max": 28 * gi,
                    "mem_used": None,
                },
                {
                    "vmid": 40,
                    "name": "heavy",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.9,
                    "mem_max": 8 * gi,
                    "mem_used": 6 * gi,
                },
            ],
        }
        result = analyze(data, cpu_threshold=0.99, mem_threshold=0.85, overcommit_threshold=8.0)
        # n2 báo cáo mem=0 nhưng pessimistic +28GiB → không nhận heavy 6GiB
        self.assertFalse(any(m.to_node == "n2" for m in result.migrations))
        by = {n.node: n for n in result.nodes}
        self.assertIn("vmid_conflict", by["n2"].reasons)

    def test_duplicate_dest_pessimistic_blocks_inbound(self):
        """n2 conflict nhưng chưa cộng dirty → nhận migrate phải kể load dirty."""
        gi = 1024**3
        data = {
            "cluster": "dup2",
            "collected_at": "2026-01-01T00:00:00+07:00",
            "nodes": [
                {"node": "n1", "status": "online", "cpu_cores": 8, "mem_total": 32 * gi},
                {"node": "n2", "status": "online", "cpu_cores": 8, "mem_total": 32 * gi},
            ],
            "vms": [
                {
                    "vmid": 2001,
                    "name": "dup",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.1,
                    "mem_max": 28 * gi,
                    "mem_used": 26 * gi,
                },
                {
                    "vmid": 2001,
                    "name": "dup",
                    "node": "n2",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.1,
                    "mem_max": 28 * gi,
                    "mem_used": 26 * gi,
                },
                {
                    "vmid": 11,
                    "name": "heavy",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.9,
                    "mem_max": 8 * gi,
                    "mem_used": 6 * gi,
                },
            ],
        }
        # n1 mem ≈ 32/32 = 100% → overload; n2 display 0 nhưng pessimistic +26GiB
        # nhận heavy 6GiB → 32/32 ≥ 85% → không nhận vào n2
        result = analyze(data, cpu_threshold=0.99, mem_threshold=0.85, overcommit_threshold=8.0)
        self.assertTrue(any(w.code == "duplicate_vmid" for w in result.warnings))
        self.assertFalse(any(m.to_node == "n2" for m in result.migrations))


class NullMemUsedTests(unittest.TestCase):
    def test_null_mem_used_m6_report_and_no_migrate(self):
        gi = 1024**3
        data = {
            "cluster": "nullmem",
            "collected_at": "2026-01-01T00:00:00+07:00",
            "nodes": [
                {"node": "n1", "status": "online", "cpu_cores": 8, "mem_total": 64 * gi},
                {"node": "n2", "status": "online", "cpu_cores": 8, "mem_total": 64 * gi},
            ],
            "vms": [
                {
                    "vmid": 2199,
                    "name": "wp",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 4,
                    "cpu_usage": 0.5,
                    "mem_max": 50 * gi,
                    "mem_used": None,
                },
                {
                    "vmid": 20,
                    "name": "ok",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.2,
                    "mem_max": 8 * gi,
                    "mem_used": 4 * gi,
                },
            ],
        }
        result = analyze(data)
        self.assertTrue(any(w.code == "null_mem_used" for w in result.warnings))
        n1 = next(n for n in result.nodes if n.node == "n1")
        self.assertEqual(n1.mem_used, 4 * gi)  # không cộng mem_max vào báo cáo
        self.assertAlmostEqual(n1.cpu_used_cores, 0.5 * 4 + 0.2 * 2)
        self.assertIn("mem_metrics_partial", n1.reasons)
        self.assertNotIn(2199, {m.vmid for m in result.migrations})

    def test_null_mem_zero_is_not_null(self):
        gi = 1024**3
        data = {
            "cluster": "zero",
            "collected_at": "2026-01-01T00:00:00+07:00",
            "nodes": [
                {"node": "n1", "status": "online", "cpu_cores": 8, "mem_total": 32 * gi},
            ],
            "vms": [
                {
                    "vmid": 1,
                    "name": "z",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.1,
                    "mem_max": 4 * gi,
                    "mem_used": 0,
                },
            ],
        }
        result = analyze(data)
        self.assertFalse(any(w.code == "null_mem_used" for w in result.warnings))
        self.assertEqual(result.nodes[0].mem_used, 0)

    def test_null_mem_without_max_skipped(self):
        gi = 1024**3
        data = {
            "cluster": "nomax",
            "collected_at": "2026-01-01T00:00:00+07:00",
            "nodes": [
                {"node": "n1", "status": "online", "cpu_cores": 8, "mem_total": 32 * gi},
            ],
            "vms": [
                {
                    "vmid": 2,
                    "name": "bad",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.5,
                    "mem_max": 0,
                    "mem_used": None,
                },
            ],
        }
        result = analyze(data)
        codes = {w.code for w in result.warnings}
        self.assertIn("null_mem_used", codes)
        self.assertIn("null_mem_used_no_max", codes)
        self.assertEqual(result.nodes[0].cpu_used_cores, 0.0)
        self.assertEqual(result.nodes[0].mem_used, 0)

    def test_null_mem_pessimistic_blocks_dest(self):
        gi = 1024**3
        data = {
            "cluster": "null-dest",
            "collected_at": "2026-01-01T00:00:00+07:00",
            "nodes": [
                {"node": "n1", "status": "online", "cpu_cores": 8, "mem_total": 32 * gi},
                {"node": "n2", "status": "online", "cpu_cores": 8, "mem_total": 32 * gi},
            ],
            "vms": [
                {
                    "vmid": 2199,
                    "name": "nullvm",
                    "node": "n2",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.1,
                    "mem_max": 28 * gi,
                    "mem_used": None,
                },
                {
                    "vmid": 30,
                    "name": "heavy",
                    "node": "n1",
                    "status": "running",
                    "vcpus": 2,
                    "cpu_usage": 0.9,
                    "mem_max": 16 * gi,
                    "mem_used": 30 * gi,
                },
            ],
        }
        # n2 báo cáo MEM≈0 nhưng reserve 28GiB → nhận +30 không đủ dưới 85%
        result = analyze(data, cpu_threshold=0.99, mem_threshold=0.85, overcommit_threshold=8.0)
        self.assertFalse(any(m.to_node == "n2" for m in result.migrations))


if __name__ == "__main__":
    unittest.main()
