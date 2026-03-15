"""
Vercel Serverless Function – /api/analyze
Fetches REAL candles from Pocket Option WebSocket.

STRICT POLICY:
  - If real candle data cannot be fetched → returns {"error": "no_data", ...}
  - NO fake / simulated / hallucinated candles are ever used
  - Every response includes a "data_source" field explaining where data came from
"""

from http.server import BaseHTTPRequestHandler
import json, asyncio, ssl, time
from datetime import datetime, timezone
import numpy as np

# ── Known Pocket Option WebSocket endpoints ───────────────────────────────────
# These are the actual endpoints PO's own platform connects to.
# If they change or block us, we return no_data — never fake data.
PO_WS_ENDPOINTS = [
    "wss://api-l.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://api.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://s3.po.market/socket.io/?EIO=4&transport=websocket",
]

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900}


# ═══════════════════════════════════════════════════════════════════════════════
# REAL CANDLE FETCHER  — returns (candles, source_str) or (None, reason_str)
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_real_candles(pair: str, tf: str, count: int = 150):
    """
    Attempt to pull real OHLC candles from Pocket Option WebSocket.
    Returns (list_of_candles, "pocket_option_ws") on success.
    Returns (None, reason_string)                  on failure.
    NEVER returns synthetic data.
    """
    tf_sec = TF_SECONDS.get(tf, 60)
    end_ts = int(datetime.now(timezone.utc).timestamp())

    try:
        import websockets
    except ImportError:
        return None, "websockets_package_not_installed"

    errors = []
    for endpoint in PO_WS_ENDPOINTS:
        try:
            history_msg = "42" + json.dumps([
                "candles",
                {"asset": pair, "period": tf_sec, "time": end_ts, "count": count}
            ])
            candles = []

            async with websockets.connect(
                endpoint,
                ssl=ssl.create_default_context(),
                open_timeout=8,
                additional_headers={
                    "Origin": "https://pocketoption.com",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                }
            ) as ws:
                # EIO4 handshake sequence
                handshake = await asyncio.wait_for(ws.recv(), timeout=5)
                await ws.send("40")                                   # namespace connect
                await asyncio.wait_for(ws.recv(), timeout=5)          # namespace ack
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

                        event, data = payload[0], payload[1]
                        raw = (
                            data.get("candles") or
                            data.get("data")    or
                            data.get("history") or
                            (data if isinstance(data, list) else [])
                        )
                        if not raw:
                            continue

                        for c in raw:
                            try:
                                if isinstance(c, (list, tuple)) and len(c) >= 5:
                                    candles.append({
                                        "t": int(c[0]),
                                        "o": float(c[1]),
                                        "h": float(c[3]),
                                        "l": float(c[4]),
                                        "c": float(c[2]),
                                    })
                                elif isinstance(c, dict):
                                    candles.append({
                                        "t": int(c.get("time", 0)),
                                        "o": float(c.get("open",  0)),
                                        "h": float(c.get("high",  0)),
                                        "l": float(c.get("low",   0)),
                                        "c": float(c.get("close", 0)),
                                    })
                            except (TypeError, ValueError):
                                continue

                        if len(candles) >= 30:
                            candles.sort(key=lambda x: x["t"])
                            # Sanity check: reject if prices are all zero
                            prices = [c["c"] for c in candles if c["c"] != 0]
                            if len(prices) < 20:
                                candles = []
                                errors.append(f"{endpoint}: prices_all_zero")
                                break
                            return candles, "pocket_option_ws"

                    except asyncio.TimeoutError:
                        break
                    except json.JSONDecodeError:
                        continue

            if candles:
                errors.append(f"{endpoint}: only_got_{len(candles)}_candles")
            else:
                errors.append(f"{endpoint}: no_candle_data_in_response")

        except asyncio.TimeoutError:
            errors.append(f"{endpoint}: connection_timeout")
        except Exception as e:
            errors.append(f"{endpoint}: {type(e).__name__}: {str(e)[:80]}")

    reason = " | ".join(errors) if errors else "all_endpoints_failed"
    return None, reason


# ═══════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

def _ema(s, p):
    out = np.full(len(s), np.nan)
    if len(s) < p: return out
    out[p-1] = np.mean(s[:p]); k = 2/(p+1)
    for i in range(p, len(s)): out[i] = s[i]*k + out[i-1]*(1-k)
    return out

def _smma(s, p):
    out = np.full(len(s), np.nan)
    if len(s) < p: return out
    out[p-1] = np.mean(s[:p])
    for i in range(p, len(s)): out[i] = (out[i-1]*(p-1)+s[i])/p
    return out

def ind_rsi(c, p=14):
    if len(c)<p+1: return None
    d=np.diff(c); g=np.where(d>0,d,0.); l=np.where(d<0,-d,0.)
    ag=np.mean(g[:p]); al=np.mean(l[:p])
    for i in range(p,len(g)): ag=(ag*(p-1)+g[i])/p; al=(al*(p-1)+l[i])/p
    rs=ag/al if al else 999; v=round(100-100/(1+rs),2)
    if v<30:   s,st="BUY",  round((30-v)/30*100,1)
    elif v>70: s,st="SELL", round((v-70)/30*100,1)
    else:      s,st="NEUTRAL",0.
    return {"value":v,"signal":s,"strength":st}

