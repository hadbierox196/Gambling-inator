# PocketSignals – Pocket Option Signal Analyzer

A web dashboard hosted on Vercel that analyzes Pocket Option candle data
using 8 technical indicators and displays entry signals with confidence scores.

**No auto-trading. Analysis only.**

---

## Live Features

- Real candle data from Pocket Option WebSocket
- 8 indicators: RSI · MACD · Bollinger Bands · Stochastic · Alligator · Momentum · EMA Cross · ATR
- Confidence score (0–100%) per signal
- Entry zone, take profit & stop loss levels
- Auto-scan every 60 seconds
- Adjustable confidence threshold
- Demo mode fallback if WebSocket unavailable

---

## Project Structure

```
pocket_signals/
├── vercel.json          ← Vercel routing config
├── requirements.txt     ← Python deps for API functions
├── public/
│   └── index.html       ← Full dashboard (single file)
└── api/
    ├── analyze.py       ← GET /api/analyze?pair=EURUSD_otc&tf=M1
    └── scan.py          ← GET /api/scan?tf=M1  (all pairs)
```

---

## Deploy to Vercel (3 steps)

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "PocketSignals"
git remote add origin https://github.com/YOUR_USERNAME/pocket-signals.git
git push -u origin main
```

### 2. Import on Vercel

- Go to https://vercel.com/new
- Import your GitHub repo
- Framework preset: **Other**
- Click **Deploy**

### 3. Done

Your dashboard is live at `https://your-project.vercel.app`

---

## Run Locally

```bash
pip install numpy websockets
# Install Vercel CLI
npm i -g vercel
vercel dev
```

Then open http://localhost:3000

---

## API Endpoints

### `GET /api/scan?tf=M1`
Scans all 10 pairs on the given timeframe.

```json
{
  "signals": [
    {
      "pair": "EURUSD_otc",
      "timeframe": "M1",
      "direction": "BUY",
      "confidence": 82.3,
      "entry_price": 1.08432,
      "entry_zone": [1.08421, 1.08443],
      "tp": 1.08510,
      "sl": 1.08380,
      "expiry_mins": 2,
      "is_demo": false,
      "indicators": { ... }
    }
  ]
}
```

### `GET /api/analyze?pair=EURUSD_otc&tf=M5`
Analyze a single pair.

---

## Notes on Real Data

The bot connects to `wss://api-l.po.market/socket.io/` — the same
WebSocket Pocket Option's own platform uses. No credentials needed for
public candle data. If the connection fails, it falls back to simulated
demo data automatically.
