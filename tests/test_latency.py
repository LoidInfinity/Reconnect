"""Latency recording, log display and SVG chart regressions."""
from datetime import datetime, timedelta
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import latency
import main


def make_logger(name="test.latency"):
    logger = logging.getLogger(name)
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


class LatencyStoreTests(unittest.TestCase):
    def test_records_success_and_failure_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latency.csv"
            latency.record_latency("https://example.org", 42.4, True, path=path)
            latency.record_latency("", None, False, path=path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("https://example.org", text)
            self.assertIn(",42,1", text)
            self.assertIn(",,0", text)

    def test_read_series_filters_window_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latency.csv"
            old = (datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds")
            fresh = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
            path.write_text(
                f"{old},https://old.example,10,1\n"
                f"{fresh},https://example.org,55,1\n"
                f"{fresh},,,0\n",
                encoding="utf-8",
            )
            points, failures = latency.read_series(hours=24, path=path)
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0][1], 55.0)
            self.assertEqual(len(failures), 1)

    def test_read_by_target_keeps_order_and_honours_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latency.csv"
            now = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
            path.write_text(
                f"{now},https://a.example,30,1\n"
                f"{now},https://a.example,,0\n"
                f"{now},https://b.example,50,1\n"
                f"{now},https://c.example,70,1\n",
                encoding="utf-8",
            )
            series = latency.read_by_target(["https://a.example", "https://b.example"], path=path)
            self.assertEqual(list(series), ["https://a.example", "https://b.example"])
            self.assertEqual(len(series["https://a.example"]["points"]), 1)
            self.assertEqual(series["https://a.example"]["points"][0][1], 30.0)
            self.assertEqual(len(series["https://a.example"]["failures"]), 1)
            self.assertEqual(series["https://b.example"]["points"][0][1], 50.0)
            self.assertNotIn("https://c.example", series)

    def test_envelope_min_and_max(self):
        now = datetime.now()
        series = {
            "a": {"points": [(now, 20.0), (now + timedelta(minutes=1), 80.0)], "failures": []},
            "b": {"points": [(now, 60.0), (now + timedelta(minutes=1), 40.0)], "failures": []},
        }
        lows, highs = latency.envelope(series)
        self.assertEqual([value for _, value in lows], [20.0, 40.0])
        self.assertEqual([value for _, value in highs], [60.0, 80.0])
        lows_one, highs_one = latency.envelope(series, selected={"a"})
        self.assertEqual([value for _, value in highs_one], [20.0, 80.0])

    def test_stats_and_svg(self):
        now = datetime.now()
        points = [(now - timedelta(minutes=2), 20.0), (now, 60.0)]
        summary = latency.stats(points)
        self.assertEqual((summary["min"], summary["max"]), (20.0, 60.0))
        svg = latency.render_svg(points, [now], hours=24)
        self.assertIn("<svg", svg)
        self.assertIn("<polyline", svg)
        self.assertIn("最新 60 ms", svg)
        self.assertIn("平均 40 ms", svg)
        self.assertIn("最近 24 小时", svg)

    def test_empty_chart_is_explained(self):
        svg = latency.render_svg([], [], hours=6)
        self.assertIn("暂无延迟数据", svg)
        self.assertIn("最近 6 小时", svg)

    def test_write_chart_creates_svg(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            data = directory_path / "latency.csv"
            latency.record_latency("https://example.org", 33.0, True, path=data)
            output = latency.write_chart(directory_path / "charts", hours=24, path=data)
            self.assertTrue(output.is_file())
            self.assertEqual(output.suffix, ".svg")
            self.assertIn("<polyline", output.read_text(encoding="utf-8"))


class HttpLatencyTests(unittest.TestCase):
    class _Response:
        status = 200

        def geturl(self):
            return "https://example.org"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def setUp(self):
        self.logger = make_logger("test.latency.http")

    def test_one_target_failure_does_not_mean_offline(self):
        class FailOpener:
            def open(self, target, timeout=None):
                raise OSError("timeout")

        class OkOpener:
            def open(self, target, timeout=None):
                return HttpLatencyTests._Response()

        with patch("urllib.request.build_opener",
                   side_effect=[FailOpener(), OkOpener()]), \
             patch.object(latency, "record_latency") as record:
            self.assertTrue(main.http_reachable(
                self.logger, ["https://bad.example", "https://example.org"]))
        record.assert_any_call("https://bad.example", None, False)
        target, elapsed, ok = record.call_args_list[-1][0]
        self.assertEqual(target, "https://example.org")
        self.assertTrue(ok)
        self.assertIsNotNone(main.last_latency_ms())

    def test_only_first_five_targets_are_probed(self):
        opened = []

        class Opener:
            def open(self, target, timeout=None):
                opened.append(target)
                raise OSError("down")

        targets = [f"https://t{index}.example" for index in range(6)]
        with patch("urllib.request.build_opener", return_value=Opener()), \
             patch.object(latency, "record_latency") as record:
            self.assertFalse(main.http_reachable(self.logger, targets))
        self.assertEqual(opened, targets[:5])
        self.assertEqual(record.call_count, 5)

    def test_successful_probe_records_latency(self):
        class Opener:
            def open(self, target, timeout=None):
                return HttpLatencyTests._Response()

        with patch("urllib.request.build_opener", return_value=Opener()), \
             patch.object(latency, "record_latency") as record:
            self.assertTrue(main.http_reachable(self.logger, ["https://example.org"]))
        record.assert_called_once()
        target, elapsed, ok = record.call_args[0]
        self.assertEqual(target, "https://example.org")
        self.assertTrue(ok)
        self.assertGreaterEqual(elapsed, 0.0)
        self.assertIsNotNone(main.last_latency_ms())

    def test_failed_probe_records_failure_marker(self):
        class Opener:
            def open(self, target, timeout=None):
                raise OSError("down")

        with patch("urllib.request.build_opener", return_value=Opener()), \
             patch.object(latency, "record_latency") as record:
            self.assertFalse(main.http_reachable(self.logger, ["https://example.org"]))
        record.assert_called_once_with("https://example.org", None, False)
        self.assertIsNone(main.last_latency_ms())


if __name__ == "__main__":
    unittest.main()