def ind_macd(c,fast=12,slow=26,sig=9):
    if len(c)<slow+sig: return None
    ml=_ema(c,fast)-_ema(c,slow); vl=ml[~np.isnan(ml)]
    if len(vl)<sig: return None
    sl=_ema(vl,sig); m=vl[-1]; s=sl[-1]; h=m-s
    if np.isnan(s): return None
    if m>s and h>0:    d,st="BUY",  min(100,abs(h)/abs(m)*1000 if m else 50)
    elif m<s and h<0:  d,st="SELL", min(100,abs(h)/abs(m)*1000 if m else 50)
    else:              d,st="NEUTRAL",0.
    return {"macd":round(float(m),6),"signal_line":round(float(s),6),
            "histogram":round(float(h),6),"direction":d,"strength":round(st,1)}

def ind_bollinger(c,p=20,dev=2.):
    if len(c)<p: return None
    mid=np.mean(c[-p:]); std=np.std(c[-p:])
    up=mid+dev*std; lo=mid-dev*std; pr=c[-1]
    pb=(pr-lo)/(up-lo) if (up-lo) else .5
    w=(up-lo)/mid*100
    if pr<lo:   s,st="BUY",  min(100,(lo-pr)/std*50)
    elif pr>up: s,st="SELL", min(100,(pr-up)/std*50)
    else:       s,st="NEUTRAL",0.
    return {"upper":round(float(up),5),"mid":round(float(mid),5),"lower":round(float(lo),5),
            "pct_b":round(float(pb),3),"width":round(float(w),3),"signal":s,"strength":round(st,1)}

def ind_stoch(h,l,c,kp=14,dp=3,ob=80,os_=20):
    n=len(c)
    if n<kp+dp: return None
    kv=np.full(n,np.nan)
    for i in range(kp-1,n):
        wh=np.max(h[i-kp+1:i+1]); wl=np.min(l[i-kp+1:i+1])
        kv[i]=100*(c[i]-wl)/(wh-wl) if wh!=wl else 50
    vk=kv[~np.isnan(kv)]
    if len(vk)<dp: return None
    dv=np.convolve(vk,np.ones(dp)/dp,mode="valid"); k,d=vk[-1],dv[-1]
    if k<os_ and d<os_ and k>d:    s,st="BUY",  min(100,(os_-k)/os_*200)
    elif k>ob and d>ob and k<d:    s,st="SELL", min(100,(k-ob)/(100-ob)*200)
    else:                          s,st="NEUTRAL",0.
    return {"k":round(float(k),2),"d":round(float(d),2),"signal":s,"strength":round(st,1)}

def ind_alligator(h,l):
    med=(h+l)/2; n=len(med)
    if n<21: return None
    def sl(a,sh): idx=len(a)-1-sh; return float(a[idx]) if idx>=0 and not np.isnan(a[idx]) else None
    jaw=sl(_smma(med,13),8); teeth=sl(_smma(med,8),5); lips=sl(_smma(med,5),3)
    if None in(jaw,teeth,lips): return None
    pr=float(med[-1])
    if lips>teeth>jaw and pr>lips:   s,st="BUY",  min(100,abs(lips-jaw)/pr*10000)
    elif lips<teeth<jaw and pr<lips: s,st="SELL", min(100,abs(jaw-lips)/pr*10000)
    else:                            s,st="NEUTRAL",0.
    return {"jaw":round(jaw,5),"teeth":round(teeth,5),"lips":round(lips,5),
            "signal":s,"strength":round(st,1)}

def ind_momentum(c,p=10):
    if len(c)<=p: return None
    pct=(c[-1]-c[-1-p])/c[-1-p]*100
    if pct>0.02:    s,st="BUY",  min(100,abs(pct)*20)
    elif pct<-0.02: s,st="SELL", min(100,abs(pct)*20)
    else:           s,st="NEUTRAL",0.
    return {"value":round(float(pct),4),"signal":s,"strength":round(st,1)}

def ind_ema_cross(c,fast=9,slow=21):
    if len(c)<slow+2: return None
    ef=_ema(c,fast); es=_ema(c,slow)
    if np.isnan(ef[-1]) or np.isnan(es[-1]): return None
    dn=float(ef[-1]-es[-1]); dp_=float(ef[-2]-es[-2])
    cu=dp_<=0<dn; cd=dp_>=0>dn
    if cu:       s,st="BUY",  min(100,abs(dn)/float(c[-1])*10000*2)
    elif cd:     s,st="SELL", min(100,abs(dn)/float(c[-1])*10000*2)
    elif dn>0:   s,st="BUY",  min(60, abs(dn)/float(c[-1])*10000)
    elif dn<0:   s,st="SELL", min(60, abs(dn)/float(c[-1])*10000)
    else:        s,st="NEUTRAL",0.
    return {"ema_fast":round(float(ef[-1]),5),"ema_slow":round(float(es[-1]),5),
            "diff":round(dn,6),"crossed_up":bool(cu),"crossed_down":bool(cd),
            "signal":s,"strength":round(st,1)}

