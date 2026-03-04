"""Local usage tracker — records token usage per run and computes rolling windows.

Data is stored in ~/.ccflow/usage.jsonl, one JSON line per model per run.
Tracks usage separately per model (opus, sonnet, haiku, etc.).
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_USAGE_PATH = os.path.expanduser("~/.ccflow/usage.jsonl")


@dataclass
class WindowStats:
    """Token stats for a rolling time window."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    num_runs: int = 0

    @property
    def total(self) -> int:
        """Total tokens (input + output + cache write). Cache reads are cheap so excluded."""
        return self.input_tokens + self.output_tokens + self.cache_write_tokens


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO timestamp, handling both aware and naive formats."""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _model_family(model_id: str) -> str:
    """Extract short family name from a full model ID.

    'claude-opus-4-6' → 'opus', 'claude-sonnet-4-6' → 'sonnet',
    'claude-haiku-4-5-20251001' → 'haiku'
    """
    lower = model_id.lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in lower:
            return family
    return model_id


class UsageTracker:
    """Tracks Claude CLI token usage locally with rolling window aggregation.

    Records are stored per-model so you can see how many opus vs sonnet tokens
    you've burned in a given time window.

    Args:
        usage_path: Path to the JSONL file. Defaults to ~/.ccflow/usage.jsonl.
        limits_5h: Per-model token limits for 5h window, e.g. {"opus": 5_000_000}.
        limits_weekly: Per-model token limits for weekly window.
    """

    def __init__(
        self,
        usage_path: str | None = None,
        limits_5h: dict[str, int] | None = None,
        limits_weekly: dict[str, int] | None = None,
    ) -> None:
        self.usage_path = usage_path or DEFAULT_USAGE_PATH
        self.limits_5h = limits_5h or {}
        self.limits_weekly = limits_weekly or {}

    def record(self, model_usage: dict, session_id: str | None = None) -> None:
        """Append usage records from a result event's modelUsage field.

        model_usage is the `modelUsage` dict from the result event:
        {
            "claude-opus-4-6": {"inputTokens": 7, "outputTokens": 966, ...},
            "claude-sonnet-4-6": {...}
        }
        """
        ts = _now_utc().isoformat()
        path = Path(self.usage_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "a", encoding="utf-8") as f:
            for model_id, stats in model_usage.items():
                record = {
                    "ts": ts,
                    "model": _model_family(model_id),
                    "model_id": model_id,
                    "input_tokens": stats.get("inputTokens", 0),
                    "output_tokens": stats.get("outputTokens", 0),
                    "cache_read_input_tokens": stats.get("cacheReadInputTokens", 0),
                    "cache_creation_input_tokens": stats.get("cacheCreationInputTokens", 0),
                    "cost_usd": stats.get("costUSD"),
                    "session_id": session_id,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _load_records(self) -> list[dict]:
        """Load all records from the JSONL file."""
        path = Path(self.usage_path)
        if not path.exists():
            return []
        records = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def _aggregate(self, since: datetime) -> dict[str, WindowStats]:
        """Aggregate token usage per model for records since a given timestamp.

        Returns a dict mapping model family → WindowStats, plus a "__total__" key.
        """
        by_model: dict[str, WindowStats] = {}

        for rec in self._load_records():
            ts_str = rec.get("ts")
            if not ts_str:
                continue
            try:
                ts = _parse_ts(ts_str)
            except (ValueError, TypeError):
                continue
            if ts < since:
                continue

            model = rec.get("model", "unknown")
            if model not in by_model:
                by_model[model] = WindowStats()
            s = by_model[model]
            s.input_tokens += rec.get("input_tokens", 0)
            s.output_tokens += rec.get("output_tokens", 0)
            s.cache_read_tokens += rec.get("cache_read_input_tokens", 0)
            s.cache_write_tokens += rec.get("cache_creation_input_tokens", 0)
            s.num_runs += 1

        # Compute total across all models
        total = WindowStats()
        for s in by_model.values():
            total.input_tokens += s.input_tokens
            total.output_tokens += s.output_tokens
            total.cache_read_tokens += s.cache_read_tokens
            total.cache_write_tokens += s.cache_write_tokens
            total.num_runs += s.num_runs
        by_model["__total__"] = total

        return by_model

    def get_5h(self) -> dict[str, WindowStats]:
        """Get per-model token usage in the last 5 hours."""
        return self._aggregate(_now_utc() - timedelta(hours=5))

    def get_weekly(self) -> dict[str, WindowStats]:
        """Get per-model token usage in the last 7 days."""
        return self._aggregate(_now_utc() - timedelta(days=7))

    def format_stats(self) -> list[str]:
        """Return formatted lines for display in the result banner."""
        lines: list[str] = []

        stats_5h = self.get_5h()
        lines.extend(_format_window("5h window", stats_5h, self.limits_5h))

        stats_weekly = self.get_weekly()
        lines.extend(_format_window("Weekly", stats_weekly, self.limits_weekly))

        return lines


def _format_tokens(n: int) -> str:
    """Human-readable token count: 1.2M, 350k, 900."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _format_window(
    label: str,
    by_model: dict[str, WindowStats],
    limits: dict[str, int],
) -> list[str]:
    """Format a window section with per-model breakdown."""
    total = by_model.get("__total__", WindowStats())
    if total.num_runs == 0:
        return [f"{label + ':':<13} no usage"]

    lines: list[str] = []

    # Per-model lines (skip __total__, sort by total descending)
    models = sorted(
        ((m, s) for m, s in by_model.items() if m != "__total__"),
        key=lambda x: x[1].total,
        reverse=True,
    )

    for model, stats in models:
        tok = _format_tokens(stats.total)
        detail = f"{_format_tokens(stats.input_tokens)} in + {_format_tokens(stats.output_tokens)} out"
        if stats.cache_write_tokens:
            detail += f" + {_format_tokens(stats.cache_write_tokens)} cw"

        limit = limits.get(model)
        pct_str = ""
        if limit and stats.total > 0:
            pct = stats.total / limit * 100
            pct_str = f"  [{pct:.0f}%]"

        prefix = f"{label + ':':<13}" if label else " " * 13
        lines.append(f"{prefix} {model:<8} {tok:>6} tokens  ({detail}){pct_str}")
        label = ""

    return lines
