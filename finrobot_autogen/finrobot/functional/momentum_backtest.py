"""Signal-driven A-share rule backtesting executed by the V2 Backtrader engine.

The rule language is a small, validated JSON DSL. It is converted into a
close-time 0/1 target series and then executed by ``run_signal_backtest`` from
``quantitative.py``, so costs, lot size, position limits and the cash ledger are
identical to the V2 fixed-rule experiment.
"""

import json
import re
from datetime import date, timedelta
from typing import Annotated, Any, Mapping

import numpy as np
import pandas as pd

from ..data_source import CNStockUtils
from .quant_tools import load_config
from .quantitative import audit_backtest, run_signal_backtest


DEFAULT_MOMENTUM_RULE = {
    "name": "positive_20d_momentum",
    "signal": {
        "all": [
            {"indicator": "momentum", "window": 20, "operator": ">", "value": 0},
        ]
    },
}
_OPERATORS = {">", ">=", "<", "<=", "=="}
_INDICATORS = {"momentum", "average_volume_ratio", "moving_average_cross"}
_FIELDS = {"close": "Close", "volume": "Volume"}
_MAX_WINDOW = 252
_MAX_CONDITIONS = 12
_COST_KEYS = (
    "initial_cash", "max_position", "lot_size", "commission",
    "min_commission", "slippage", "annual_periods", "risk_free_rate",
)
_COST_DEFAULTS = {
    "initial_cash": 100000.0, "max_position": 0.8, "lot_size": 100,
    "commission": 0.0003, "min_commission": 5.0, "slippage": 0.0005,
    "annual_periods": 252, "risk_free_rate": 0.0,
}


def _prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(bars, pd.DataFrame):
        raise ValueError("Daily bars must be a pandas DataFrame.")
    if "Open" not in bars or "Close" not in bars:
        raise ValueError("Daily bars must include Open and Close columns.")
    dates = pd.DatetimeIndex(pd.to_datetime(bars.index, errors="raise")).normalize()
    if dates.hasnans or dates.has_duplicates:
        raise ValueError("Daily bar dates must be nonmissing and unique.")
    return bars.copy().set_axis(dates.rename("Date")).sort_index()


