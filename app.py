#!/usr/bin/env python3
"""
五大指標策略儀表板 - Flask 後端 v3
"""
from flask import Flask, render_template, jsonify, request
import yfinance as yf
import pandas as pd
import json, os, time
from datetime import datetime, timedelta
import traceback

app = Flask(__name__)

# ===== In-Memory Cache（避免 yfinance rate limit）=====
_cache = {}
CACHE_TTL = 300  # 5 分鐘

def cache_get(key):
    """從 cache 取得數據，過期返回 None"""
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    return None

def cache_set(key, data):
    """寫入 cache"""
    _cache[key] = {"data": data, "ts": time.time()}

# ===== 數據安全範圍（超出此範圍 = 數據有誤）=====
SANE_RANGES = {
    "0050": (10, 300),      # 0050 合理價格
    "VIX":  (5, 100),       # VIX 歷史極端是 80+
    "DXY":  (70, 130),      # 美元指數
    "Oil":  (10, 200),      # WTI 原油
    "Gold": (800, 15000),   # 黃金（2026 年已破 5000）
    "Yield":(0.5, 8),       # 10年殖利率
    "SMH":  (50, 500),      # 半導體 ETF
}

def validate_data(df):
    """驗證數據合理性，返回 (通過, 警告列表)"""
    warnings = []
    if len(df) == 0:
        return False, ["無數據"]
    latest = df.iloc[-1]
    for col, (lo, hi) in SANE_RANGES.items():
        if col not in df.columns:
            warnings.append(f"缺少 {col} 數據")
            continue
        val = float(latest[col])
        if val < lo or val > hi:
            warnings.append(f"⚠️ {col}={val:.2f} 超出合理範圍({lo}~{hi})，可能數據異常")
    # 檢查是否有重複值（可能是 ffill 導致的假數據）— 週末除外
    now = datetime.utcnow() + timedelta(hours=8)
    is_weekend = now.weekday() >= 5  # 5=六, 6=日
    if len(df) >= 3 and not is_weekend:
        last3 = df.tail(3)
        for col in ["0050", "VIX"]:
            if col in df.columns:
                vals = last3[col].tolist()
                if vals[0] == vals[1] == vals[2]:
                    warnings.append(f"⚠️ {col} 連續3天相同({vals[0]:.2f})，可能停止更新")
    passed = len([w for w in warnings if "⚠️" in w]) == 0
    return passed, warnings

def get_timing_info():
    """計算當前時間與最佳看盤時間的關係（台灣時間 UTC+8）"""
    now = datetime.utcnow() + timedelta(hours=8)
    h = now.hour
    if 5 <= h < 9:
        timing = {"status": "optimal", "label": "✅ 最佳看盤時間", "note": "美股已收盤，台股未開盤，信號最準確"}
    elif 9 <= h <= 13:
        timing = {"status": "caution", "label": "⚠️ 台股交易中", "note": "盤中指標數據為昨日，非即時。建議收盤後再看"}
    elif 13 < h < 21:
        timing = {"status": "waiting", "label": "🕐 等待美股開盤", "note": "台股已收盤，美股尚未開盤。今日台股數據已更新，美股數據為昨日"}
    elif h >= 21 or h < 4:
        timing = {"status": "us_open", "label": "🇺🇸 美股交易中", "note": "美股盤中數據波動大，信號可能不穩定。建議收盤後(05:00)再看"}
    else:
        timing = {"status": "updating", "label": "🔄 數據更新中", "note": "美股剛收盤，數據正在更新，稍等再看"}
    timing["current_time"] = now.strftime("%H:%M")
    return timing

# ===== 備援數據源 =====

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "config.json")

def get_config():
    cfg = {}
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
    except:
        pass
    # Vercel 環境變數 fallback
    for file_key, env_key in [
        ("fred_api_key", "FRED_API_KEY"),
        ("alpha_vantage_key", "ALPHA_VANTAGE_KEY"),
        ("polygon_key", "POLYGON_KEY"),
    ]:
        if not cfg.get(file_key):
            env_val = os.environ.get(env_key, "")
            if env_val:
                cfg[file_key] = env_val
    return cfg

