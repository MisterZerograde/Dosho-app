import os
import sys
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

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

VERSION = "1.1"
PORT = 5678
_MARKER = Path(os.environ.get("APPDATA", "")) / "MT5Bridge" / ".installed"

_mt5_lock = threading.Lock()
_status = {"connected": False, "login": None, "server": None, "balance": None, "currency": None}
_last_sync = {"time": None, "count": 0}
_icon_ref = None


# ── First-run ─────────────────────────────────────────────────────────────────

def _is_first_run():
    return not _MARKER.exists()

def _mark_installed():
    _MARKER.parent.mkdir(parents=True, exist_ok=True)
    _MARKER.touch()


# ── Startup registry ──────────────────────────────────────────────────────────

def _startup_exe_path():
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'

def _startup_enabled():
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ,
        )
        try:
            winreg.QueryValueEx(key, "MT5Bridge")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False

def _set_startup(enable):
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        if enable:
            winreg.SetValueEx(key, "MT5Bridge", 0, winreg.REG_SZ, _startup_exe_path())
        else:
            winreg.DeleteValue(key, "MT5Bridge")
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"Startup toggle error: {e}")
        return False

def _toggle_startup(icon, _item):
    _set_startup(not _startup_enabled())
    icon.update_menu()


# ── MT5 helpers ───────────────────────────────────────────────────────────────

def _refresh_status():
    global _status
    try:
        with _mt5_lock:
            ok = mt5.initialize()
        if ok:
            info = mt5.account_info()
            with _mt5_lock:
                mt5.shutdown()
            if info:
                _status = {
                    "connected": True,
                    "login": info.login,
                    "server": info.server,
                    "balance": round(info.balance, 2),
                    "currency": info.currency,
                }
                return
        with _mt5_lock:
            mt5.shutdown()
    except Exception:
        pass
    _status = {"connected": False, "login": None, "server": None, "balance": None, "currency": None}

def _status_loop():
    while True:
        _refresh_status()
        if _icon_ref:
            try:
                _icon_ref.icon = _build_icon()
                _icon_ref.title = _tray_title()
            except Exception:
                pass
        time.sleep(20)

def _strip_suffix(symbol):
    return re.sub(r"\.[a-zA-Z]+\d*$", "", symbol).upper()

def _deals_to_trades(deals):
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

    result = []
    for pid, pos in positions.items():
        if not pos["opens"] or not pos["closes"]:
            continue
        open_d = pos["opens"][0]
        closes = pos["closes"]
        total_comm = sum(d.commission for d in pos["opens"]) + sum(d.commission for d in closes)
        total_swap = sum(d.swap for d in closes)
        total_profit = sum(d.profit for d in closes)
        pnl = round(total_profit + total_comm + total_swap, 2)
        last_close = max(closes, key=lambda d: d.time)
        result.append({
            "id": str(pid),
            "symbol": _strip_suffix(open_d.symbol),
            "direction": "BUY" if open_d.type == mt5.DEAL_TYPE_BUY else "SELL",
            "openDt": datetime.fromtimestamp(open_d.time).strftime("%Y-%m-%dT%H:%M"),
            "closeDt": datetime.fromtimestamp(last_close.time).strftime("%Y-%m-%dT%H:%M"),
            "openPx": round(open_d.price, 5),
            "closePx": round(last_close.price, 5),
            "volume": open_d.volume,
            "pnl": pnl,
            "commission": round(total_comm, 2),
            "swap": round(total_swap, 2),
            "tag": "",
            "notes": last_close.comment or "",
        })
    result.sort(key=lambda t: t["openDt"])
    return result


# ── Flask ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Access-Control-Request-Private-Network"
    )
    return response

@app.route("/status")
def route_status():
    _refresh_status()
    resp = dict(_status)
    resp["bridge"] = "ok"
    resp["version"] = VERSION
    if _last_sync["time"]:
        resp["lastSync"] = {"time": _last_sync["time"], "count": _last_sync["count"]}
    return jsonify(resp)

