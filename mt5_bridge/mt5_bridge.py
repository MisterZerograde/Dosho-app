import sys
import re
import json
import threading
from datetime import datetime, timedelta

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: pip install MetaTrader5")
    sys.exit(1)

try:
    from flask import Flask, jsonify, request
    from flask_cors import CORS
except ImportError:
    print("ERROR: pip install flask flask-cors")
    sys.exit(1)

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("ERROR: pip install pystray Pillow")
    sys.exit(1)

PORT = 5678
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Access-Control-Request-Private-Network"
    )
    return response


def strip_broker_suffix(symbol):
    return re.sub(r"\.[a-zA-Z]+\d*$", "", symbol).upper()


def deals_to_trades(deals):
    positions = {}
    for deal in deals:
        if deal.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            continue
        pid = deal.position_id
        if pid == 0:
            continue
        if pid not in positions:
            positions[pid] = {"opens": [], "closes": []}
        if deal.entry == mt5.DEAL_ENTRY_IN:
            positions[pid]["opens"].append(deal)
        elif deal.entry == mt5.DEAL_ENTRY_OUT:
            positions[pid]["closes"].append(deal)

    trades = []
    for pid, pos in positions.items():
        if not pos["opens"] or not pos["closes"]:
            continue

        open_d = pos["opens"][0]
        all_closes = pos["closes"]

        total_commission = sum(d.commission for d in pos["opens"]) + sum(
            d.commission for d in all_closes
        )
        total_swap = sum(d.swap for d in all_closes)
        total_profit = sum(d.profit for d in all_closes)
        pnl = round(total_profit + total_commission + total_swap, 2)

        last_close = max(all_closes, key=lambda d: d.time)
        close_px = last_close.price

        open_dt = datetime.fromtimestamp(open_d.time).strftime("%Y-%m-%dT%H:%M")
        close_dt = datetime.fromtimestamp(last_close.time).strftime("%Y-%m-%dT%H:%M")

        trades.append(
            {
                "id": str(pid),
                "symbol": strip_broker_suffix(open_d.symbol),
                "direction": "BUY" if open_d.type == mt5.DEAL_TYPE_BUY else "SELL",
                "openDt": open_dt,
                "closeDt": close_dt,
                "openPx": round(open_d.price, 5),
                "closePx": round(close_px, 5),
                "volume": open_d.volume,
                "pnl": pnl,
                "commission": round(total_commission, 2),
                "swap": round(total_swap, 2),
                "tag": "",
                "notes": last_close.comment or "",
            }
        )

    trades.sort(key=lambda t: t["openDt"])
    return trades


@app.route("/status")
def status():
    if mt5.initialize():
        info = mt5.account_info()
        mt5.shutdown()
        return jsonify(
            {
                "status": "ok",
                "mt5": "connected",
                "account": info.login if info else None,
                "server": info.server if info else None,
            }
        )
    return jsonify({"status": "ok", "mt5": "not_connected"})


@app.route("/sync")
def sync():
    days = int(request.args.get("days", 365))
    if not mt5.initialize():
        return (
            jsonify(
                {
                    "error": "Cannot connect to MT5. Make sure the MT5 terminal is running."
                }
            ),
            503,
        )
    try:
        from_date = datetime.now() - timedelta(days=days)
        deals = mt5.history_deals_get(from_date, datetime.now())
        if deals is None:
            return jsonify({"trades": [], "count": 0})
        result = deals_to_trades(deals)
        return jsonify({"trades": result, "count": len(result)})
    finally:
        mt5.shutdown()


def run_server():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


def create_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill="#7B2FBE")
    d.ellipse([22, 22, 42, 42], fill="white")
    return img


def quit_app(icon, _item):
    icon.stop()
    sys.exit(0)


def main():
    threading.Thread(target=run_server, daemon=True).start()
    print(f"MT5 Bridge running on http://127.0.0.1:{PORT}")
    print("Right-click the tray icon to quit.")

    icon = pystray.Icon(
        "MT5 Bridge",
        create_icon(),
        f"MT5 Bridge  port {PORT}",
        menu=pystray.Menu(
            pystray.MenuItem("MT5 Bridge", None, enabled=False),
            pystray.MenuItem(f"localhost:{PORT}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        ),
    )
    icon.run()


if __name__ == "__main__":
    main()
