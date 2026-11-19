"""Deterministic daily-bar factors and an A-share agent tool."""

import json
import re
from datetime import date, timedelta
from typing import Annotated

import numpy as np
import pandas as pd

from ..data_source import CNStockUtils


FACTOR_DEFINITIONS = {
    "momentum_20d": {"formula": "close[t] / close[t-20] - 1", "unit": "decimal"},
    "momentum_60d": {"formula": "close[t] / close[t-60] - 1", "unit": "decimal"},
    "volatility_20d": {
        "formula": "sample_std(last 20 daily simple returns) * sqrt(252)",
        "unit": "decimal, annualized",
    },
    "avg_volume_20d": {"formula": "mean(last 20 daily volumes)", "unit": "lots/day"},
}


def compute_price_factors(bars: pd.DataFrame) -> pd.DataFrame:
    """Compute factors at each bar's close from observed daily bars.

    Accepts a date index and a Close column; Volume (lots) is optional. Windows
    count observed bars, not exchange sessions. Missing inputs are never filled.
    """
    if "Close" not in bars:
        raise ValueError("Daily bars must include a Close column.")
    if pd.api.types.is_numeric_dtype(bars.index.dtype):
        raise ValueError("Daily bars must be indexed by date, not row number.")
    dates = pd.DatetimeIndex(pd.to_datetime(bars.index, errors="raise")).normalize()
    if dates.hasnans or dates.has_duplicates:
        raise ValueError("Daily bar dates must be nonmissing and unique.")
    bars = bars.set_axis(dates.rename("Date")).sort_index()

    close = pd.to_numeric(bars["Close"], errors="coerce")
    close = close.where(np.isfinite(close) & (close > 0))
    volume = pd.to_numeric(
        bars.get("Volume", pd.Series(np.nan, index=bars.index)), errors="coerce"
    )
    volume = volume.where(np.isfinite(volume) & (volume >= 0))
    daily_return = close.div(close.shift(1)).sub(1)
    return pd.DataFrame(
        {
            "momentum_20d": close.div(close.shift(20)).sub(1),
            "momentum_60d": close.div(close.shift(60)).sub(1),
            "volatility_20d": daily_return.rolling(20).std(ddof=1) * np.sqrt(252),
            "avg_volume_20d": volume.rolling(20).mean(),
        },
        index=bars.index,
    ).replace([np.inf, -np.inf], np.nan)


def get_a_share_factors(
    symbol: Annotated[str, "Six-digit A-share stock code, for example 600519"],
    as_of: Annotated[str, "Inclusive cutoff date, YYYY-MM-DD; use after-close data"],
) -> str:
    """Calculate A-share 20/60-day momentum, annualized volatility and average volume."""
    if not re.fullmatch(r"[0-9]{6}", symbol):
        raise ValueError("Use a six-digit A-share code, including leading zeros.")
    cutoff = date.fromisoformat(as_of)
    if cutoff.isoformat() != as_of:
        raise ValueError("Use YYYY-MM-DD for as_of.")
    start = cutoff - timedelta(days=180)
    bars = CNStockUtils.get_stock_data(symbol, start.isoformat(), as_of, adjust="qfq")
    if bars is None or bars.empty:
        raise ValueError(f"No daily bars returned for {symbol} through {as_of}.")
    dates = pd.DatetimeIndex(bars.index).date
    bars = bars[(dates >= start) & (dates <= cutoff)]
    if bars.empty:
        raise ValueError(f"No daily bars for {symbol} within the requested window.")

    latest = compute_price_factors(bars).iloc[-1]
    return json.dumps(
        {
            "symbol": symbol,
            "requested_as_of": as_of,
            "data_date": latest.name.date().isoformat(),
            "source_tool": "CNStockUtils.get_stock_data",
            "price_adjustment": "qfq",
            "bar_count": len(bars),
            "factors": {
                name: {
                    **definition,
                    "value": None if pd.isna(latest[name]) else float(latest[name]),
                }
                for name, definition in FACTOR_DEFINITIONS.items()
            },
        },
        allow_nan=False,
    )
