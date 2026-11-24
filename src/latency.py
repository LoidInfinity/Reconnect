"""Latency history and a dependency-free SVG line chart.

Every HTTP probe records its round-trip time to ``data/latency.csv``; the chart
is plain SVG so it can be generated everywhere (CLI, GUI, packaged build)
without matplotlib or Pillow.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from app_info import DATA_DIR

LATENCY_FILE = DATA_DIR / "latency.csv"
MAX_BYTES = 2 * 1024 * 1024
KEEP_LINES = 20000
MAX_POINTS = 600
MAX_TARGETS = 5


def effective_targets(targets) -> list[str]:
    """Only the first five unique HTTP targets are probed and charted."""
    ordered: list[str] = []
    for target in targets or []:
        value = str(target).strip()
        if value and value not in ordered:
            ordered.append(value)
    return ordered[:MAX_TARGETS]


def _trim(path: Path) -> None:
    try:
        if path.stat().st_size < MAX_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        path.write_text("\n".join(lines[-KEEP_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        return


def record_latency(target: str, latency_ms: float | None, ok: bool,
                   path: Path | None = None) -> None:
    """Append one probe result; never let disk errors break the monitor loop."""
    target_path = Path(path or LATENCY_FILE)
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _trim(target_path)
        with target_path.open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([
                datetime.now().isoformat(timespec="seconds"),
                target,
                "" if latency_ms is None else f"{latency_ms:.0f}",
                "1" if ok else "0",
            ])
    except OSError:
        return


def read_by_target(targets, hours: int = 24, path: Path | None = None) -> dict:
    """Per-target series for the first five configured check addresses.

    Returns ``{target: {"points": [(time, ms)...], "failures": [time...]}}`` in
    configuration order; targets outside the first five are ignored.
    """
    ordered = effective_targets(targets)
    series = {target: {"points": [], "failures": []} for target in ordered}
    target_path = Path(path or LATENCY_FILE)
    cutoff = datetime.now() - timedelta(hours=max(1, hours))
    try:
        with target_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            for row in csv.reader(handle):
                if len(row) < 4:
                    continue
                target = row[1]
                if target not in series:
                    continue
                try:
                    stamp = datetime.fromisoformat(row[0])
                except ValueError:
                    continue
                if stamp < cutoff:
                    continue
                if row[3] != "1":
                    series[target]["failures"].append(stamp)
                    continue
                try:
                    series[target]["points"].append((stamp, float(row[2])))
                except ValueError:
                    continue
    except OSError:
        pass
    return series


def envelope(series: dict, selected=None, bucket_seconds: int = 60):
    """Min/max latency per time bucket across the selected target series."""
    buckets: dict[datetime, list[float]] = {}
    for target, item in series.items():
        if selected is not None and target not in selected:
            continue
        for stamp, value in item.get("points", []):
            key = stamp.replace(second=0, microsecond=0)
            buckets.setdefault(key, []).append(value)
    lows, highs = [], []
    for stamp in sorted(buckets):
        values = buckets[stamp]
        lows.append((stamp, min(values)))
        highs.append((stamp, max(values)))
    return lows, highs


def read_series(hours: int = 24, path: Path | None = None) -> tuple[list[tuple[datetime, float]],
                                                                   list[datetime]]:
    """Return (successful latency points, failure timestamps) in the window."""
    target_path = Path(path or LATENCY_FILE)
    cutoff = datetime.now() - timedelta(hours=max(1, hours))
    points: list[tuple[datetime, float]] = []
    failures: list[datetime] = []
    try:
        with target_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            for row in csv.reader(handle):
                if len(row) < 4:
                    continue
                try:
                    stamp = datetime.fromisoformat(row[0])
                except ValueError:
                    continue
                if stamp < cutoff:
                    continue
                if row[3] != "1":
                    failures.append(stamp)
                    continue
                try:
                    points.append((stamp, float(row[2])))
                except ValueError:
                    continue
    except OSError:
        pass
    return points, failures


def _downsample(points: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    if len(points) <= MAX_POINTS:
        return points
    bucket = len(points) / MAX_POINTS
    result = []
    for index in range(MAX_POINTS):
        chunk = points[int(index * bucket):max(int(index * bucket) + 1, int((index + 1) * bucket))]
        if not chunk:
            continue
        result.append((chunk[len(chunk) // 2][0], sum(value for _, value in chunk) / len(chunk)))
    return result


def stats(points: list[tuple[datetime, float]]) -> dict[str, float]:
    if not points:
        return {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0, "last": 0.0}
    values = [value for _, value in points]
    return {
        "count": len(values),
        "avg": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
        "last": values[-1],
    }


def render_svg(points: list[tuple[datetime, float]], failures: list[datetime],
               hours: int = 24, width: int = 1100, height: int = 420) -> str:
    """Build a self-contained SVG chart (line + failure markers)."""
    left, right, top, bottom = 70, 30, 50, 55
    plot_w = width - left - right
    plot_h = height - top - bottom
    summary = stats(points)

    if points:
        y_max = max(10.0, summary["max"] * 1.15)
        first, last = points[0][0], points[-1][0]
    else:
        y_max = 100.0
        first = last = datetime.now() - timedelta(hours=hours)

    span = max(1.0, (last - first).total_seconds())

    def x_of(stamp: datetime) -> float:
        return left + min(1.0, max(0.0, (stamp - first).total_seconds() / span)) * plot_w

    def y_of(value: float) -> float:
        return top + (1 - min(1.0, value / y_max)) * plot_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Microsoft YaHei, Segoe UI, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{left}" y="26" font-size="17" font-weight="bold" fill="#1F3864">'
        f'Network Guard 网络延迟（最近 {hours} 小时）</text>',
        f'<text x="{width - right}" y="26" text-anchor="end" font-size="12" fill="#64748b">'
        f'样本 {summary["count"]} · 平均 {summary["avg"]:.0f} ms · 最高 {summary["max"]:.0f} ms</text>',
    ]

    for step in range(5):
        value = y_max * step / 4
        y = y_of(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                     f'stroke="#e2e8f0" stroke-width="1"/>')
        parts.append(f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
                     f'fill="#64748b">{value:.0f}</text>')

    parts.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" '
                 f'stroke="#334155" stroke-width="1.5"/>')
    parts.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" '
                 f'stroke="#334155" stroke-width="1.5"/>')
    parts.append(f'<text x="{left - 46}" y="{top + 10}" font-size="11" fill="#64748b">ms</text>')

    if points:
        sampled = _downsample(points)
        coords = " ".join(f"{x_of(stamp):.1f},{y_of(value):.1f}" for stamp, value in sampled)
        parts.append(f'<polyline points="{coords}" fill="none" stroke="#2563eb" '
                     f'stroke-width="2" stroke-linejoin="round"/>')
        last_x, last_y = x_of(sampled[-1][0]), y_of(sampled[-1][1])
        parts.append(f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="#2563eb"/>')
        parts.append(f'<text x="{last_x:.1f}" y="{last_y - 10:.1f}" text-anchor="end" '
                     f'font-size="11" fill="#1d4ed8">最新 {sampled[-1][1]:.0f} ms</text>')
    else:
        parts.append(f'<text x="{left + plot_w / 2}" y="{top + plot_h / 2}" text-anchor="middle" '
                     f'font-size="15" fill="#94a3b8">暂无延迟数据</text>')

    for stamp in failures[:200]:
        x = x_of(stamp)
        parts.append(f'<line x1="{x:.1f}" y1="{top + plot_h - 8}" x2="{x:.1f}" '
                     f'y2="{top + plot_h}" stroke="#dc2626" stroke-width="3" opacity="0.6"/>')

    for index in range(5):
        stamp = first + timedelta(seconds=span * index / 4)
        x = left + plot_w * index / 4
        parts.append(f'<text x="{x:.1f}" y="{top + plot_h + 20}" text-anchor="middle" '
                     f'font-size="11" fill="#64748b">{stamp.strftime("%m-%d %H:%M")}</text>')

    parts.append(f'<text x="{left}" y="{height - 12}" font-size="11" fill="#94a3b8">'
                 f'蓝色为 HTTP 往返延迟，红色竖线为检测失败时刻</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def write_chart(directory: Path | None = None, hours: int = 24,
                path: Path | None = None) -> Path:
    """Write a timestamped SVG chart and return its path."""
    target_dir = Path(directory) if directory else DATA_DIR / "charts"
    target_dir.mkdir(parents=True, exist_ok=True)
    points, failures = read_series(hours=hours, path=path)
    output = target_dir / f"NetworkGuard-latency-{datetime.now():%Y%m%d-%H%M%S}.svg"
    output.write_text(render_svg(points, failures, hours=hours), encoding="utf-8")
    return output
