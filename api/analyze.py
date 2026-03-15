# Vercel Python Serverless Function — /api/analyze
# Compatible with Vercel's Python 3.11 runtime (WSGI-style handler)

import json, asyncio, ssl, time, sys, traceback
from datetime import datetime, timezone

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

PO_WS_ENDPOINTS = [
    "wss://api-l.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://api.po.market/socket.io/?EIO=4&transport=websocket",
]
TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900}


# ── WebSocket candle fetcher ──────────────────────────────────────────────────

async def fetch_real_candles(pair, tf, count=150):
    tf_sec = TF_SECONDS.get(tf, 60)
    end_ts = int(datetime.now(timezone.utc).timestamp())
    errors = []

    try:
        import websockets
    except ImportError:
        return None, "websockets_not_installed"

    for endpoint in PO_WS_ENDPOINTS:
        try:
            history_msg = "42" + json.dumps([
                "candles",
                {"asset": pair, "period": tf_sec, "time": end_ts, "count": count}
            ])
            candles = []
            ctx = ssl.create_default_context()
            async with websockets.connect(
                endpoint, ssl=ctx, open_timeout=8,
                additional_headers={
                    "Origin": "https://pocketoption.com",
                    "User-Agent": "Mozilla/5.0",
                }
            ) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
                await ws.send("40")
                await asyncio.wait_for(ws.recv(), timeout=5)
                await ws.send(history_msg)
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=3)
                        if not msg.startswith("42"):
                            continue
                        payload = json.loads(msg[2:])
                        if not (isinstance(payload, list) and len(payload) > 1):
                            continue
                        data = payload[1]
                        raw = (data.get("candles") or data.get("data") or
                               data.get("history") or
                               (data if isinstance(data, list) else []))
                        for c in raw:
                            try:
                                if isinstance(c, (list, tuple)) and len(c) >= 5:
                                    candles.append({"t":int(c[0]),"o":float(c[1]),
                                                    "h":float(c[3]),"l":float(c[4]),"c":float(c[2])})
                                elif isinstance(c, dict):
                                    candles.append({"t":int(c.get("time",0)),
                                                    "o":float(c.get("open",0)),
                                                    "h":float(c.get("high",0)),
                                                    "l":float(c.get("low",0)),
                                                    "c":float(c.get("close",0))})
                            except (TypeError, ValueError):
                                continue
                        if len(candles) >= 30:
                            candles.sort(key=lambda x: x["t"])
                            prices = [c["c"] for c in candles if c["c"] != 0]
                            if len(prices) >= 20:
                                return candles, "pocket_option_ws"
                    except asyncio.TimeoutError:
                        break
                    except json.JSONDecodeError:
                        continue
            errors.append(f"{endpoint.split('/')[2]}: got_{len(candles)}_candles")
        except asyncio.TimeoutError:
            errors.append(f"{endpoint.split('/')[2]}: timeout")
        except Exception as e:
            errors.append(f"{endpoint.split('/')[2]}: {type(e).__name__}")

    return None, " | ".join(errors) or "all_endpoints_failed"


# ── Indicators (pure Python fallback if numpy missing) ───────────────────────

def _mean(arr):
    return sum(arr) / len(arr) if arr else 0

def _std(arr):
    if not arr: return 0
    m = _mean(arr)
    return (sum((x - m) ** 2 for x in arr) / len(arr)) ** 0.5

def _ema_list(prices, p):
    if len(prices) < p: return [None] * len(prices)
    out = [None] * p
    out[-1] = _mean(prices[:p])
    k = 2 / (p + 1)
    for i in range(p, len(prices)):
        out.append(out[-1] * (1 - k) + prices[i] * k)
    return out

def _smma_list(prices, p):
    if len(prices) < p: return [None] * len(prices)
    out = [None] * p
    out[-1] = _mean(prices[:p])
    for i in range(p, len(prices)):
        out.append((out[-1] * (p - 1) + prices[i]) / p)
    return out

def ind_rsi(closes, p=14):
    if len(closes) < p + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = _mean(gains[:p]); al = _mean(losses[:p])
    for i in range(p, len(gains)):
        ag = (ag*(p-1) + gains[i]) / p
        al = (al*(p-1) + losses[i]) / p
    rs = ag / al if al else 999
    v = round(100 - 100 / (1 + rs), 2)
    if v < 30:   s, st = "BUY",     round((30 - v) / 30 * 100, 1)
    elif v > 70: s, st = "SELL",    round((v - 70) / 30 * 100, 1)
    else:        s, st = "NEUTRAL", 0.0
    return {"value": v, "signal": s, "strength": st}

