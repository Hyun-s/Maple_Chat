"""Small dependency-free metric registry with Prometheus text export."""

from __future__ import annotations

from collections import defaultdict


class MetricRegistry:
    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._observations: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = (
            defaultdict(list)
        )

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
        if not name.replace("_", "").isalnum():
            raise ValueError("metric name contains unsupported characters")
        return name, tuple(sorted(labels.items()))

    def increment(self, name: str, value: float = 1.0, **labels: str) -> None:
        if value < 0:
            raise ValueError("counter increment cannot be negative")
        self._counters[self._key(name, labels)] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        if value < 0:
            raise ValueError("metric observation cannot be negative")
        self._observations[self._key(name, labels)].append(value)

    @staticmethod
    def _labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        rendered = ",".join(f'{key}="{value}"' for key, value in labels)
        return "{" + rendered + "}"

    def render(self) -> str:
        lines: list[str] = []
        for (name, labels), value in sorted(self._counters.items()):
            lines.append(f"{name}_total{self._labels(labels)} {value:g}")
        for (name, labels), values in sorted(self._observations.items()):
            suffix = self._labels(labels)
            lines.append(f"{name}_count{suffix} {len(values)}")
            lines.append(f"{name}_sum{suffix} {sum(values):g}")
        return "\n".join(lines) + ("\n" if lines else "")