@app.route("/sync")
def route_sync():
    days = int(request.args.get("days", 365))
    with _mt5_lock:
        if not mt5.initialize():
            return jsonify({"error": "Cannot connect to MT5. Make sure the MT5 terminal is running."}), 503
        try:
            from_date = datetime.now() - timedelta(days=days)
            deals = mt5.history_deals_get(from_date, datetime.now())
        finally:
            mt5.shutdown()

    if deals is None:
        return jsonify({"trades": [], "count": 0})

    trades = _deals_to_trades(deals)
    _last_sync["time"] = datetime.now().strftime("%H:%M")
    _last_sync["count"] = len(trades)

    if _icon_ref:
        try:
            _icon_ref.update_menu()
        except Exception:
            pass

    return jsonify({"trades": trades, "count": len(trades)})


# ── Tray ──────────────────────────────────────────────────────────────────────

def _build_icon():
    connected = _status["connected"]
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = "#7B2FBE" if connected else "#64748b"
    d.ellipse([2, 2, size - 2, size - 2], fill=bg)
    w = "#ffffff"
    d.arc([13, 13, 51, 51], start=210, end=330, fill=w, width=5)
    d.polygon([(48, 27), (55, 21), (41, 18)], fill=w)
    d.arc([13, 13, 51, 51], start=30, end=150, fill=w, width=5)
    d.polygon([(16, 37), (9, 43), (23, 46)], fill=w)
    return img

def _tray_title():
    if _status["connected"]:
        bal = f"  {_status['balance']} {_status.get('currency','')}" if _status["balance"] else ""
        return f"MT5 Bridge v{VERSION}  •  #{_status['login']} @ {_status['server']}{bal}"
    return f"MT5 Bridge v{VERSION}  •  MT5 ไม่ได้เชื่อมต่อ"

def _build_menu():
    if _status["connected"]:
        line1 = f"#{_status['login']}  {_status['server']}"
        line2 = f"Balance: {_status['balance']} {_status.get('currency','')}" if _status["balance"] else None
    else:
        line1 = "MT5: ไม่ได้เชื่อมต่อ  (เปิด MT5 ก่อน)"
        line2 = None

    sync_line = (
        f"ซิงค์ล่าสุด {_last_sync['time']}  ({_last_sync['count']} รายการ)"
        if _last_sync["time"] else "ยังไม่ได้ซิงค์"
    )

    items = [
        pystray.MenuItem(f"MT5 Bridge  v{VERSION}  •  port {PORT}", None, enabled=False),
        pystray.MenuItem(line1, None, enabled=False),
    ]
    if line2:
        items.append(pystray.MenuItem(line2, None, enabled=False))
    items += [
        pystray.MenuItem(sync_line, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "เริ่มพร้อม Windows",
            _toggle_startup,
            checked=lambda _: _startup_enabled(),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("ออกจากโปรแกรม", lambda icon, _: (icon.stop(), sys.exit(0))),
    ]
    return pystray.Menu(*items)

def _run_server():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _icon_ref

    first_run = _is_first_run()

    # Auto-enable startup on first run
    if first_run and not _startup_enabled():
        _set_startup(True)
    if first_run:
        _mark_installed()

    threading.Thread(target=_run_server, daemon=True).start()
    threading.Thread(target=_status_loop, daemon=True).start()
    time.sleep(1)  # let status thread do first check

    icon = pystray.Icon(
        "MT5 Bridge",
        _build_icon(),
        _tray_title(),
        menu=_build_menu(),
    )
    _icon_ref = icon

    if first_run:
        def _notify_first_run():
            time.sleep(2)
            try:
                icon.notify(
                    "MT5 Bridge พร้อมใช้งาน!\nโปรแกรมจะเริ่มอัตโนมัติทุกครั้งที่เปิด Windows",
                    "MT5 Bridge",
                )
            except Exception:
                pass
        threading.Thread(target=_notify_first_run, daemon=True).start()

    icon.run()


if __name__ == "__main__":
    main()
