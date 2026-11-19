"""中国大陆 A 股数据适配层（AkShare 数据源）。

AkShare 免费、无需 API key，聚合了东方财富、新浪财经、巨潮资讯、同花顺等公开数据。
本模块的方法签名尽量对齐 ``YFinanceUtils`` / ``FMPUtils`` / ``FinnHubUtils``，
方便把上层 agent 的数据工具从美股数据源切换到 A 股数据源。

股票代码支持以下写法（大小写均可）::

    600519 / "600519.SH" / "SH600519" / "sh.600519"
    000001 / "000001.SZ"
    830799 / "830799.BJ"

依赖::

    pip install akshare

使用方法（与本仓库其他数据类一致，直接以类方法调用）::

    from finrobot.data_source import AkShareUtils

    AkShareUtils.get_stock_data("600519", "2024-01-01", "2024-12-31")
"""

import json
import re
from datetime import date, datetime
from typing import Annotated, List, Optional

import numpy as np
import pandas as pd

from ..utils import save_output, SavePathType


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _require_ak():
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "AkShare is required. Install it with `pip install akshare`."
        ) from exc
    return ak


def _normalize_symbol(symbol: str) -> str:
    """把各种 A 股代码写法统一成 6 位数字。"""
    raw = str(symbol).strip().upper().replace(" ", "")
    raw = re.sub(r"^(SH|SZ|BJ|SS)[.\-_]?", "", raw)
    raw = re.sub(r"[.\-_](SH|SZ|BJ|SS)$", "", raw)
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 6:
        raise ValueError(
            f"无法识别 A 股代码: {symbol!r}，应为 6 位数字，如 600519 / 000001 / 830799"
        )
    return digits


def is_cn_symbol(symbol: str) -> bool:
    """判断代码是否为 A 股 6 位代码（支持 ``600519.SH`` / ``SH600519`` 等写法）。

    用于上层工具在美股代码（如 ``AAPL``）与 A 股代码之间自动选择数据源。
    """
    try:
        _normalize_symbol(symbol)
        return True
    except (TypeError, ValueError):
        return False


def _exchange_symbol(symbol: str) -> str:
    """转换为东方财富财报接口要求的 ``SH600519`` / ``SZ000001`` / ``BJ830799``。"""
    code = _normalize_symbol(symbol)
    if code.startswith(("6", "9")):
        return "SH" + code
    if code.startswith(("0", "2", "3")):
        return "SZ" + code
    if code.startswith(("4", "8")):
        return "BJ" + code
    raise ValueError(f"无法判断交易所: {code}")


def _tx_symbol(symbol: str) -> str:
    """腾讯行情接口使用小写前缀，如 ``sh600519``。"""
    return _exchange_symbol(symbol).lower()


def _date8(value: str) -> str:
    return pd.to_datetime(value).strftime("%Y%m%d")


def _date_dash(value: str) -> str:
    return pd.to_datetime(value).strftime("%Y-%m-%d")


_PRICE_RENAME = {
    "date": "Date", "open": "Open", "close": "Close", "high": "High",
    "low": "Low", "volume": "Volume", "amount": "Amount", "turnover": "Turnover",
    "日期": "Date",
    "开盘": "Open",
    "收盘": "Close",
    "最高": "High",
    "最低": "Low",
    "成交量": "Volume",
    "成交额": "Amount",
    "振幅": "Amplitude",
    "涨跌幅": "PctChange",
    "涨跌额": "Change",
    "换手率": "Turnover",
}