def ind_macd(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig: return None
    ef = _ema_list(closes, fast)
    es = _ema_list(closes, slow)
    ml = [a - b for a, b in zip(ef, es) if a is not None and b is not None]
    if len(ml) < sig: return None
    sl = _ema_list(ml, sig)
    m = ml[-1]; s = sl[-1]
    if s is None: return None
    h = m - s
    if m > s and h > 0:   d, st = "BUY",     min(100, abs(h)/abs(m)*1000 if m else 50)
    elif m < s and h < 0: d, st = "SELL",    min(100, abs(h)/abs(m)*1000 if m else 50)
    else:                 d, st = "NEUTRAL", 0.0
    return {"macd": round(m,6), "signal_line": round(s,6),
            "histogram": round(h,6), "direction": d, "strength": round(st,1)}

def ind_bollinger(closes, p=20, dev=2.0):
    if len(closes) < p: return None
    window = closes[-p:]
    mid = _mean(window); std = _std(window)
    up = mid + dev*std; lo = mid - dev*std; pr = closes[-1]
    pb = (pr - lo) / (up - lo) if (up - lo) else 0.5
    w = (up - lo) / mid * 100
    if pr < lo:   s, st = "BUY",     min(100, (lo-pr)/std*50 if std else 0)
    elif pr > up: s, st = "SELL",    min(100, (pr-up)/std*50 if std else 0)
    else:         s, st = "NEUTRAL", 0.0
    return {"upper":round(up,5),"mid":round(mid,5),"lower":round(lo,5),
            "pct_b":round(pb,3),"width":round(w,3),"signal":s,"strength":round(st,1)}

def ind_stoch(highs, lows, closes, kp=14, dp=3, ob=80, os_=20):
    n = len(closes)
    if n < kp + dp: return None
    k_vals = []
    for i in range(kp-1, n):
        wh = max(highs[i-kp+1:i+1]); wl = min(lows[i-kp+1:i+1])
        k_vals.append(100*(closes[i]-wl)/(wh-wl) if wh!=wl else 50)
    if len(k_vals) < dp: return None
    d_vals = [_mean(k_vals[i:i+dp]) for i in range(len(k_vals)-dp+1)]
    k, d = k_vals[-1], d_vals[-1]
    if k < os_ and d < os_ and k > d:   s, st = "BUY",     min(100,(os_-k)/os_*200)
    elif k > ob and d > ob and k < d:   s, st = "SELL",    min(100,(k-ob)/(100-ob)*200)
    else:                                s, st = "NEUTRAL", 0.0
    return {"k":round(k,2),"d":round(d,2),"signal":s,"strength":round(st,1)}

def ind_alligator(highs, lows):
    median = [(h+l)/2 for h,l in zip(highs,lows)]
    if len(median) < 21: return None
    jaw_arr   = _smma_list(median, 13)
    teeth_arr = _smma_list(median, 8)
    lips_arr  = _smma_list(median, 5)
    def get(arr, shift):
        idx = len(arr) - 1 - shift
        return arr[idx] if idx >= 0 and arr[idx] is not None else None
    jaw = get(jaw_arr,8); teeth = get(teeth_arr,5); lips = get(lips_arr,3)
    if None in (jaw, teeth, lips): return None
    pr = median[-1]
    if lips > teeth > jaw and pr > lips:   s, st = "BUY",     min(100,abs(lips-jaw)/pr*10000)
    elif lips < teeth < jaw and pr < lips: s, st = "SELL",    min(100,abs(jaw-lips)/pr*10000)
    else:                                   s, st = "NEUTRAL", 0.0
    return {"jaw":round(jaw,5),"teeth":round(teeth,5),"lips":round(lips,5),
            "signal":s,"strength":round(st,1)}

def ind_momentum(closes, p=10):
    if len(closes) <= p: return None
    pct = (closes[-1] - closes[-1-p]) / closes[-1-p] * 100
    if pct > 0.02:    s, st = "BUY",     min(100, abs(pct)*20)
    elif pct < -0.02: s, st = "SELL",    min(100, abs(pct)*20)
    else:             s, st = "NEUTRAL", 0.0
    return {"value":round(pct,4),"signal":s,"strength":round(st,1)}

def ind_ema_cross(closes, fast=9, slow=21):
    if len(closes) < slow + 2: return None
    ef = _ema_list(closes, fast)
    es = _ema_list(closes, slow)
    if ef[-1] is None or es[-1] is None: return None
    if ef[-2] is None or es[-2] is None: return None
    dn = ef[-1] - es[-1]; dp_ = ef[-2] - es[-2]
    cu = dp_ <= 0 < dn; cd = dp_ >= 0 > dn
    if cu:       s, st = "BUY",     min(100, abs(dn)/closes[-1]*10000*2)
    elif cd:     s, st = "SELL",    min(100, abs(dn)/closes[-1]*10000*2)
    elif dn > 0: s, st = "BUY",     min(60,  abs(dn)/closes[-1]*10000)
    elif dn < 0: s, st = "SELL",    min(60,  abs(dn)/closes[-1]*10000)
    else:        s, st = "NEUTRAL", 0.0
    return {"ema_fast":round(ef[-1],5),"ema_slow":round(es[-1],5),
            "diff":round(dn,6),"crossed_up":bool(cu),"crossed_down":bool(cd),
            "signal":s,"strength":round(st,1)}

def ind_atr(highs, lows, closes, p=14):
    if len(closes) < p + 1: return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    atr = _mean(trs[-p:])
    pct = atr / closes[-1] * 100
    vol = "HIGH" if pct > 0.3 else ("MEDIUM" if pct > 0.1 else "LOW")
    return {"atr":round(atr,6),"pct":round(pct,3),"volatility":vol}


# ── Signal engine ─────────────────────────────────────────────────────────────

def compute_signal(pair, tf, raw_candles, data_source):
    if len(raw_candles) < 50:
        return None
    h = [c["h"] for c in raw_candles]
    l = [c["l"] for c in raw_candles]
    c = [c["c"] for c in raw_candles]

    rsi   = ind_rsi(c)
    macd  = ind_macd(c)
    bb    = ind_bollinger(c)
    stoch = ind_stoch(h, l, c)
    alig  = ind_alligator(h, l)
    mom   = ind_momentum(c)
    ema   = ind_ema_cross(c)
    atr   = ind_atr(h, l, c)

    vb, vs, sb, ss = 0, 0, [], []
    def tally(ind, sk="signal", stk="strength"):
        nonlocal vb, vs
        if not ind: return
        s = ind.get(sk) or ind.get("direction", "NEUTRAL")
        st = ind.get(stk, 0)
        if s == "BUY":    vb += 1; sb.append(st)
        elif s == "SELL": vs += 1; ss.append(st)

    tally(rsi); tally(macd, sk="direction"); tally(bb)
    tally(stoch); tally(alig); tally(mom); tally(ema)

    tv = sum(1 for x in [rsi,macd,bb,stoch,alig,mom,ema] if x)
    if not tv: return None

    if vb > vs:   direction, aligned, strengths = "BUY",     vb, sb
    elif vs > vb: direction, aligned, strengths = "SELL",    vs, ss
    else:         direction, aligned, strengths = "NEUTRAL", 0,  []

    conf = 0.0
    if direction != "NEUTRAL" and strengths:
        avg_st = _mean(strengths)
        conf = min(100, round(avg_st * 0.55 + (aligned/tv) * 100 * 0.45, 1))

    price = c[-1]
    atr_v = atr["atr"] if atr else price * 0.001
    ez_lo = round(price - 0.3*atr_v, 5)
    ez_hi = round(price + 0.3*atr_v, 5)
    tp = round(price + (2.0*atr_v  if direction=="BUY"  else -2.0*atr_v),  5)
    sl = round(price - (1.2*atr_v  if direction=="BUY"  else -1.2*atr_v),  5)
    expiry = max(1, TF_SECONDS.get(tf, 60) * 2 // 60)

    return {
        "pair": pair, "timeframe": tf, "direction": direction,
        "confidence": conf, "entry_price": round(price, 5),
        "entry_zone": [ez_lo, ez_hi], "tp": tp, "sl": sl,
        "expiry_mins": expiry, "aligned": aligned, "total_indicators": tv,
        "candles_used": len(raw_candles),
        "data_source": data_source,
        "is_real_data": True,
        "indicators": {
            "rsi": rsi, "macd": macd, "bollinger": bb,
            "stochastic": stoch, "alligator": alig,
            "momentum": mom, "ema_cross": ema, "atr": atr,
        },
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


# ── Vercel handler ────────────────────────────────────────────────────────────

async def _handle(pair, tf):
    raw, source = await fetch_real_candles(pair, tf)
    if raw is None:
        return {
            "error": "no_data", "reason": source,
            "pair": pair, "timeframe": tf,
            "message": "Could not fetch real candle data from Pocket Option. No signal generated.",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    sig = compute_signal(pair, tf, raw, source)
    if sig is None:
        return {
            "error": "insufficient_candles", "pair": pair, "timeframe": tf,
            "candles_received": len(raw),
            "message": f"Received {len(raw)} candles but need at least 50.",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    return sig


def handler(request, response):
    """Vercel Python handler (WSGI-compatible)."""
    try:
        pair = request.args.get("pair", "EURUSD_otc")
        tf   = request.args.get("tf",   "M1")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_handle(pair, tf))
        loop.close()
        body   = json.dumps(result)
        status = 200
    except Exception as e:
        body   = json.dumps({"error": "server_error", "detail": str(e),
                             "traceback": traceback.format_exc()})
        status = 500
    response.status_code = status
    response.headers["Content-Type"]                = "application/json"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response.make_response(body)


# ── Also support BaseHTTPRequestHandler for local dev ─────────────────────────
from http.server import BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        qs   = parse_qs(urlparse(self.path).query)
        pair = qs.get("pair", ["EURUSD_otc"])[0]
        tf   = qs.get("tf",   ["M1"])[0]
        try:
            loop   = asyncio.new_event_loop()
            result = loop.run_until_complete(_handle(pair, tf))
            loop.close()
            body, status = json.dumps(result), 200
        except Exception as e:
            body   = json.dumps({"error": "server_error", "detail": str(e)})
            status = 500
        self.send_response(status)
        self.send_header("Content-Type",               "application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(body.encode())
    def log_message(self, *a): pass
