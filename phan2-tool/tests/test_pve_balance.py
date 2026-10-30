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


if __name__ == "__main__":
    unittest.main()
