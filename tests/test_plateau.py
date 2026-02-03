from app.analytics.plateau import detect_plateau


def test_detects_plateau_flat_trend():
    # Nearly flat weights across 14 days (tiny noise)
    weighins = [
        {"date": f"2026-01-{d:02d}", "weight": 150.0 + (0.05 if d % 2 == 0 else -0.05)}
        for d in range(1, 15)
    ]
    result = detect_plateau(weighins, window_days=14, min_weighins=10)
    assert result["detected"] is True
    assert result["severity"] in ("mild", "strong")


def test_not_plateau_clear_loss():
    # Clear downward trend across 14 days
    weighins = [
        {"date": f"2026-01-{d:02d}", "weight": 155.0 - (d * 0.20)}
        for d in range(1, 15)
    ]
    result = detect_plateau(weighins, window_days=14, min_weighins=10)
    assert result["detected"] is False


def test_not_plateau_insufficient_weighins():
    # Only 6 weigh-ins in the window, should fail the "enough data" gate
    weighins = [
        {"date": "2026-01-01", "weight": 150.0},
        {"date": "2026-01-03", "weight": 150.1},
        {"date": "2026-01-05", "weight": 150.0},
        {"date": "2026-01-07", "weight": 150.2},
        {"date": "2026-01-10", "weight": 150.1},
        {"date": "2026-01-14", "weight": 150.0},
    ]
    result = detect_plateau(weighins, window_days=14, min_weighins=10)
    assert result["detected"] is False
    assert result["reason"] == "insufficient_weighins"