def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def fetch_twse_0050():
    """從台灣證交所取得 0050 最新收盤價"""
    try:
        import urllib.request
        today = datetime.now().strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={today}&stockNo=0050"
        req = urllib.request.Request(url, headers={"User-Agent": "SignalDashboard/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("stat") == "OK" and data.get("data"):
            last_row = data["data"][-1]
            close = float(last_row[6].replace(",", ""))
            date_parts = last_row[0].split("/")
            date_str = f"{int(date_parts[0])+1911}-{date_parts[1]}-{date_parts[2]}"
            return {"value": close, "date": date_str, "source": "TWSE"}
    except:
        pass
    return None

# ===== 備援數據源 =====

def fetch_fred(series_id):
    """從 FRED API 取得最新數據（聯準會官方）"""
    cfg = get_config()
    api_key = cfg.get("fred_api_key", "")
    if not api_key:
        return None
    try:
        import urllib.request
        url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&sort_order=desc&limit=5&file_type=json&api_key={api_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "SignalDashboard/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for obs in data.get("observations", []):
            val = obs.get("value", ".")
            if val != ".":
                return {"value": float(val), "date": obs["date"], "source": "FRED"}
    except:
        pass
    return None

FRED_SERIES = {
    "VIX": "VIXCLS",
    "Oil": "DCOILWTICO",
    "Gold": "GOLDAMGBD228NLBM",
    "Yield": "DGS10",
}

# ===== 專業備援數據源 =====

# Alpha Vantage（專業級市場數據 API #1）
ALPHA_VANTAGE_SYMBOLS = {
    "VIX": "VIX",
    "DXY": "DX-Y.NYB",
    "Oil": "CL=F",
    "Gold": "GC=F",
    "Yield": "TNX",
}

def fetch_alpha_vantage(name):
    """從 Alpha Vantage 取得最新報價"""
    cfg = get_config()
    api_key = cfg.get("alpha_vantage_key", "")
    if not api_key:
        return None
    symbol = ALPHA_VANTAGE_SYMBOLS.get(name)
    if not symbol:
        return None
    try:
        import urllib.request
        url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={api_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "SignalDashboard/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        quote = data.get("Global Quote", {})
        if quote:
            price = float(quote.get("05. price", 0))
            date = quote.get("07. latest trading day", "")
            if price > 0:
                return {"value": price, "date": date, "source": "AlphaVantage"}
    except:
        pass
    return None

# Polygon.io（專業級市場數據 API #2 — 全覆蓋）
POLYGON_SYMBOLS = {
    "VIX": "I:VIX",
    "DXY": "I:DXY",
    "Oil": "C:CLUSD",
    "Gold": "C:GCUSD",
    "Yield": "I:US10Y",
}

def fetch_polygon(name):
    """從 Polygon.io 取得最新報價"""
    cfg = get_config()
    api_key = cfg.get("polygon_key", "")
    if not api_key:
        return None
    symbol = POLYGON_SYMBOLS.get(name)
    if not symbol:
        return None
    try:
        import urllib.request
        url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?adjusted=true&apiKey={api_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "SignalDashboard/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        if results:
            close = results[0].get("c", 0)
            if close > 0:
                return {"value": close, "date": data.get("resultsCount", ""), "source": "Polygon"}
    except:
        pass
    return None

def cross_validate(primary_data, warnings_list):
    """用備援數據源交叉驗證 yfinance 數據（三重專業驗證）"""
    cross_results = []

    if len(primary_data) == 0:
        return cross_results

    latest = primary_data.iloc[-1]

    # === TWSE 驗證 0050（台灣證交所官方）===
    twse = fetch_twse_0050()
    if twse and "0050" in primary_data.columns:
        yf_val = float(latest["0050"])
        tw_val = twse["value"]
        diff_pct = abs(yf_val - tw_val) / tw_val * 100
        result = {"name": "0050", "yfinance": yf_val, "backup": tw_val, "source": "TWSE", "diff_pct": round(diff_pct, 2)}
        cross_results.append(result)
        if diff_pct > 2:
            warnings_list.append(f"⚠️ 0050 數據不一致：yfinance={yf_val:.2f} vs TWSE={tw_val:.2f}（差{diff_pct:.1f}%）")

    # === FRED 驗證（聯準會官方數據，延遲 1-2 天）===
    for name, series in FRED_SERIES.items():
        fred = fetch_fred(series)
        if fred and name in primary_data.columns:
            yf_val = float(latest[name])
            fred_val = fred["value"]
            if fred_val > 0:
                diff_pct = abs(yf_val - fred_val) / fred_val * 100
                result = {"name": name, "yfinance": round(yf_val, 2), "backup": fred_val, "source": "FRED", "diff_pct": round(diff_pct, 2)}
                cross_results.append(result)
                if diff_pct > 10:  # FRED 延遲 + 現貨 vs 期貨，放寬到 10%
                    warnings_list.append(f"⚠️ {name} 數據差異大：yfinance={yf_val:.2f} vs FRED={fred_val:.2f}（差{diff_pct:.1f}%）")

    # === Alpha Vantage 驗證（專業市場數據 #1）===
    for name in ALPHA_VANTAGE_SYMBOLS:
        av = fetch_alpha_vantage(name)
        if av and name in primary_data.columns:
            yf_val = float(latest[name])
            av_val = av["value"]
            if av_val > 0:
                diff_pct = abs(yf_val - av_val) / av_val * 100
                result = {"name": name, "yfinance": round(yf_val, 2), "backup": av_val, "source": "AlphaVantage", "diff_pct": round(diff_pct, 2)}
                cross_results.append(result)
                if diff_pct > 5:
                    warnings_list.append(f"⚠️ {name} 數據差異大：yfinance={yf_val:.2f} vs AlphaVantage={av_val:.2f}（差{diff_pct:.1f}%）")

    # === Polygon.io 驗證（專業市場數據 #2 — 全覆蓋）===
    for name in POLYGON_SYMBOLS:
        pg = fetch_polygon(name)
        if pg and name in primary_data.columns:
            yf_val = float(latest[name])
            pg_val = pg["value"]
            if pg_val > 0:
                diff_pct = abs(yf_val - pg_val) / pg_val * 100
                result = {"name": name, "yfinance": round(yf_val, 2), "backup": pg_val, "source": "Polygon", "diff_pct": round(diff_pct, 2)}
                cross_results.append(result)
                if diff_pct > 5:
                    warnings_list.append(f"⚠️ {name} 數據差異大：yfinance={yf_val:.2f} vs Polygon={pg_val:.2f}（差{diff_pct:.1f}%）")

    return cross_results

# ===== 指標計算引擎 =====

def _extract_close(d):
    """安全地從 yfinance DataFrame 取出 Close 欄位為 1D Series"""
    if d is None or len(d) == 0:
        return None
    # 扁平化 MultiIndex 欄位
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = ['_'.join(str(c) for c in col).strip('_') for col in d.columns]
    # 找 Close 欄位（可能叫 Close, Close_XXXX, 等等）
    close_cols = [c for c in d.columns if 'Close' in str(c) or 'close' in str(c)]
    if close_cols:
        series = d[close_cols[0]].copy()
    else:
        # fallback: 取第一欄
        series = d.iloc[:, 0].copy()
    # 確保是 1D
    if hasattr(series, 'columns'):
        series = series.iloc[:, 0]
    return series

def _safe_close(df, ticker):
    """從 yfinance DataFrame 安全取出 Close 價格序列"""
    if df is None or len(df) == 0:
        return None
    try:
        # 方法1: MultiIndex → 直接用 tuple key
        if isinstance(df.columns, pd.MultiIndex):
            if ('Close', ticker) in df.columns:
                return df[('Close', ticker)].copy()
            # fallback: 找任何 Close 欄
            close_cols = [c for c in df.columns if c[0] == 'Close']
            if close_cols:
                return df[close_cols[0]].copy()
        # 方法2: 單一欄位
        if 'Close' in df.columns:
            s = df['Close']
            if hasattr(s, 'columns'):  # 仍是 DataFrame
                return s.iloc[:, 0].copy()
            return s.copy()
        # 方法3: 扁平化名稱
        flat = {str(c): c for c in df.columns}
        for k, v in flat.items():
            if 'close' in k.lower():
                s = df[v]
                if hasattr(s, 'columns'):
                    return s.iloc[:, 0].copy()
                return s.copy()
    except:
        pass
    return None

# 主要 ticker + 備用 ticker（雲端伺服器有時抓不到期貨/外匯）
TICKER_MAP = {
    "0050":  [("0050.TW", None)],
    "VIX":   [("^VIX", None)],
    "DXY":   [("DX-Y.NYB", None), ("UUP", lambda s: s / s.iloc[-1] * 103)],
    "Oil":   [("CL=F", None), ("USO", lambda s: s / s.iloc[-1] * 70)],
    "Gold":  [("GC=F", None), ("GLD", lambda s: s / s.iloc[-1] * 3000)],
    "Yield": [("^TNX", None)],
    "SMH":   [("SMH", None)],
}

# 更嚴格的範圍檢查（用於防止欄位混淆）
STRICT_RANGES = {
    "0050":  (20, 200),
    "VIX":   (5, 60),       # VIX 很少超過 60
    "DXY":   (85, 120),     # 近年 DXY 在 90-115
    "Oil":   (30, 150),     # 近年油價 40-130
    "Gold":  (1500, 8000),  # 近年金價 1800-5000+
    "Yield": (1.0, 6.0),    # 近年殖利率 1-5%
    "SMH":   (100, 400),    # 近年 SMH 在 150-350
}

def _isolated_download(ticker, period=None, start=None, end=None):
    """完全隔離的下載：用獨立 Ticker 物件避免 session cache 污染"""
    try:
        t = yf.Ticker(ticker)
        if period:
            d = t.history(period=period)
        else:
            d = t.history(start=start, end=end)
        if d is None or len(d) == 0:
            return None
        if 'Close' in d.columns:
            return d['Close'].copy()
        return None
    except:
        # fallback 用 download
        try:
            d = yf.download(ticker, period=period, start=start, end=end,
                           progress=False, auto_adjust=True)
            return _safe_close(d, ticker)
        except:
            return None

def _fetch_with_fallback(name, period=None, start=None, end=None, prev_values=None):
    """嘗試主要 ticker，失敗則用備用。prev_values 防止欄位混淆"""
    candidates = TICKER_MAP.get(name, [])
    strict = STRICT_RANGES.get(name, SANE_RANGES.get(name))

    for ticker, transform in candidates:
        try:
            series = _isolated_download(ticker, period=period, start=start, end=end)
            if series is not None and len(series) > 0:
                if transform:
                    series = transform(series)
                last_val = float(series.iloc[-1])

                # 嚴格範圍檢查
                if strict:
                    lo, hi = strict
                    if last_val < lo or last_val > hi:
                        print(f"[WARN] {name}={last_val:.2f} from {ticker} out of strict range({lo}~{hi})")
                        continue

                # 防止 cache 污染：值不能跟之前下載的一樣
                if prev_values:
                    for prev_name, prev_val in prev_values.items():
                        if abs(last_val - prev_val) < 0.01 and name != prev_name:
                            print(f"[WARN] {name}={last_val:.2f} same as {prev_name}, cache contamination!")
                            continue

                print(f"[OK] {name} fetched from {ticker}: {last_val:.2f}")
                return series
        except Exception as e:
            print(f"[ERR] {name} from {ticker}: {e}")
            continue
    print(f"[FAIL] {name}: all sources failed")
    return None

def fetch_data(period="3mo"):
    """下載市場數據（隔離下載 + 備用源 + 防污染），含 cache"""
    cache_key = f"fetch_data_{period}"
    cached = cache_get(cache_key)
    if cached is not None:
        passed, _ = validate_data(cached)
        if passed:
            return cached
        else:
            _cache.pop(cache_key, None)

    data = {}
    prev_values = {}
    for name in TICKER_MAP:
        series = _fetch_with_fallback(name, period=period, prev_values=prev_values)
        if series is not None:
            data[name] = series
            prev_values[name] = float(series.iloc[-1])
    result = pd.DataFrame(data).ffill().dropna()
    cache_set(cache_key, result)
    return result

def fetch_data_range(start, end):
    """按日期範圍下載（含備用源 + 防污染）"""
    data = {}
    prev_values = {}
    for name in TICKER_MAP:
        series = _fetch_with_fallback(name, start=start, end=end, prev_values=prev_values)
        if series is not None:
            data[name] = series
            prev_values[name] = float(series.iloc[-1])
    return pd.DataFrame(data).ffill().dropna()

def add_indicators(df):
    """計算所有技術指標"""
    for col in ["DXY", "Oil", "Gold", "Yield"]:
        if col not in df.columns: continue
        df[f"{col}_ma5"] = df[col].rolling(5).mean()
        df[f"{col}_trend"] = "平"
        df.loc[df[col] > df[f"{col}_ma5"] * 1.005, f"{col}_trend"] = "漲"
        df.loc[df[col] < df[f"{col}_ma5"] * 0.995, f"{col}_trend"] = "跌"

    df["VIX_peak10"] = df["VIX"].rolling(10).max()
    df["VIX_panic_bounce"] = (df["VIX_peak10"] > 30) & (df["VIX"] < df["VIX_peak10"] * 0.85)

    delta = df["0050"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta).clip(lower=0).rolling(14).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-10))
    df["MA20"] = df["0050"].rolling(20).mean()
    df["MA60"] = df["0050"].rolling(60).mean()
    df["MA120"] = df["0050"].rolling(120).mean()
    df["above_MA20"] = df["0050"] > df["MA20"]
    df["above_MA60"] = df["0050"] > df["MA60"]
    df["momentum_20"] = df["0050"].pct_change(20) * 100
    df["drawdown"] = (df["0050"] / df["0050"].cummax() - 1) * 100
    if "SMH" in df.columns:
        df["SMH_MA50"] = df["SMH"].rolling(50).mean()
    return df

def calc_signal(row):
    """計算單日信號"""
    score = 0
    details = []

    vix = row.get("VIX", 20)
    if vix < 15:
        score += 1.5; details.append({"name": "VIX", "value": f"{vix:.1f}", "status": "bullish", "note": "<15 極低"})
    elif vix < 20:
        score += 1; details.append({"name": "VIX", "value": f"{vix:.1f}", "status": "bullish", "note": "<20 正常"})
    elif vix > 30:
        if row.get("VIX_panic_bounce", False):
            score += 2; details.append({"name": "VIX", "value": f"{vix:.1f}", "status": "bullish", "note": "恐慌底反彈"})
        else:
            score -= 2; details.append({"name": "VIX", "value": f"{vix:.1f}", "status": "bearish", "note": ">30 恐慌"})
    elif vix > 25:
        score -= 1; details.append({"name": "VIX", "value": f"{vix:.1f}", "status": "bearish", "note": ">25 升溫"})
    else:
        score -= 0.5; details.append({"name": "VIX", "value": f"{vix:.1f}", "status": "neutral", "note": "20-25 留意"})

    dxy_trend = row.get("DXY_trend", "平")
    dxy = row.get("DXY", 0)
    if dxy_trend == "跌":
        score += 0.5; details.append({"name": "DXY", "value": f"{dxy:.2f}", "status": "bullish", "note": "美元走弱"})
    elif dxy_trend == "漲":
        score -= 0.5; details.append({"name": "DXY", "value": f"{dxy:.2f}", "status": "bearish", "note": "美元走強"})
    else:
        details.append({"name": "DXY", "value": f"{dxy:.2f}", "status": "neutral", "note": "持平"})

    oil_trend = row.get("Oil_trend", "平")
    oil = row.get("Oil", 0)
    if oil_trend == "漲":
        score += 0.3; details.append({"name": "Oil", "value": f"${oil:.2f}", "status": "bullish", "note": "需求漲"})
    elif oil_trend == "跌":
        score -= 0.3; details.append({"name": "Oil", "value": f"${oil:.2f}", "status": "bearish", "note": "需求弱"})
    else:
        details.append({"name": "Oil", "value": f"${oil:.2f}", "status": "neutral", "note": "持平"})

    gold_trend = row.get("Gold_trend", "平")
    gold = row.get("Gold", 0)
    if gold_trend == "跌":
        score += 0.3; details.append({"name": "Gold", "value": f"${gold:.0f}", "status": "bullish", "note": "避險降"})
    elif gold_trend == "漲":
        score -= 0.3; details.append({"name": "Gold", "value": f"${gold:.0f}", "status": "bearish", "note": "避險升"})
    else:
        details.append({"name": "Gold", "value": f"${gold:.0f}", "status": "neutral", "note": "持平"})

    yield_trend = row.get("Yield_trend", "平")
    yld = row.get("Yield", 4)
    if yield_trend == "跌":
        score += 0.5; details.append({"name": "Yield", "value": f"{yld:.2f}%", "status": "bullish", "note": "寬鬆"})
    elif yield_trend == "漲" and yld > 4.5:
        score -= 0.5; details.append({"name": "Yield", "value": f"{yld:.2f}%", "status": "bearish", "note": "急升"})
    else:
        details.append({"name": "Yield", "value": f"{yld:.2f}%", "status": "neutral", "note": "穩定"})

    rsi = row.get("RSI", 50)
    if rsi > 60:
        score += 0.5; details.append({"name": "RSI", "value": f"{rsi:.0f}", "status": "bullish", "note": "多頭"})
    elif rsi < 30:
        score += 1; details.append({"name": "RSI", "value": f"{rsi:.0f}", "status": "bullish", "note": "超賣反彈"})
    elif rsi < 40:
        score -= 0.5; details.append({"name": "RSI", "value": f"{rsi:.0f}", "status": "bearish", "note": "偏弱"})
    else:
        details.append({"name": "RSI", "value": f"{rsi:.0f}", "status": "neutral", "note": "中性"})

    above_ma20 = row.get("above_MA20", False)
    ma20 = row.get("MA20", 0)
    if above_ma20:
        score += 0.5; details.append({"name": "MA20", "value": f"{ma20:.2f}", "status": "bullish", "note": "站上"})
    else:
        score -= 0.5; details.append({"name": "MA20", "value": f"{ma20:.2f}", "status": "bearish", "note": "跌破"})

    above_ma60 = row.get("above_MA60", False)
    if above_ma60: score += 0.3
    else: score -= 0.3

    mom = row.get("momentum_20", 0)
    if mom > 5: score += 0.5
    elif mom < -5: score -= 0.5

    if score >= 2:
        signal = "LONG"
        label = "做多"
        color = "green"
        position = 100
    elif score <= -1.5:
        signal = "SHORT"
        label = "避險"
        color = "red"
        position = 0
    else:
        signal = "HOLD"
        label = "觀望"
        color = "yellow"
        position = 70

    return {
        "signal": signal, "label": label, "color": color,
        "score": round(score, 1), "position": position,
        "ma60": round(float(row.get("MA60", 0)), 2),
        "details": details
    }

def calc_catastrophe(row):
    """計算災難出場(5取3) / 安全進場(4取3) 條件"""
    import numpy as np

    price = float(row.get("0050", 0))
    vix = float(row.get("VIX", 20))
    mom = float(row.get("momentum_20", 0))
    ma120 = float(row.get("MA120", 0))
    ma60 = float(row.get("MA60", 0))
    dd = float(row.get("drawdown", 0))
    smh = float(row.get("SMH", 0)) if "SMH" in row.index else 0
    smh_ma50 = float(row.get("SMH_MA50", 0)) if "SMH_MA50" in row.index else 0

    # 處理 NaN
    for v in [ma120, ma60, smh_ma50, dd, mom]:
        if np.isnan(v):
            return {"exit_score": 0, "entry_score": 0, "exit_triggered": False,
                    "entry_triggered": False, "exit_conditions": [], "entry_conditions": [],
                    "status": "data_insufficient"}

    # 5 個出場條件
    exit_conditions = [
        {"name": "0050 < MA120×0.97", "threshold": f"< {ma120*0.97:.2f}",
         "value": f"{price:.2f}", "met": price < ma120 * 0.97},
        {"name": "VIX > 28", "threshold": "> 28",
         "value": f"{vix:.1f}", "met": vix > 28},
        {"name": "動能 < -8%", "threshold": "< -8%",
         "value": f"{mom:.1f}%", "met": mom < -8},
        {"name": "SMH < MA50", "threshold": f"< {smh_ma50:.2f}",
         "value": f"{smh:.2f}", "met": smh < smh_ma50 and smh > 0},
        {"name": "回撤 > 8%", "threshold": "< -8%",
         "value": f"{dd:.1f}%", "met": dd < -8},
    ]

    # 4 個進場條件
    entry_conditions = [
        {"name": "0050 > MA60", "threshold": f"> {ma60:.2f}",
         "value": f"{price:.2f}", "met": price > ma60},
        {"name": "VIX < 25", "threshold": "< 25",
         "value": f"{vix:.1f}", "met": vix < 25},
        {"name": "動能 > 0%", "threshold": "> 0%",
         "value": f"{mom:.1f}%", "met": mom > 0},
        {"name": "SMH > MA50", "threshold": f"> {smh_ma50:.2f}",
         "value": f"{smh:.2f}", "met": smh > smh_ma50 and smh > 0},
    ]

    exit_score = sum(1 for c in exit_conditions if c["met"])
    entry_score = sum(1 for c in entry_conditions if c["met"])

    # 綜合狀態
    if exit_score >= 3:
        status = "exit_triggered"
    elif exit_score >= 2:
        status = "exit_warning"
    elif entry_score >= 3:
        status = "entry_safe"
    else:
        status = "normal"

    return {
        "exit_score": exit_score,
        "entry_score": entry_score,
        "exit_triggered": exit_score >= 3,
        "entry_triggered": entry_score >= 3,
        "exit_conditions": exit_conditions,
        "entry_conditions": entry_conditions,
        "status": status,
    }

def run_backtest(start, end):
    """跑回測"""
    df = fetch_data_range(start, end)
    if len(df) < 30:
        return {"error": "數據不足"}

    df = add_indicators(df)
    # 去掉前面指標還沒算出來的 NaN 行
    df = df.dropna(subset=["RSI", "MA20"])

    if len(df) < 20:
        return {"error": "有效數據不足"}

    signals = df.apply(calc_signal, axis=1)
    sig_series = signals.apply(lambda x: 1 if x["signal"]=="LONG" else (-1 if x["signal"]=="SHORT" else 0))

    df["daily_ret"] = df["0050"].pct_change().fillna(0)
    pos = sig_series.shift(1).map({1:1.0, 0:0.7, -1:0.0}).fillna(0.7)
    strat_ret = df["daily_ret"] * pos
    cum_bh = (1 + df["daily_ret"]).cumprod()
    cum_st = (1 + strat_ret).cumprod()

    def safe_round(val, n=2):
        try:
            import math
            v = float(val)
            return 0 if math.isnan(v) or math.isinf(v) else round(v, n)
        except:
            return 0

    bh_ret = safe_round((cum_bh.iloc[-1] - 1) * 100)
    st_ret = safe_round((cum_st.iloc[-1] - 1) * 100)
    bh_dd = safe_round(((cum_bh / cum_bh.cummax()) - 1).min() * 100)
    st_dd = safe_round(((cum_st / cum_st.cummax()) - 1).min() * 100)

    # 月度
    monthly = []
    m_bh = df["daily_ret"].resample("ME").apply(lambda x: ((1+x).prod()-1)*100)
    m_st = strat_ret.resample("ME").apply(lambda x: ((1+x).prod()-1)*100)
    for m in m_bh.index:
        monthly.append({
            "month": m.strftime("%Y-%m"),
            "bh": safe_round(m_bh.get(m, 0)),
            "strat": safe_round(m_st.get(m, 0)),
        })

    # 每日曲線
    curve = []
    for i, idx in enumerate(df.index):
        if i % 3 == 0 or i == len(df) - 1:
            curve.append({
                "date": idx.strftime("%Y-%m-%d"),
                "bh": safe_round((cum_bh.iloc[i] - 1) * 100),
                "strat": safe_round((cum_st.iloc[i] - 1) * 100),
            })

    long_d = (sig_series == 1).sum()
    hold_d = (sig_series == 0).sum()
    short_d = (sig_series == -1).sum()

    return {
        "period": f"{df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}",
        "days": len(df),
        "bh_return": bh_ret,
        "strat_return": st_ret,
        "excess": safe_round(st_ret - bh_ret),
        "bh_drawdown": bh_dd,
        "strat_drawdown": st_dd,
        "long_days": int(long_d),
        "hold_days": int(hold_d),
        "short_days": int(short_d),
        "monthly": monthly,
        "curve": curve,
        "p0050_start": safe_round(df["0050"].iloc[0]),
        "p0050_end": safe_round(df["0050"].iloc[-1]),
    }

# ===== Routes =====

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")

def load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/signal")
def api_signal():
    """取得當前信號（含數據驗證 + 時間提示）"""
    try:
        df = fetch_data("3mo")
        if len(df) < 20:
            return jsonify({"error": "數據不足"}), 500
        df = add_indicators(df)

        # 數據驗證
        data_valid, data_warnings = validate_data(df)

        latest = df.iloc[-1]
        result = calc_signal(latest)
        result["date"] = df.index[-1].strftime("%Y-%m-%d")
        result["price"] = round(latest["0050"], 2)

        # 災難出場/進場條件
        result["catastrophe"] = calc_catastrophe(latest)

        # 數據新鮮度
        last_date = df.index[-1]
        days_old = (datetime.now() - last_date.to_pydatetime().replace(tzinfo=None)).days
        result["data_age_days"] = days_old
        if days_old > 3:
            data_warnings.append(f"⚠️ 數據已 {days_old} 天未更新（最後日期：{result['date']}）")

        # 如果數據有嚴重警告，降級信號
        if not data_valid:
            result["original_signal"] = result["signal"]
            result["signal"] = "HOLD"
            result["label"] = "觀望（數據異常）"
            result["color"] = "yellow"
            result["position"] = 70
            result["data_override"] = True

        result["data_valid"] = data_valid
        result["data_warnings"] = data_warnings

        # 時間提示
        result["timing"] = get_timing_info()

        # 備援數據源交叉驗證
        try:
            cross_results = cross_validate(df, data_warnings)
            result["cross_validation"] = cross_results
            result["data_warnings"] = data_warnings  # 可能被 cross_validate 新增了
            # 重新檢查是否通過
            has_critical = len([w for w in data_warnings if "⚠️" in w]) > 0
            result["data_valid"] = not has_critical
        except:
            result["cross_validation"] = []

        if len(df) >= 2:
            prev = calc_signal(df.iloc[-2])
            result["prev_signal"] = prev["signal"]
            result["signal_changed"] = prev["signal"] != result["signal"]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/api/backtest")
def api_backtest():
    try:
        start = request.args.get("start", "2025-03-15")
        end = request.args.get("end", "2026-03-15")
        result = run_backtest(start, end)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/history")
def api_history():
    try:
        df = fetch_data("3mo")
        df = add_indicators(df)
        history = []
        for idx, row in df.tail(30).iterrows():
            sig = calc_signal(row)
            cat = calc_catastrophe(row)
            history.append({
                "date": idx.strftime("%Y-%m-%d"),
                "price": round(row["0050"], 2),
                "signal": sig["signal"],
                "label": sig["label"],
                "score": sig["score"],
                "ma20": round(row["MA20"], 2) if pd.notna(row.get("MA20")) else None,
                "ma60": round(row["MA60"], 2) if pd.notna(row.get("MA60")) else None,
                "ma120": round(row["MA120"], 2) if pd.notna(row.get("MA120")) else None,
                "vix": round(row["VIX"], 2) if "VIX" in row and pd.notna(row["VIX"]) else None,
                "exit_score": cat["exit_score"],
                "entry_score": cat["entry_score"],
            })
        return jsonify(history)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===== Portfolio & Trade APIs =====

@app.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    """取得投資組合"""
    portfolio = load_json(PORTFOLIO_FILE, None)
    if portfolio is None or not isinstance(portfolio, dict) or "initial_capital" not in portfolio:
        return jsonify({"status": "empty"})
    # 取得最新 0050 價格
    try:
        df = fetch_data("5d")
        if len(df) > 0:
            portfolio["current_price"] = round(df["0050"].iloc[-1], 2)
            portfolio["price_date"] = df.index[-1].strftime("%Y-%m-%d")
            shares = portfolio.get("shares", 0)
            cash = portfolio.get("cash", 0)
            stock_val = shares * portfolio["current_price"]
            total = stock_val + cash
            initial = portfolio.get("initial_capital", total)
            portfolio["stock_value"] = round(stock_val, 0)
            portfolio["total_value"] = round(total, 0)
            portfolio["pnl"] = round(total - initial, 0)
            portfolio["pnl_pct"] = round((total / initial - 1) * 100, 2) if initial > 0 else 0
            portfolio["position_pct"] = round(stock_val / total * 100, 1) if total > 0 else 0
    except:
        pass
    portfolio["status"] = "active"
    return jsonify(portfolio)

@app.route("/api/portfolio/create", methods=["POST"])
def create_portfolio():
    """建立投資組合"""
    data = request.get_json()
    capital = float(data.get("capital", 0))
    if capital <= 0:
        return jsonify({"error": "請輸入有效金額"}), 400
    portfolio = {
        "initial_capital": capital,
        "cash": capital,
        "shares": 0,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "strategy": "five_indicators_v2",
    }
    save_json(PORTFOLIO_FILE, portfolio)
    save_json(TRADES_FILE, [])
    return jsonify({"ok": True, "portfolio": portfolio})

@app.route("/api/trade", methods=["POST"])
def record_trade():
    """記錄交易"""
    portfolio = load_json(PORTFOLIO_FILE, None)
    if portfolio is None:
        return jsonify({"error": "尚未建立投資組合"}), 400

    data = request.get_json()
    action = data.get("action")  # "buy" or "sell"
    shares_count = int(data.get("shares", 0))
    price = float(data.get("price", 0))
    note = data.get("note", "")

    if shares_count <= 0 or price <= 0:
        return jsonify({"error": "請輸入有效的股數和價格"}), 400

    amount = shares_count * price
    fee = round(amount * 0.001425, 0)  # 手續費 0.1425%
    tax = round(amount * 0.001, 0) if action == "sell" else 0  # 賣出交易稅 0.1%

    if action == "buy":
        total_cost = amount + fee
        if total_cost > portfolio["cash"]:
            return jsonify({"error": f"現金不足：需要 {total_cost:,.0f}，只有 {portfolio['cash']:,.0f}"}), 400
        portfolio["cash"] -= total_cost
        portfolio["shares"] += shares_count
    elif action == "sell":
        if shares_count > portfolio["shares"]:
            return jsonify({"error": f"股數不足：持有 {portfolio['shares']:,}，要賣 {shares_count:,}"}), 400
        total_revenue = amount - fee - tax
        portfolio["cash"] += total_revenue
        portfolio["shares"] -= shares_count
    else:
        return jsonify({"error": "action 必須是 buy 或 sell"}), 400

    portfolio["cash"] = round(portfolio["cash"], 0)
    save_json(PORTFOLIO_FILE, portfolio)

    # 記錄交易
    trades = load_json(TRADES_FILE, [])
    trade = {
        "id": len(trades) + 1,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "action": action,
        "shares": shares_count,
        "price": price,
        "amount": round(amount, 0),
        "fee": fee,
        "tax": tax,
        "net": round(amount + fee + tax if action == "buy" else amount - fee - tax, 0),
        "note": note,
        "cash_after": portfolio["cash"],
        "shares_after": portfolio["shares"],
    }
    trades.append(trade)
    save_json(TRADES_FILE, trades)

    return jsonify({"ok": True, "trade": trade, "portfolio": portfolio})

@app.route("/api/trades", methods=["GET"])
def get_trades():
    """取得交易紀錄"""
    trades = load_json(TRADES_FILE, [])
    return jsonify(trades)

@app.route("/api/portfolio/reset", methods=["POST"])
def reset_portfolio():
    """重設投資組合"""
    for f in [PORTFOLIO_FILE, TRADES_FILE]:
        if os.path.exists(f):
            os.remove(f)
    return jsonify({"ok": True})

# ===== Strategy Registry =====

STRATEGIES = [
    {
        "id": "five_indicators_v2",
        "name": "五大指標 v2",
        "description": "VIX + DXY + Oil + Gold + Yield + RSI + MA20/MA60 + 動能",
        "status": "active",
        "version": "2.0",
        "indicators": 9,
        "backtest_annual": "+74.5%",
        "backtest_sharpe": "3.84",
    },
    {
        "id": "catastrophe_exit",
        "name": "災難出場 v1",
        "description": "5取3出場 + 4取3進場 + 5日冷卻 + 20萬加碼",
        "status": "active",
        "version": "1.0",
        "indicators": 5,
        "backtest_annual": "+326%（11年）",
        "backtest_sharpe": "—",
    },
    {
        "id": "trump_code",
        "name": "川普密碼",
        "description": "Truth Social 貼文分析 × S&P 500 預測模型",
        "status": "planned",
        "version": "-",
        "note": "偏多信號過強，需優化為事件觸發模式",
    },
    {
        "id": "pentagon_pizza",
        "name": "五角大廈 Pizza 指數",
        "description": "美國國防部周邊 Pizza 外送量黑天鵝預警",
        "status": "planned",
        "version": "-",
        "note": "無歷史數據，僅供即時預警參考",
    },
    {
        "id": "foreign_investor",
        "name": "外資買賣超策略",
        "description": "台股外資連續買賣超天數 + 融資餘額變化",
        "status": "planned",
        "version": "-",
        "note": "待開發：接入台灣證交所 API",
    },
]

# ===== Config API =====

@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = get_config()
    def mask(key_name):
        key = cfg.get(key_name, "")
        if len(key) > 8: return key[:4] + "****" + key[-4:]
        return "已設定" if key else "未設定"
    return jsonify({
        "fred_status": mask("fred_api_key"),
        "fred_configured": bool(cfg.get("fred_api_key", "")),
        "alpha_vantage_status": mask("alpha_vantage_key"),
        "alpha_vantage_configured": bool(cfg.get("alpha_vantage_key", "")),
        "polygon_status": mask("polygon_key"),
        "polygon_configured": bool(cfg.get("polygon_key", "")),
    })

@app.route("/api/config", methods=["POST"])
def api_set_config():
    data = request.get_json()
    cfg = get_config()
    for key in ["fred_api_key", "alpha_vantage_key", "polygon_key", "tg_bot_token", "tg_chat_id"]:
        if key in data:
            cfg[key] = data[key].strip()
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/strategies")
def get_strategies():
    return jsonify(STRATEGIES)

# ===== 操作紀錄 (Operations Log) =====
OPS_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ops_log.json")

@app.route("/api/ops", methods=["GET"])
def get_ops_log():
    """取得操作紀錄"""
    ops = load_json(OPS_LOG_FILE, [])
    # 計算累計投入和當前市值
    total_invested = 0
    total_shares = 0
    total_added = 0
    for op in ops:
        if op.get("type") == "entry":
            total_invested += op.get("amount", 0)
            total_shares += op.get("shares", 0)
            total_added += op.get("add_amount", 0)
        elif op.get("type") == "exit":
            total_shares = 0  # 全部賣出
    # 取得當前價格
    try:
        df = fetch_data("5d")
        current_price = round(df["0050"].iloc[-1], 2) if len(df) > 0 else 0
    except:
        current_price = 0
    market_val = total_shares * current_price if total_shares > 0 else 0
    return jsonify({
        "ops": ops,
        "summary": {
            "total_invested": total_invested,
            "total_added": total_added,
            "total_shares": total_shares,
            "current_price": current_price,
            "market_value": round(market_val, 0),
            "pnl": round(market_val - total_invested, 0),
            "pnl_pct": round((market_val / total_invested - 1) * 100, 1) if total_invested > 0 else 0,
        }
    })

@app.route("/api/ops", methods=["POST"])
def add_ops_log():
    """新增操作紀錄"""
    data = request.get_json()
    ops = load_json(OPS_LOG_FILE, [])
    entry = {
        "id": len(ops) + 1,
        "date": data.get("date", datetime.now().strftime("%Y-%m-%d")),
        "type": data.get("type", "entry"),  # entry / exit
        "price": data.get("price", 0),
        "shares": data.get("shares", 0),
        "amount": data.get("amount", 0),
        "add_amount": data.get("add_amount", 0),  # 加碼金額
        "exit_score": data.get("exit_score", 0),
        "entry_score": data.get("entry_score", 0),
        "note": data.get("note", ""),
    }
    ops.append(entry)
    save_json(OPS_LOG_FILE, ops)
    return jsonify({"ok": True, "entry": entry})

@app.route("/api/ops/reset", methods=["POST"])
def reset_ops_log():
    """清除操作紀錄"""
    save_json(OPS_LOG_FILE, [])
    return jsonify({"ok": True})

# ===== Telegram 通知 =====
import urllib.request as urlreq

def send_telegram(message):
    """發送 Telegram 訊息（優先 env，再讀 config.json）"""
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        cfg = get_config()
        token = token or cfg.get("tg_bot_token", "")
        chat_id = chat_id or cfg.get("tg_chat_id", "")
    if not token or not chat_id:
        print("[TG] Missing TG_BOT_TOKEN or TG_CHAT_ID")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "HTML"}).encode()
        req = urlreq.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urlreq.urlopen(req, timeout=10)
        print(f"[TG] Sent: {message[:50]}...")
        return True
    except Exception as e:
        print(f"[TG] Error: {e}")
        return False

@app.route("/api/cron/signal-check")
def cron_signal_check():
    """Vercel Cron：每日檢查信號並通知 Telegram"""
    try:
        df = fetch_data("3mo")
        if len(df) < 20:
            return jsonify({"error": "數據不足"}), 500
        df = add_indicators(df)

        latest = calc_signal(df.iloc[-1])
        cur_signal = latest["signal"]
        cur_score = latest["score"]
        price_0050 = round(float(df.iloc[-1]["0050"]), 2)
        date_str = df.index[-1].strftime("%Y-%m-%d")

        # 取得前一天信號
        prev_signal = cur_signal
        if len(df) >= 2:
            prev = calc_signal(df.iloc[-2])
            prev_signal = prev["signal"]

        transition = f"{prev_signal}→{cur_signal}"

        # 操作建議（LONG=做多/綠, HOLD=觀望/黃, SHORT=避險/紅）
        actions = {
            "LONG→SHORT":  ("🚨🚨🚨 重大恐慌！立即加碼！",
                           "恐慌基金拿 <b>50 萬</b>，開盤市價買 <b>00631L</b>",
                           "市場從做多→避險，歷史回測最佳買入時機"),
            "HOLD→SHORT": ("⚠️ 小幅加碼信號",
                           "恐慌基金拿 <b>1 萬</b>，買 <b>00631L</b>",
                           "市場從觀望→避險，小額佈局"),
            "SHORT→LONG":  ("🎉 恐慌結束！",
                           "不賣出，繼續持有享受反彈",
                           "市場恢復做多，加碼部位開始獲利"),
            "SHORT→HOLD": ("🟡 市場回穩中",
                           "不動作，繼續觀察",
                           "從恐慌回暖，尚未完全安全"),
        }

        if transition in actions:
            title, action, reason = actions[transition]
            msg = (f"{title}\n\n"
                   f"📊 信號：{transition}\n"
                   f"📈 0050：{price_0050} 元\n"
                   f"🎯 評分：{cur_score:+.1f}\n"
                   f"📅 日期：{date_str}\n\n"
                   f"👉 {action}\n"
                   f"💡 {reason}")
            send_telegram(msg)
            return jsonify({"notified": True, "transition": transition, "action": action})
        else:
            # 每日簡報（非緊急）
            emoji = {"LONG": "🟢", "HOLD": "🟡", "SHORT": "🔴"}.get(cur_signal, "⚪")
            msg = (f"{emoji} 每日信號：{cur_signal}\n"
                   f"📈 0050：{price_0050} 元｜評分：{cur_score:+.1f}\n"
                   f"😎 今日不需操作")
            send_telegram(msg)
            return jsonify({"notified": True, "transition": transition, "action": "none"})

    except Exception as e:
        send_telegram(f"❌ Dashboard 異常：{str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("🚀 五大指標策略儀表板 v3")
    print("📊 http://localhost:5566")
    print("🔄 自動刷新：每 5 分鐘 | Cache TTL：5 分鐘")
    app.run(host="0.0.0.0", port=5566, debug=False)

