#!/usr/bin/env python3
"""Forecast Polymarket's "Next CEO of NVIDIA?" Yes prices, 4 hours ahead.

Per market, per minute h = 1..240 after the checkpoint, five quantiles
(5%, 25%, 50%, 75%, 95%) of that minute's price.

Method (stdlib only, deterministic, no network):
  * Median: the price at the checkpoint (persistence). Prediction-market
    prices are martingales in expectation; local tests showed any drift
    (momentum or mean reversion) hurts pinball loss badly.
  * Spread: a per-market volatility sigma, estimated from the history's
    h-step price changes at anchors h in {1, 5, 15, 60, 240} minutes.
    For each anchor, the robust scale of the changes is
    max(IQR / 1.349, 1.4826 * MAD), expressed per sqrt(minute), and
    sigma is the median of the anchors' values. The estimate blends
    three lookback windows (3h / 12h / 72h, weights 0.4 / 0.3 / 0.3)
    so it adapts to the current volatility regime while staying stable.
    These markets move in steps with rare huge jumps, so a robust scale
    (not a plain standard deviation) is what survives.
  * Quantiles: x + z_tau * sigma * sqrt(h), with the outer quantiles
    (|z| > 1) widened by 1.3x to cover the jump tails, clamped to [0, 1]
    and rounded to 6 decimals. The 75% quantile is deliberately NOT the
    Gaussian 0.674: these prices are step functions that sit flat most
    minutes and jump rarely, so the realised 75th percentile of moves
    sits only ~0.10-0.18 sigma*sqrt(h) above the median (measured on the
    six practice cases; the Gaussian 0.674 is far too wide and costs
    pinball loss on the common flat minutes). The 25% quantile keeps the
    Gaussian 0.674: the down side is wider than the up side in the centre.

Local practice score (p00-p05, p05 answers rebuilt from public
Polymarket trade history): pinball loss 0.267, vs 0.275 for the v2
Gaussian fan and 0.393 for the house baseline's fixed sigma of 0.0025
on the same cases. Walk-forward pseudo-cases (earlier 4h windows from
the same inputs): 0.280 vs v2's 0.286 (recent) and 2.571 vs 2.580
(early wild regime) — the change helps in both regimes.
"""

import json
import math
import os
from pathlib import Path

QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)
# Gaussian z-scores, except the 75% quantile: 0.15, not 0.674 (see docstring).
Z = (-1.644854, -0.674490, 0.0, 0.15, 1.644854)
ANCHORS = (1, 5, 15, 60, 240)
# (lookback minutes, weight) for the volatility blend.
WINDOWS = ((180, 0.4), (720, 0.3), (4320, 0.3))
SIGMA_FLOOR = 0.0004
SIGMA_CAP = 0.03
OUTER_WIDEN = 1.3


def _quantile(sorted_xs, tau):
    n = len(sorted_xs)
    if n == 0:
        return 0.0
    pos = tau * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def _median(xs):
    return _quantile(sorted(xs), 0.5)


def _upper_median(xs):
    """Upper median of a sorted list (element at index len // 2)."""
    s = sorted(xs)
    return s[len(s) // 2]


def _robust_scale_h(prices, h):
    """Robust scale (per sqrt(h) minute) of h-step price changes."""
    n = len(prices)
    if h >= n:
        return None
    step = max(1, h // 8)
    diffs = [prices[t] - prices[t - h] for t in range(h, n, step)]
    if len(diffs) < 8:
        return None
    med = _median(diffs)
    q25 = _quantile(sorted(diffs), 0.25)
    q75 = _quantile(sorted(diffs), 0.75)
    iqr_scale = (q75 - q25) / 1.349
    mad_scale = 1.4826 * _median([abs(d - med) for d in diffs])
    return max(iqr_scale, mad_scale) / math.sqrt(h)


def _sigma_for_window(prices, window):
    p = prices[-window:] if window and len(prices) > window else prices
    vals = []
    for h in ANCHORS:
        s = _robust_scale_h(p, h)
        if s is not None:
            vals.append(s)
    if not vals:
        return None
    return _upper_median(vals)


def estimate_sigma(prices):
    """Blend of robust volatilities over three lookback windows."""
    total = 0.0
    weight = 0.0
    for window, w in WINDOWS:
        s = _sigma_for_window(prices, window)
        if s is not None:
            total += w * s
            weight += w
    if weight == 0.0:
        return 0.0025
    sigma = total / weight
    return min(max(sigma, SIGMA_FLOOR), SIGMA_CAP)


def _price6(x):
    return round(min(1.0, max(0.0, x)), 6) + 0.0


def forecast_market(prices, horizon):
    x = prices[-1]
    sigma = estimate_sigma(prices)
    rows = []
    for h in range(1, horizon + 1):
        spread = sigma * math.sqrt(h)
        row = []
        for z in Z:
            widen = OUTER_WIDEN if abs(z) > 1.0 else 1.0
            row.append(_price6(x + z * widen * spread))
        for j in range(1, 5):
            row[j] = max(row[j], row[j - 1])
        rows.append(row)
    return rows


def main():
    input_dir = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
    output_dir = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs = sorted(p for p in input_dir.iterdir() if p.name.endswith(".in"))
    for path in inputs:
        case = json.loads(path.read_text(encoding="utf-8"))
        horizon = int(case["horizon_minutes"])
        forecasts = []
        for market in case["markets"]:
            prices = market["prices"]
            if not prices:
                raise ValueError(f"market {market['id']} has no prices")
            forecasts.append(
                {"id": market["id"], "q": forecast_market(prices, horizon)}
            )
        out_path = output_dir / (path.stem + ".out")
        out_path.write_text(
            json.dumps({"forecasts": forecasts}) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