def _standardize_price(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.rename(columns=_PRICE_RENAME)
    if "股票代码" in out.columns:
        out = out.drop(columns=["股票代码"])
    date_col = "Date" if "Date" in out.columns else "date"
    if date_col in out.columns:
        out[date_col] = pd.to_datetime(out[date_col])
        out = out.set_index(date_col).sort_index()
        out.index.name = "Date"
    return out


def _annual_reports(df: pd.DataFrame) -> pd.DataFrame:
    """只保留年报（12-31），按报告期升序。"""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["_date"] = pd.to_datetime(out["REPORT_DATE"], errors="coerce")
    out = out.dropna(subset=["_date"])
    out = out[out["_date"].dt.month == 12]
    return out.sort_values("_date").drop_duplicates("_date")


def _num_col(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(np.nan, index=df.index)


def _select_annual_report(df: pd.DataFrame, report_year: str | None = None) -> pd.Series | None:
    """按报告年份选择正式年报公告，而不是按披露年份。"""
    if df is None or df.empty:
        return None
    title_col = next((name for name in ("公告标题", "title", "TITLE") if name in df.columns), None)
    if title_col is None:
        return None
    candidates = df.copy()
    candidates = candidates[candidates[title_col].astype(str).str.contains("年度报告", na=False)]
    if report_year is not None:
        candidates = candidates[
            candidates[title_col].astype(str).str.contains(
                rf"(?<!\d){re.escape(str(report_year))}年年度报告", regex=True, na=False
            )
        ]
    if candidates.empty:
        return None
    # 同一报告期可能同时返回摘要、英文版、更正或取消公告，优先正式版。
    penalty = candidates[title_col].astype(str).map(
        lambda text: sum(token in text for token in ("摘要", "英文版", "取消", "更正"))
    )
    rows = candidates.assign(_penalty=penalty).sort_values("_penalty", kind="stable")
    return rows.iloc[0].drop(labels=["_penalty"], errors="ignore")


def _json_safe(value):
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if pd.isna(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


_FORECAST_CACHE: dict = {}


def _forecast_table(ak) -> pd.DataFrame:
    """东财盈利预测全表按天缓存一次（该接口不支持按代码过滤）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if _FORECAST_CACHE.get("date") != today:
        _FORECAST_CACHE["date"] = today
        _FORECAST_CACHE["df"] = ak.stock_profit_forecast_em()
    return _FORECAST_CACHE["df"]


# ---------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------

class AkShareUtils:
    """A 股数据适配器（AkShare）。"""

    # ------------------------------ 行情 ------------------------------

    def get_stock_data(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        start_date: Annotated[str, "开始日期，YYYY-mm-dd"],
        end_date: Annotated[str, "结束日期，YYYY-mm-dd"],
        adjust: Annotated[str, "复权方式: qfq 前复权 / hfq 后复权 / 空字符串 不复权"] = "qfq",
        save_path: SavePathType = None,
    ) -> pd.DataFrame:
        """获取 A 股日线行情（东财，失败时回退腾讯）。"""
        ak = _require_ak()
        code = _normalize_symbol(symbol)
        start, end = _date8(start_date), _date8(end_date)

        df = pd.DataFrame()
        source = "akshare_eastmoney"
        endpoint = "stock_zh_a_hist"
        try:
            df = _standardize_price(
                ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start,
                    end_date=end,
                    adjust=adjust,
                )
            )
        except Exception as exc:
            print(f"[AkShareUtils] 东方财富行情获取失败（{exc}），回退到腾讯行情。")

        if df.empty:
            source, endpoint = "akshare_tencent", "stock_zh_a_hist_tx"
            df = _standardize_price(
                ak.stock_zh_a_hist_tx(
                    symbol=_tx_symbol(code),
                    start_date=start,
                    end_date=end,
                    adjust=adjust,
                )
            )
            # 腾讯接口的 volume 单位是股，东财是手，统一折算成手方便对比
            if "volume" in df.columns:
                df = df.rename(columns={"volume": "Volume"})
            if "turnover" in df.columns:
                df = df.rename(columns={"turnover": "Turnover"})
            if "amount" in df.columns:
                df = df.rename(columns={"amount": "Amount"})
            if "Volume" in df.columns:
                # AkShare 1.20.7 mistakenly skips shares conversion for sz000 stocks.
                already_lots = getattr(ak, "__version__", "") == "1.20.7" and code.startswith("000")
                df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
                if not already_lots:
                    df["Volume"] = df["Volume"] / 100
            if "Turnover" in df.columns:
                df["Turnover"] = pd.to_numeric(df["Turnover"], errors="coerce") * 100

        df.attrs.update(source=source, endpoint=endpoint, adjust=adjust,
                        volume_unit="lots (100 shares)", turnover_unit="percent",
                        akshare_version=getattr(ak, "__version__", "unknown"))
        save_output(df, f"Stock data for {code}", save_path)
        return df

    def get_index_data(
        index_symbol: Annotated[str, "指数代码，如 000300（沪深300）、000001（上证指数）"],
        start_date: Annotated[str, "开始日期，YYYY-mm-dd"],
        end_date: Annotated[str, "结束日期，YYYY-mm-dd"],
        save_path: SavePathType = None,
    ) -> pd.DataFrame:
        """指数日线行情（东财），列名与 ``get_stock_data`` 一致。"""
        ak = _require_ak()
        code = _normalize_symbol(index_symbol)
        df = _standardize_price(
            ak.index_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=_date8(start_date),
                end_date=_date8(end_date),
            )
        )
        save_output(df, f"Index data for {code}", save_path)
        return df

    def get_stock_info(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
    ) -> dict:
        """返回个股基础信息：总市值、流通市值、行业、上市时间、总股本等。

        同时补充 ``shortName`` / ``longName`` / ``symbol`` / ``currency`` /
        ``industry`` 等英文字段，方便与 ``YFinanceUtils`` 的返回结构复用。
        """
        ak = _require_ak()
        code = _normalize_symbol(symbol)
        df = ak.stock_individual_info_em(symbol=code)
        info = dict(zip(df["item"], df["value"]))
        info.setdefault("symbol", code)
        info.setdefault("shortName", info.get("股票简称", code))
        info.setdefault("longName", info.get("股票简称", code))
        info.setdefault("industry", info.get("行业", ""))
        info.setdefault("currency", "CNY")
        info.setdefault("marketCap", info.get("总市值"))
        return info

    def get_company_info(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        save_path: Optional[str] = None,
    ) -> pd.DataFrame:
        """返回公司名称、行业、市场、官网等基础资料。"""
        ak = _require_ak()
        code = _normalize_symbol(symbol)

        info_df = ak.stock_individual_info_em(symbol=code)
        info = dict(zip(info_df["item"], info_df["value"]))
        try:
            profile = ak.stock_profile_cninfo(symbol=code).iloc[0].to_dict()
        except Exception:
            profile = {}

        row = {
            "Company Name": profile.get("公司名称") or info.get("股票简称", "N/A"),
            "Industry": profile.get("所属行业") or info.get("行业", "N/A"),
            "Sector": profile.get("所属市场", "A股"),
            "Country": "中国",
            "Website": profile.get("官方网站", "N/A"),
        }
        out = pd.DataFrame([row])
        save_output(out, f"Company info for {code}", save_path)
        return out

    def get_company_profile(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
    ) -> str:
        """生成一段公司简介文本（对齐 FinnHubUtils.get_company_profile 的用途）。"""
        ak = _require_ak()
        code = _normalize_symbol(symbol)

        try:
            profile = ak.stock_profile_cninfo(symbol=code).iloc[0].to_dict()
        except Exception:
            profile = {}
        try:
            info_df = ak.stock_individual_info_em(symbol=code)
            info = dict(zip(info_df["item"], info_df["value"]))
        except Exception:
            info = {}

        name = profile.get("公司名称") or info.get("股票简称") or code
        short_name = info.get("股票简称") or profile.get("A股简称") or code
        industry = profile.get("所属行业") or info.get("行业") or "未知行业"
        business = str(profile.get("主营业务") or "N/A").rstrip("。")
        website = profile.get("官方网站") or "N/A"
        listed_raw = info.get("上市时间") or profile.get("上市日期") or "N/A"
        listed = (
            pd.to_datetime(str(listed_raw)).strftime("%Y-%m-%d")
            if str(listed_raw).isdigit()
            else listed_raw
        )
        market = profile.get("所属市场") or "A股"

        market_cap = info.get("总市值")
        shares = info.get("总股本")
        market_cap_text = (
            f"{float(market_cap) / 1e8:.2f} 亿元" if market_cap not in (None, "N/A") else "N/A"
        )
        shares_text = (
            f"{float(shares) / 1e8:.2f} 亿股" if shares not in (None, "N/A") else "N/A"
        )

        return (
            "[公司简介]:\n\n"
            f"{name}（{short_name}，代码 {code}）是一家在{market}上市的 A 股公司，"
            f"所属行业为{industry}。公司于 {listed} 上市，主营业务为：{business}。\n\n"
            f"截至最新数据，公司总市值约 {market_cap_text}，总股本约 {shares_text}。"
            f"公司官网：{website}。"
        )

    def get_stock_dividends(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        save_path: Optional[str] = None,
    ) -> pd.DataFrame:
        """返回历史分红送配明细。"""
        ak = _require_ak()
        code = _normalize_symbol(symbol)
        df = ak.stock_fhps_detail_em(symbol=code)
        save_output(df, f"Dividends for {code}", save_path)
        return df

    # ------------------------------ 财务报表 ------------------------------

    def get_income_stmt(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        save_path: Optional[str] = None,
    ) -> pd.DataFrame:
        """利润表（东方财富，全部报告期，列名为 EM 财务字段）。"""
        ak = _require_ak()
        df = ak.stock_profit_sheet_by_report_em(symbol=_exchange_symbol(symbol))
        save_output(df, f"Income statement for {_normalize_symbol(symbol)}", save_path)
        return df

    def get_balance_sheet(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        save_path: Optional[str] = None,
    ) -> pd.DataFrame:
        """资产负债表（东方财富，全部报告期）。"""
        ak = _require_ak()
        df = ak.stock_balance_sheet_by_report_em(symbol=_exchange_symbol(symbol))
        save_output(df, f"Balance sheet for {_normalize_symbol(symbol)}", save_path)
        return df

    def get_cash_flow(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        save_path: Optional[str] = None,
    ) -> pd.DataFrame:
        """现金流量表（东方财富，全部报告期）。"""
        ak = _require_ak()
        df = ak.stock_cash_flow_sheet_by_report_em(symbol=_exchange_symbol(symbol))
        save_output(df, f"Cash flow for {_normalize_symbol(symbol)}", save_path)
        return df

    # ------------------------------ 财务指标 ------------------------------

    def get_financial_metrics(
        ticker_symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        years: Annotated[int, "回溯年数，默认 4"] = 4,
    ) -> pd.DataFrame:
        """按年度汇总核心财务指标（营收、利润、ROE、ROA、负债率、现金流等）。

        返回 DataFrame：行=指标，列=年份，与 ``FMPUtils.get_financial_metrics`` 类似。
        """
        ak = _require_ak()
        ex_symbol = _exchange_symbol(ticker_symbol)

        inc = _annual_reports(ak.stock_profit_sheet_by_report_em(symbol=ex_symbol))
        bal = _annual_reports(ak.stock_balance_sheet_by_report_em(symbol=ex_symbol))
        cf = _annual_reports(ak.stock_cash_flow_sheet_by_report_em(symbol=ex_symbol))

        if inc.empty:
            raise ValueError(f"未获取到 {ticker_symbol} 的年报数据")

        merged = inc[
            [
                "_date",
                "TOTAL_OPERATE_INCOME",
                "OPERATE_COST",
                "PARENT_NETPROFIT",
                "NETPROFIT",
                "BASIC_EPS",
            ]
        ].merge(
            bal[["_date", "TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY"]],
            on="_date",
            how="left",
        ).merge(
            cf[["_date", "NETCASH_OPERATE", "CONSTRUCT_LONG_ASSET"]],
            on="_date",
            how="left",
        )
        merged = merged.sort_values("_date").reset_index(drop=True)

        revenue = _num_col(merged, "TOTAL_OPERATE_INCOME")
        cost = _num_col(merged, "OPERATE_COST")
        net_profit = _num_col(merged, "PARENT_NETPROFIT").fillna(
            _num_col(merged, "NETPROFIT")
        )
        assets = _num_col(merged, "TOTAL_ASSETS")
        liabilities = _num_col(merged, "TOTAL_LIABILITIES")
        equity = _num_col(merged, "TOTAL_EQUITY")
        ocf = _num_col(merged, "NETCASH_OPERATE")
        capex = _num_col(merged, "CONSTRUCT_LONG_ASSET")

        avg_assets = (assets + assets.shift(1)) / 2
        avg_equity = (equity + equity.shift(1)) / 2
        fcf = ocf - capex

        metrics = {
            "Revenue (亿元)": (revenue / 1e8).round(2),
            "Revenue Growth (%)": (revenue.pct_change() * 100).round(1),
            "Gross Profit (亿元)": ((revenue - cost) / 1e8).round(2),
            "Gross Margin (%)": ((revenue - cost) / revenue * 100).round(1),
            "Net Profit (亿元)": (net_profit / 1e8).round(2),
            "Net Margin (%)": (net_profit / revenue * 100).round(1),
            "ROE (%)": (net_profit / avg_equity * 100).round(1),
            "ROA (%)": (net_profit / avg_assets * 100).round(1),
            "Debt Ratio (%)": (liabilities / assets * 100).round(1),
            "Operating Cash Flow (亿元)": (ocf / 1e8).round(2),
            "FCF (亿元)": (fcf / 1e8).round(2),
            "FCF Conversion": (fcf / net_profit).round(2),
            "EPS (元)": _num_col(merged, "BASIC_EPS").round(2),
        }

        out = pd.DataFrame(metrics).tail(years).T
        out.columns = merged["_date"].dt.strftime("%Y").tail(years).tolist()
        out = out.replace([np.inf, -np.inf], np.nan)
        out.columns.name = "Year"
        return out

    def get_competitor_financial_metrics(
        ticker_symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        competitors: Annotated[List[str], "可比公司代码列表，如 ['000858', '000568']"],
        years: Annotated[int, "回溯年数，默认 4"] = 4,
    ) -> dict:
        """返回公司及可比公司的核心财务指标字典。"""
        symbols = [_normalize_symbol(ticker_symbol)] + [
            _normalize_symbol(c) for c in competitors
        ]
        result = {}
        for sym in symbols:
            try:
                result[sym] = AkShareUtils.get_financial_metrics(sym, years)
            except Exception as exc:
                print(f"[AkShareUtils] {sym} 财务指标获取失败: {exc}")
                result[sym] = None
        return result

    def get_basic_financials(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        selected_columns: Annotated[
            Optional[List[str]], "指定返回的指标字段；None 表示全部"
        ] = None,
    ) -> str:
        """最新一期财务指标（JSON 字符串，字段为中文指标名）。"""
        ak = _require_ak()
        code = _normalize_symbol(symbol)
        start_year = str(max(datetime.now().year - 3, 1990))
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=start_year)
        if df is None or df.empty:
            return f"未获取到 {code} 的财务指标"

        df = df.sort_values("日期")
        latest = {
            k: _json_safe(v) for k, v in df.iloc[-1].to_dict().items()
        }
        if selected_columns:
            latest = {k: v for k, v in latest.items() if k in selected_columns}
        return json.dumps(latest, ensure_ascii=False, indent=2, default=_json_safe)

    def get_basic_financials_history(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        freq: Annotated[str, "报告频率: annual 年报 / quarterly 全部报告期"],
        start_date: Annotated[str, "开始日期，YYYY-mm-dd"],
        end_date: Annotated[str, "结束日期，YYYY-mm-dd"],
        selected_columns: Annotated[
            Optional[List[str]], "指定返回的指标字段；None 表示全部"
        ] = None,
        save_path: SavePathType = None,
    ) -> pd.DataFrame:
        """历史财务指标序列，索引为报告期。"""
        if freq not in ("annual", "quarterly"):
            return f"Invalid reporting frequency {freq}. Please specify either 'annual' or 'quarterly'."

        ak = _require_ak()
        code = _normalize_symbol(symbol)
        start_year = str(max(pd.to_datetime(start_date).year - 1, 1990))
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=start_year)
        if df is None or df.empty:
            return pd.DataFrame()

        df = df.copy()
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        df = df.dropna(subset=["日期"])

        if freq == "annual":
            df = df[df["日期"].dt.month == 12]

        df = df[
            (df["日期"] >= pd.to_datetime(start_date))
            & (df["日期"] <= pd.to_datetime(end_date))
        ]
        if selected_columns:
            keep = ["日期"] + [c for c in selected_columns if c in df.columns]
            df = df[keep]

        out = df.sort_values("日期").set_index("日期")
        out.index.name = "date"
        save_output(out, "basic financials", save_path)
        return out

    # ------------------------------ 估值/预测 ------------------------------

    def get_historical_market_cap(
        ticker_symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        date: Annotated[Optional[str], "指定日期 YYYY-mm-dd；为空则返回历史序列"] = None,
        start_date: Annotated[Optional[str], "历史序列开始日期"] = None,
        end_date: Annotated[Optional[str], "历史序列结束日期"] = None,
    ) -> Optional[float | pd.DataFrame]:
        """历史总市值（单位：亿元）。指定 date 时返回该日最接近的市值。"""
        ak = _require_ak()
        code = _normalize_symbol(ticker_symbol)
        df = ak.stock_zh_valuation_baidu(
            symbol=code, indicator="总市值", period="近十年"
        )
        if df is None or df.empty:
            return None

        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        if date is not None:
            target = pd.to_datetime(date)
            idx = (df["date"] - target).abs().idxmin()
            return float(df.loc[idx, "value"])

        if start_date is not None:
            df = df[df["date"] >= pd.to_datetime(start_date)]
        if end_date is not None:
            df = df[df["date"] <= pd.to_datetime(end_date)]
        return df

    def get_historical_bvps(
        ticker_symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        target_date: Annotated[
            Optional[str], "目标日期 YYYY-mm-dd；为空则返回最新一期"
        ] = None,
    ) -> Optional[float]:
        """历史每股净资产（元/股）。"""
        ak = _require_ak()
        code = _normalize_symbol(ticker_symbol)
        start_year = (
            str(max(pd.to_datetime(target_date).year - 2, 1990))
            if target_date
            else "2000"
        )
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=start_year)
        if df is None or df.empty:
            return None

        df = df.copy()
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        df = df.dropna(subset=["日期"]).sort_values("日期")

        col = None
        for candidate in ("每股净资产_调整前(元)", "每股净资产_调整后(元)", "每股净资产"):
            if candidate in df.columns:
                col = candidate
                break
        if col is None:
            return None

        if target_date is not None:
            target = pd.to_datetime(target_date)
            past = df[df["日期"] <= target]
            row = past.iloc[-1] if not past.empty else df.iloc[0]
        else:
            row = df.iloc[-1]

        value = pd.to_numeric(pd.Series([row[col]]), errors="coerce").iloc[0]
        return None if pd.isna(value) else float(value)

    def get_analyst_recommendations(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
    ) -> tuple:
        """返回近六个月机构评级中最主流的评级及机构数。"""
        ak = _require_ak()
        code = _normalize_symbol(symbol)
        df = _forecast_table(ak)
        if df is None or df.empty:
            return None, 0

        sub = df[df["代码"].astype(str).str.zfill(6) == code]
        if sub.empty:
            return None, 0

        rating_cols = [c for c in df.columns if "机构投资评级" in str(c)]
        if not rating_cols:
            return None, 0

        votes = pd.to_numeric(sub.iloc[0][rating_cols], errors="coerce")
        if votes.isna().all() or votes.max() <= 0:
            return None, 0

        best_col = votes.idxmax()
        rating = str(best_col).split("-")[-1]
        return rating, int(votes.max())

    def get_analyst_forecast(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        indicator: Annotated[str, "预测指标，默认预测年报每股收益"] = "预测年报每股收益",
    ) -> pd.DataFrame:
        """同花顺机构一致预期（预测机构数、最小值/均值/最大值）。"""
        ak = _require_ak()
        return ak.stock_profit_forecast_ths(
            symbol=_normalize_symbol(symbol), indicator=indicator
        )

    def get_research_reports(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        max_reports: Annotated[int, "最多返回的研报数量，默认 20"] = 20,
        save_path: SavePathType = None,
    ) -> pd.DataFrame:
        """东方财富个股研报列表（评级、机构、盈利预测、PDF 链接）。"""
        ak = _require_ak()
        code = _normalize_symbol(symbol)
        df = ak.stock_research_report_em(symbol=code)
        out = df.head(max_reports) if df is not None else pd.DataFrame()
        save_output(out, f"Research reports for {code}", save_path)
        return out

    def get_target_price(
        ticker_symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        date: Annotated[Optional[str], "日期参数（保留以对齐 FMP 接口，可忽略）"] = None,
    ) -> str:
        """基于研报盈利预测（EPS × 预测 PE）推算的目标价区间。

        注意：它不是交易所或数据商发布的官方一致目标价，仅作估值参考。
        """
        ak = _require_ak()
        code = _normalize_symbol(ticker_symbol)
        df = ak.stock_research_report_em(symbol=code)
        if df is None or df.empty:
            return "N/A"

        prices = []
        for col in df.columns:
            match = re.match(r"^(\d{4})-盈利预测-收益$", str(col))
            if not match:
                continue
            pe_col = f"{match.group(1)}-盈利预测-市盈率"
            if pe_col not in df.columns:
                continue
            eps = pd.to_numeric(df[col], errors="coerce")
            pe = pd.to_numeric(df[pe_col], errors="coerce")
            prices.extend((eps * pe).dropna().tolist())

        if not prices:
            return "N/A"

        arr = np.array(prices, dtype=float)
        return f"{arr.min():.2f} - {arr.max():.2f} (md. {np.median(arr):.2f})"

    # ------------------------------ 公告/新闻 ------------------------------

    def get_announcements(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        start_date: Annotated[Optional[str], "开始日期 YYYY-mm-dd，默认一年前"] = None,
        end_date: Annotated[Optional[str], "结束日期 YYYY-mm-dd，默认今天"] = None,
        category: Annotated[str, "公告分类，如 年报/半年报/业绩预告；空字符串表示全部"] = "",
        save_path: SavePathType = None,
    ) -> pd.DataFrame:
        """巨潮资讯（cninfo）公告列表。"""
        ak = _require_ak()
        code = _normalize_symbol(symbol)
        end = end_date or datetime.now().strftime("%Y-%m-%d")
        start = start_date or (
            pd.to_datetime(end) - pd.Timedelta(days=365)
        ).strftime("%Y-%m-%d")

        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code,
            market="沪深京",
            keyword="",
            category=category,
            start_date=_date8(start),
            end_date=_date8(end),
        )
        save_output(df, f"Announcements for {code}", save_path)
        return df

    def get_sec_report(
        ticker_symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        fyear: Annotated[
            str, "年报年份，如 '2024' 或 'latest'。默认 'latest'"
        ] = "latest",
    ) -> str:
        """返回年报公告标题、链接和披露日期（对应 FMPUtils.get_sec_report）。

        ``fyear`` 指报告年份，例如 ``"2024"`` 表示 2024 年度报告。年报通常在
        次年披露，因此检索窗口会覆盖到报告年后两年，并按标题中的报告年份匹配。
        """
        code = _normalize_symbol(ticker_symbol)
        if fyear == "latest":
            report_year = None
            start_date = f"{max(datetime.now().year - 5, 1990)}-01-01"
            end_date = datetime.now().strftime("%Y-%m-%d")
        else:
            try:
                report_year = str(int(fyear))
            except (TypeError, ValueError):
                raise ValueError("fyear must be 'latest' or a four-digit report year")
            start_date = f"{report_year}-01-01"
            end_date = f"{int(report_year) + 2}-12-31"

        df = AkShareUtils.get_announcements(
            code, start_date=start_date, end_date=end_date, category="年报"
        )
        if df is None or df.empty:
            return f"未找到 {code} {fyear} 的年报公告"

        if report_year is None:
            # latest：取披露日期最新的正式年报，优先排除摘要、英文版、更正、取消。
            title_col = next(
                (name for name in ("公告标题", "title", "TITLE") if name in df.columns), None
            )
            date_col = next(
                (name for name in ("公告时间", "披露时间", "date") if name in df.columns), None
            )
            row = None
            if title_col:
                formal = df[df[title_col].astype(str).str.contains("年度报告", na=False)].copy()
                if date_col and not formal.empty:
                    formal["_date"] = pd.to_datetime(formal[date_col], errors="coerce")
                    formal = formal.dropna(subset=["_date"])
                if not formal.empty:
                    clean = formal[
                        ~formal[title_col].astype(str).str.contains(
                            "摘要|英文版|取消|更正", regex=True, na=False
                        )
                    ]
                    latest = (clean if not clean.empty else formal).sort_values("_date")
                    row = latest.iloc[-1].drop(labels=["_date"], errors="ignore")
        else:
            row = _select_annual_report(df, report_year)
        if row is None:
            return f"未找到 {code} {fyear} 的年报公告"

        return (
            f"Report Title: {row.get('公告标题', row.get('title', ''))}\n"
            f"Link: {row.get('公告链接', row.get('url', ''))}\n"
            f"Filing Date: {row.get('公告时间', '')}"
        )

    def get_company_news(
        symbol: Annotated[str, "6 位 A 股代码，如 600519"],
        start_date: Annotated[str, "开始日期 YYYY-mm-dd"],
        end_date: Annotated[str, "结束日期 YYYY-mm-dd"],
        max_news_num: Annotated[int, "最多返回条数，默认 10"] = 10,
        save_path: SavePathType = None,
    ) -> pd.DataFrame:
        """个股新闻（东方财富），返回 date/headline/summary/source/url。"""
        ak = _require_ak()
        code = _normalize_symbol(symbol)
        raw = ak.stock_news_em(symbol=code)
        if raw is None or raw.empty:
            return pd.DataFrame(columns=["date", "headline", "summary", "source", "url"])

        df = raw.rename(
            columns={
                "新闻标题": "headline",
                "新闻内容": "summary",
                "发布时间": "date",
                "文章来源": "source",
                "新闻链接": "url",
            }
        )
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = df[
            (df["date"] >= pd.to_datetime(start_date))
            & (df["date"] <= pd.to_datetime(end_date) + pd.Timedelta(days=1))
        ]

        if df.empty:
            return df[["date", "headline", "summary", "source", "url"]]

        df = df[["date", "headline", "summary", "source", "url"]].sort_values("date")
        if len(df) > max_news_num:
            df = df.sample(n=max_news_num).sort_values("date")

        save_output(df, f"company news of {code}", save_path)
        return df

    def get_global_news(
        source: Annotated[str, "新闻源: cls 财联社 / em 东方财富 / sina 新浪 / ths 同花顺"] = "cls",
        save_path: SavePathType = None,
    ) -> pd.DataFrame:
        """全市场财经快讯。"""
        ak = _require_ak()
        sources = {
            "cls": ak.stock_info_global_cls,
            "em": ak.stock_info_global_em,
            "sina": ak.stock_info_global_sina,
            "ths": ak.stock_info_global_ths,
        }
        if source not in sources:
            raise ValueError(f"不支持的新闻源: {source}，可选 {list(sources)}")
        df = sources[source]()
        save_output(df, f"global news ({source})", save_path)
        return df

    def get_hot_rank(
        save_path: SavePathType = None,
    ) -> pd.DataFrame:
        """东方财富个股人气榜（可视为 A 股散户关注度/情绪代理）。"""
        ak = _require_ak()
        df = ak.stock_hot_rank_em()
        save_output(df, "eastmoney hot rank", save_path)
        return df


if __name__ == "__main__":
    print(AkShareUtils.get_stock_data("600519", "2024-01-01", "2024-12-31").tail())
    print(AkShareUtils.get_company_info("600519"))
    print(AkShareUtils.get_financial_metrics("600519", years=4))
    print(AkShareUtils.get_analyst_recommendations("600519"))
    print(AkShareUtils.get_sec_report("600519", "latest"))
