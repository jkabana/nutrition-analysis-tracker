from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple

# ---------------------------
# Helpers
# ---------------------------

def _median(values: List[float]) -> float:
    if not values:
        raise ValueError("median() requires at least one value")
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0

def _to_date(d: Any) -> date:
    # Accept date, datetime, or ISO string
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        # Supports "YYYY-MM-DD" or ISO datetime
        return datetime.fromisoformat(d.replace("Z", "+00:00")).date()
    raise TypeError(f"Unsupported date type: {type(d)}")

def _linear_regression_slope(xs: List[float], ys: List[float]) -> float:
    """
    Returns slope (delta y per 1 unit x).
    """
    n = len(xs)
    if n < 2:
        return 0.0
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    den = sum((x - x_mean) ** 2 for x in xs)
    return 0.0 if den == 0 else num / den

def _rolling_mean(values: List[float], window: int) -> List[Optional[float]]:
    """
    Simple trailing moving average over available points (not calendar days).
    Returns list same length as input; first window-1 are None.
    """
    out: List[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if i + 1 >= window:
            chunk = values[i + 1 - window : i + 1]
            out[i] = sum(chunk) / window
    return out

def _ewma(values: List[float], span: int = 7) -> List[float]:
    """
    EWMA smoothing. Span ~ similar to moving average window length.
    """
    if not values:
        return []
    alpha = 2 / (span + 1)
    smoothed = [values[0]]
    for v in values[1:]:
        smoothed.append(alpha * v + (1 - alpha) * smoothed[-1])
    return smoothed

# ---------------------------
# Core function
# ---------------------------

def detect_plateau(
    weighins: List[Dict[str, Any]],
    *,
    window_days: int = 14,
    min_weighins: int = 10,
    ma_window_points: int = 7,
    slope_flat_lbs_per_week: float = 0.25,
    net_change_lbs: float = 0.5,
    strong_slope_lbs_per_week: float = 0.15,
    strong_net_change_lbs: float = 0.3,
    use_ewma_if_sparse: bool = True,
    sodium_series: Optional[List[Dict[str, Any]]] = None,
    sodium_spike_mg: float = 2300.0,
    sodium_spike_days_flag: int = 4,
) -> Dict[str, Any]:
    """
    weighins: list of {"date": <date|datetime|iso str>, "weight": float}
    sodium_series (optional): list of {"date": ..., "sodium": float}
    """

    # Validate and sort
    cleaned: List[Tuple[date, float]] = []
    for row in weighins:
        if row is None:
            continue
        d = _to_date(row.get("date") or row.get("logged_at") or row.get("weighed_at"))
        w = row.get("weight")
        if w is None:
            continue
        cleaned.append((d, float(w)))

    cleaned.sort(key=lambda x: x[0])

    if not cleaned:
        return {
            "detected": False,
            "reason": "no_weighins",
        }

    end_date = cleaned[-1][0]
    start_date = end_date - timedelta(days=window_days - 1)

    # Filter to window
    window_points = [(d, w) for d, w in cleaned if d >= start_date and d <= end_date]
    n = len(window_points)

    if n < max(2, min_weighins):
        return {
            "detected": False,
            "reason": "insufficient_weighins",
            "window_days": window_days,
            "weighins_count": n,
            "required_min": min_weighins,
            "window_start": start_date.isoformat(),
            "window_end": end_date.isoformat(),
        }

    dates = [d for d, _ in window_points]
    weights = [w for _, w in window_points]

    # Smoothing: prefer 7-point rolling mean, but if sparse (few points),
    # fall back to EWMA so we still get a usable trend line.
    smoothed: List[float] = []
    method = "rolling_mean"
    rm = _rolling_mean(weights, ma_window_points)
    if any(v is not None for v in rm):
        # take only non-None aligned portion
        aligned = [(dates[i], rm[i]) for i in range(len(rm)) if rm[i] is not None]
        if len(aligned) >= 2:
            dates_s = [d for d, _ in aligned]
            smoothed = [float(v) for _, v in aligned]
        else:
            smoothed = []
    else:
        smoothed = []

    if use_ewma_if_sparse and len(smoothed) < 2:
        method = "ewma"
        smoothed = _ewma(weights, span=ma_window_points)
        dates_s = dates  # EWMA aligns to all points
    else:
        # rolling mean dates already set
        pass

    # Regression slope on smoothed values
    # x in days from first smoothed point
    d0 = dates_s[0]
    xs = [(d - d0).days for d in dates_s]
    slope_lbs_per_day = _linear_regression_slope(xs, smoothed)
    slope_lbs_per_week = slope_lbs_per_day * 7.0

    # Net change via medians: first half vs last half of window
    # Use 7 and 7 by calendar intent, but with irregular points we approximate:
    # split by date midpoint.
    midpoint = start_date + timedelta(days=(window_days // 2))
    first = [w for d, w in window_points if d <= midpoint]
    last = [w for d, w in window_points if d > midpoint]

    # If split ends up empty due to sparse data, do simple half split by index
    if not first or not last:
        half = n // 2
        first = weights[:half] if half > 0 else weights[:1]
        last = weights[half:] if half < n else weights[-1:]

    first_med = _median(first)
    last_med = _median(last)
    net = last_med - first_med

    # Plateau gates
    flat_trend = abs(slope_lbs_per_week) < slope_flat_lbs_per_week
    small_change = abs(net) < net_change_lbs

    detected = flat_trend and small_change

    severity = None
    if detected:
        if abs(slope_lbs_per_week) < strong_slope_lbs_per_week and abs(net) < strong_net_change_lbs:
            severity = "strong"
        else:
            severity = "mild"

    flags: List[str] = []

    # Optional sodium flag
    if sodium_series:
        sodium_cleaned: List[Tuple[date, float]] = []
        for row in sodium_series:
            d = _to_date(row.get("date") or row.get("logged_at"))
            s = row.get("sodium")
            if s is None:
                continue
            sodium_cleaned.append((d, float(s)))
        sodium_window = [s for d, s in sodium_cleaned if d >= start_date and d <= end_date]
        spike_days = sum(1 for s in sodium_window if s >= sodium_spike_mg)
        if detected and spike_days >= sodium_spike_days_flag:
            flags.append("possible_water_retention_high_sodium")

    return {
        "detected": detected,
        "window_days": window_days,
        "window_start": start_date.isoformat(),
        "window_end": end_date.isoformat(),
        "weighins_count": n,
        "smoothing": method,
        "slope_lbs_per_week": round(slope_lbs_per_week, 3),
        "first_half_median": round(first_med, 2),
        "last_half_median": round(last_med, 2),
        "net_change_lbs": round(net, 2),
        "flat_trend": flat_trend,
        "small_change": small_change,
        "severity": severity,
        "flags": flags,
    }