def ind_atr(h,l,c,p=14):
    if len(c)<p+1: return None
    tr=np.maximum(h[1:]-l[1:],np.maximum(abs(h[1:]-c[:-1]),abs(l[1:]-c[:-1])))
    atr=float(np.mean(tr[-p:])); pct=atr/float(c[-1])*100
    vol="HIGH" if pct>0.3 else("MEDIUM" if pct>0.1 else "LOW")
    return {"atr":round(atr,6),"pct":round(pct,3),"volatility":vol}


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def compute_signal(pair, tf, raw_candles, data_source):
    h = np.array([c["h"] for c in raw_candles], dtype=float)
    l = np.array([c["l"] for c in raw_candles], dtype=float)
    c = np.array([c["c"] for c in raw_candles], dtype=float)
    if len(c) < 50: return None

    rsi   = ind_rsi(c)
    macd  = ind_macd(c)
    bb    = ind_bollinger(c)
    stoch = ind_stoch(h,l,c)
    alig  = ind_alligator(h,l)
    mom   = ind_momentum(c)
    ema   = ind_ema_cross(c)
    atr   = ind_atr(h,l,c)

    vb,vs,sb,ss = 0,0,[],[]
    def tally(ind, sk="signal", stk="strength"):
        nonlocal vb,vs
        if not ind: return
        s=ind.get(sk) or ind.get("direction","NEUTRAL"); st=ind.get(stk,0)
        if s=="BUY":  vb+=1; sb.append(st)
        elif s=="SELL": vs+=1; ss.append(st)

    tally(rsi); tally(macd,sk="direction"); tally(bb)
    tally(stoch); tally(alig); tally(mom); tally(ema)

    tv=sum(1 for x in [rsi,macd,bb,stoch,alig,mom,ema] if x)
    if not tv: return None

    if vb>vs:   direction,aligned,strengths="BUY", vb,sb
    elif vs>vb: direction,aligned,strengths="SELL",vs,ss
    else:       direction,aligned,strengths="NEUTRAL",0,[]

    if direction=="NEUTRAL": conf=0.
    else:
        avg_st=float(np.mean(strengths)) if strengths else 0
        conf=min(100,round(avg_st*.55+(aligned/tv)*100*.45,1))

    price=float(c[-1])
    atr_v=atr["atr"] if atr else price*0.001
    ez_lo=round(price-0.3*atr_v,5); ez_hi=round(price+0.3*atr_v,5)
    tp=round(price+(2.*atr_v if direction=="BUY" else -2.*atr_v),5)
    sl=round(price-(1.2*atr_v if direction=="BUY" else -1.2*atr_v),5)
    expiry=max(1,TF_SECONDS.get(tf,60)*2//60)

    return {
        "pair":pair,"timeframe":tf,"direction":direction,
        "confidence":conf,"entry_price":price,
        "entry_zone":[ez_lo,ez_hi],"tp":tp,"sl":sl,
        "expiry_mins":expiry,"aligned":aligned,"total_indicators":tv,
        "candles_used": len(raw_candles),
        "data_source": data_source,           # always explicit
        "is_real_data": True,                 # only set when real
        "indicators":{"rsi":rsi,"macd":macd,"bollinger":bb,
                      "stochastic":stoch,"alligator":alig,
                      "momentum":mom,"ema_cross":ema,"atr":atr},
        "timestamp":datetime.utcnow().isoformat()+"Z",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# VERCEL HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle(pair, tf):
    raw, source = await fetch_real_candles(pair, tf)

    # ── STRICT: no data → return error, never fake it ─────────────────────────
    if raw is None:
        return {
            "error":       "no_data",
            "reason":      source,
            "pair":        pair,
            "timeframe":   tf,
            "message":     "Could not fetch real candle data from Pocket Option. "
                           "No signal generated.",
            "timestamp":   datetime.utcnow().isoformat() + "Z",
        }

    sig = compute_signal(pair, tf, raw, source)
    if sig is None:
        return {
            "error":     "insufficient_candles",
            "pair":      pair,
            "timeframe": tf,
            "candles_received": len(raw),
            "message":   f"Received {len(raw)} candles but need ≥50 to compute indicators.",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    return sig


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        qs   = parse_qs(urlparse(self.path).query)
        pair = qs.get("pair",["EURUSD_otc"])[0]
        tf   = qs.get("tf",  ["M1"])[0]
        try:
            loop   = asyncio.new_event_loop()
            result = loop.run_until_complete(_handle(pair, tf))
            loop.close()
            body, status = json.dumps(result), 200
        except Exception as e:
            body   = json.dumps({"error":"server_error","detail":str(e)})
            status = 500
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self,*a): pass