def _positive_window(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_WINDOW:
        raise ValueError(f"{name} must be an integer between 1 and {_MAX_WINDOW}.")
    return value


def _validate_condition(condition: Mapping[str, Any]) -> int:
    if not isinstance(condition, Mapping):
        raise ValueError("Each signal condition must be an object.")
    if "all" in condition or "any" in condition:
        keys = [key for key in ("all", "any") if key in condition]
        if len(keys) != 1 or not isinstance(condition[keys[0]], list) or not condition[keys[0]]:
            raise ValueError("A logical signal must contain a nonempty all or any list.")
        if len(condition[keys[0]]) > _MAX_CONDITIONS:
            raise ValueError(f"A signal may contain at most {_MAX_CONDITIONS} conditions.")
        if set(condition) != {keys[0]}:
            raise ValueError("Logical signal objects may contain only all or any.")
        windows = [_validate_condition(item) for item in condition[keys[0]]]
        return max(windows)
    indicator = condition.get("indicator")
    if indicator not in _INDICATORS:
        raise ValueError(f"Unsupported indicator: {indicator!r}.")
    allowed = {
        "momentum": {"indicator", "window", "operator", "value"},
        "average_volume_ratio": {"indicator", "short_window", "long_window", "operator", "value"},
        "moving_average_cross": {
            "indicator", "field", "short_window", "long_window", "operator", "value"
        },
    }[indicator]
    if not set(condition).issubset(allowed) or not {"indicator", "operator"}.issubset(condition):
        raise ValueError(f"Invalid fields for {indicator} condition.")
    operator = condition.get("operator")
    if operator not in _OPERATORS:
        raise ValueError(f"Unsupported operator: {operator!r}.")
    value = condition.get("value", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
        raise ValueError("Signal condition value must be a finite number.")
    if indicator == "momentum":
        return _positive_window(condition.get("window"), "window")
    if indicator == "average_volume_ratio":
        short = _positive_window(condition.get("short_window"), "short_window")
        long = _positive_window(condition.get("long_window"), "long_window")
        if short > long:
            raise ValueError("short_window must not exceed long_window.")
        return long
    field = condition.get("field", "close")
    if field not in _FIELDS:
        raise ValueError("moving_average_cross field must be close or volume.")
    short = _positive_window(condition.get("short_window"), "short_window")
    long = _positive_window(condition.get("long_window"), "long_window")
    if short > long:
        raise ValueError("short_window must not exceed long_window.")
    return long


def validate_strategy_rule(rule: Mapping[str, Any]) -> dict:
    """Validate and copy the supported strategy DSL before it reaches the engine."""
    if not isinstance(rule, Mapping):
        raise ValueError("Strategy rule must be a JSON object.")
    if not set(rule).issubset({"name", "signal", "execution"}):
        raise ValueError("Strategy rule contains unsupported top-level fields.")
    signal = rule.get("signal")
    if not isinstance(signal, Mapping):
        raise ValueError("Strategy rule must contain a signal object.")
    if set(signal) not in ({"all"}, {"any"}) or not isinstance(signal[next(iter(signal))], list):
        raise ValueError("The top-level signal must contain all or any.")
    _validate_condition(signal)
    if "name" in rule and (not isinstance(rule["name"], str) or not rule["name"].strip()):
        raise ValueError("Strategy name must be a nonempty string.")
    if "execution" in rule and rule["execution"] != "next_open":
        raise ValueError("Only next_open execution is supported.")
    normalized = json.loads(json.dumps(dict(rule), ensure_ascii=False))
    normalized["name"] = normalized.get("name", "custom_strategy")
    normalized["execution"] = "next_open"
    return normalized


def _clean_series(bars: pd.DataFrame, field: str) -> pd.Series:
    if field == "volume" and "Volume" not in bars:
        raise ValueError("This strategy requires the Volume column.")
    values = pd.to_numeric(bars[_FIELDS[field]], errors="coerce")
    if field == "volume":
        return values.where(np.isfinite(values) & (values >= 0))
    return values.where(np.isfinite(values) & (values > 0))


def _indicator_values(bars: pd.DataFrame, condition: Mapping[str, Any]) -> pd.Series:
    indicator = condition["indicator"]
    if indicator == "momentum":
        close = _clean_series(bars, "close")
        window = condition["window"]
        return close.div(close.shift(window)).sub(1).replace([np.inf, -np.inf], np.nan)
    if indicator == "average_volume_ratio":
        volume = _clean_series(bars, "volume")
        short = volume.rolling(condition["short_window"]).mean()
        long = volume.rolling(condition["long_window"]).mean()
        return short.div(long).replace([np.inf, -np.inf], np.nan)
    field = condition.get("field", "close")
    values = _clean_series(bars, field)
    short = values.rolling(condition["short_window"]).mean()
    long = values.rolling(condition["long_window"]).mean()
    return short.sub(long)


def _compare(values: pd.Series, operator: str, threshold: Any) -> tuple[pd.Series, pd.Series]:
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("Signal condition value must be numeric.")
    valid = values.notna()
    if operator == ">":
        result = values > threshold
    elif operator == ">=":
        result = values >= threshold
    elif operator == "<":
        result = values < threshold
    elif operator == "<=":
        result = values <= threshold
    else:
        result = values == threshold
    return result.fillna(False), valid


def _condition_signal(bars: pd.DataFrame, condition: Mapping[str, Any]) -> tuple[pd.Series, pd.Series]:
    if "all" in condition or "any" in condition:
        key = "all" if "all" in condition else "any"
        parts = [_condition_signal(bars, item) for item in condition[key]]
        values = pd.concat([part[0] for part in parts], axis=1)
        valid = pd.concat([part[1] for part in parts], axis=1).all(axis=1)
        result = values.all(axis=1) if key == "all" else values.any(axis=1)
        return result.where(valid, False), valid
    values = _indicator_values(bars, condition)
    return _compare(values, condition["operator"], condition.get("value", 0))


def build_strategy_signals(bars: pd.DataFrame, rule: Mapping[str, Any]) -> pd.Series:
    """Build close-time target positions from a validated strategy rule.

    Unavailable warm-up values stay as missing values so callers cannot silently
    trade them; the executor fills them with flat positions and checks that the
    signal at the evaluation start is available.
    """
    bars = _prepare_bars(bars)
    rule = validate_strategy_rule(rule)
    signal, valid = _condition_signal(bars, rule["signal"])
    return signal.where(valid).astype("boolean")


def build_momentum_signals(bars: pd.DataFrame, window: int = 20, threshold: float = 0) -> pd.Series:
    rule = {
        "name": f"positive_{window}d_momentum",
        "signal": {"all": [{"indicator": "momentum", "window": window,
                              "operator": ">", "value": threshold}]},
    }
    return build_strategy_signals(bars, rule)


def _benchmark_config(start: str) -> dict:
    saved = load_config()
    config = {key: saved.get(key, _COST_DEFAULTS[key]) for key in _COST_KEYS}
    config["evaluation_start"] = start
    return config


def _load_bars(symbol: str, start: str, end: str, rule: Mapping[str, Any]):
    if not re.fullmatch(r"[0-9]{6}", symbol):
        raise ValueError("Use a six-digit A-share code, including leading zeros.")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if start != first.isoformat() or end != last.isoformat() or first >= last:
        raise ValueError("Use YYYY-MM-DD with start earlier than end.")
    lookback = _validate_condition(rule["signal"])
    bars = CNStockUtils.get_stock_data(
        symbol,
        (first - timedelta(days=max(180, lookback * 3))).isoformat(),
        end,
        adjust="qfq",
    )
    if bars is None or bars.empty:
        raise ValueError(f"No daily bars returned for {symbol}.")
    return first, last, bars


def _csi300_open(start: str, end: str) -> pd.Series:
    import akshare as ak

    index = ak.stock_zh_index_daily(symbol="sh000300")
    dates = pd.to_datetime(index["date"])
    return index.set_index(dates)["open"].sort_index().loc[start:end]


def _csi300_benchmark(bars: pd.DataFrame, start: str, end: str):
    evaluation_dates = bars.loc[start:end].index
    if len(evaluation_dates) < 2:
        return None
    try:
        prices = _csi300_open(start, end)
    except Exception as exc:  # network or provider specific; the main result is still valid
        return {"available": False, "reason": type(exc).__name__}
    prices = pd.to_numeric(prices.reindex(evaluation_dates), errors="coerce")
    if prices.isna().any() or (prices <= 0).any():
        return {"available": False, "reason": "沪深300 开盘价在回测日期上缺失"}
    equity = prices / prices.iloc[0]
    return {
        "available": True,
        "symbol": "000300",
        "first_date": prices.index[0].date().isoformat(),
        "last_date": prices.index[-1].date().isoformat(),
        "intervals": len(prices) - 1,
        "total_return": float(equity.iloc[-1] - 1),
        "max_drawdown": float((1 - equity / equity.cummax()).max()),
        "note": "沪深300 价格指数，按相同交易日开盘价采样，不含费用。",
    }


def _run_v2_backtest(symbol: str, start: str, end: str, rule: Mapping[str, Any]) -> dict:
    first, last, bars = _load_bars(symbol, start, end, rule)
    bars = _prepare_bars(bars)
    signals = build_strategy_signals(bars, rule)
    evaluation_dates = bars.loc[start:end].index
    prior_dates = bars.index[bars.index < pd.Timestamp(start)]
    required = list(prior_dates[-1:]) + list(evaluation_dates)
    if len(prior_dates) == 0 or len(evaluation_dates) < 2:
        raise ValueError("Not enough history or evaluation observations for this window.")
    if signals.loc[required].isna().any():
        raise ValueError("Insufficient warm-up bars to form a signal at the evaluation start.")
    targets = signals.fillna(False).astype(int)

    config = _benchmark_config(start)
    strategy = run_signal_backtest(bars, targets, config)
    hold = run_signal_backtest(bars, pd.Series(1, index=bars.index), config)
    audit = audit_backtest(strategy, config, bars)
    return {
        "symbol": symbol,
        "rule": rule,
        "method": "收盘后形成目标仓位，最早在下一交易日开盘成交；使用 V2 固定实验的佣金、"
                  "最低佣金、滑点、整手和仓位上限假设；期末按收盘估值，不强制平仓。",
        "start": first.isoformat(),
        "end": last.isoformat(),
        "evaluation_first_close": strategy["equity"][0]["Date"],
        "evaluation_last_close": strategy["equity"][-1]["Date"],
        "observations": len(strategy["equity"]),
        "cost_assumptions": {key: config[key] for key in _COST_KEYS},
        "strategy": strategy["metrics"],
        "risk_events": strategy["risk_events"],
        "buy_and_hold": hold["metrics"],
        "csi300": _csi300_benchmark(bars, start, end),
        "audit_ok": audit["ok"],
    }


def backtest_a_share_strategy(
    symbol: Annotated[str, "Six-digit A-share code"],
    start: Annotated[str, "First eligible evaluation date, YYYY-MM-DD"],
    end: Annotated[str, "Last eligible evaluation date, YYYY-MM-DD"],
    rule_json: Annotated[str, "JSON rule using the supported indicators and next_open execution"],
) -> str:
    """Backtest a validated strategy rule against A-share daily bars with V2 costs."""
    try:
        rule = json.loads(rule_json) if isinstance(rule_json, str) else rule_json
    except json.JSONDecodeError as exc:
        raise ValueError("rule_json must be valid JSON.") from exc
    rule = validate_strategy_rule(rule)
    result = _run_v2_backtest(symbol, start, end, rule)
    return json.dumps(result, ensure_ascii=False, allow_nan=False)


def backtest_a_share_momentum(
    symbol: Annotated[str, "Six-digit A-share code"],
    start: Annotated[str, "First eligible evaluation date, YYYY-MM-DD"],
    end: Annotated[str, "Last eligible evaluation date, YYYY-MM-DD"],
) -> str:
    """Compare the fixed 20-bar positive-momentum rule with buy and hold."""
    result = _run_v2_backtest(symbol, start, end, validate_strategy_rule(DEFAULT_MOMENTUM_RULE))
    return json.dumps(result, ensure_ascii=False, allow_nan=False)
