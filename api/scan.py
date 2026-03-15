"""
Vercel Serverless Function – /api/scan
Scans all pairs. Pairs with no real data are returned with error:"no_data".
NO fake candles are ever generated.
"""
from http.server import BaseHTTPRequestHandler
import json, asyncio, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from analyze import fetch_real_candles, compute_signal
from datetime import datetime

PAIRS = [
    "EURUSD_otc","GBPUSD_otc","USDJPY_otc","AUDUSD_otc","EURGBP_otc",
    "USDCAD_otc","NZDUSD_otc","USDCHF_otc","EURJPY_otc","GBPJPY_otc",
]

async def _scan(tf):
    signals, no_data = [], []
    for pair in PAIRS:
        raw, source = await fetch_real_candles(pair, tf)
        if raw is None:
            no_data.append({"pair": pair, "reason": source})
            continue
        sig = compute_signal(pair, tf, raw, source)
        if sig:
            signals.append(sig)
        else:
            no_data.append({
                "pair": pair,
                "reason": f"only_{len(raw)}_candles_received_need_50"
            })
    signals.sort(key=lambda x: x["confidence"], reverse=True)
    return {
        "signals":        signals,
        "no_data_pairs":  no_data,
        "signals_count":  len(signals),
        "no_data_count":  len(no_data),
        "all_real_data":  len(no_data) == 0,
        "timeframe":      tf,
        "timestamp":      datetime.utcnow().isoformat() + "Z",
    }

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        tf = parse_qs(urlparse(self.path).query).get("tf",["M1"])[0]
        try:
            loop   = asyncio.new_event_loop()
            result = loop.run_until_complete(_scan(tf))
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
