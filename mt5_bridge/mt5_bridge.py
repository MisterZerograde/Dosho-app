import os
import sys
import re
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: pip install MetaTrader5"); sys.exit(1)

try:
    from flask import Flask, jsonify, request
    from flask_cors import CORS
except ImportError:
    print("ERROR: pip install flask flask-cors"); sys.exit(1)

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("ERROR: pip install pystray Pillow"); sys.exit(1)

VERSION = "2.0"
PORT    = 5678

CONFIG_PATH = Path(os.environ.get("APPDATA", "~")).expanduser() / "MT5Bridge" / "config.json"

PERIOD_OPTIONS   = [("7 วัน", 7), ("30 วัน", 30), ("90 วัน", 90), ("1 ปี", 365), ("ทั้งหมด", 1095)]
INTERVAL_OPTIONS = [("ปิด", 0), ("1 นาที", 60), ("5 นาที", 300), ("15 นาที", 900), ("30 นาที", 1800)]

_mt5_lock    = threading.Lock()
_cache_lock  = threading.Lock()
_icon_ref    = None

_config = {"period_days": 30, "interval_secs": 0}
_cache  = {"trades": [], "synced_at": None, "count": 0}
_status = {"connected": False, "login": None, "server": None, "balance": None, "currency": None}


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config():
    global _config
    try:
        _config = json.loads(CONFIG_PATH.read_text())
    except Exception:
        pass

def _save_config():
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(_config))
    except Exception:
        pass


# ── MT5 helpers ───────────────────────────────────────────────────────────────

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
        pnl        = round(sum(d.profit for d in closes) + total_comm + total_swap, 2)
        last_close = max(closes, key=lambda d: d.time)
        result.append({
            "id":        str(pid),
            "symbol":    _strip_suffix(open_d.symbol),
            "direction": "BUY" if open_d.type == mt5.DEAL_TYPE_BUY else "SELL",
            "openDt":    datetime.fromtimestamp(open_d.time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M"),
            "closeDt":   datetime.fromtimestamp(last_close.time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M"),
            "openPx":    round(open_d.price, 5),
            "closePx":   round(last_close.price, 5),
            "volume":    open_d.volume,
            "pnl":       pnl,
            "commission":round(total_comm, 2),
            "swap":      round(total_swap, 2),
            "tag":       "",
            "notes":     last_close.comment or "",
        })
    result.sort(key=lambda t: t["openDt"])
    return result

def _fetch_and_cache():
    global _status
    days = _config.get("period_days", 30)
    with _mt5_lock:
        if not mt5.initialize():
            mt5.shutdown()
            _status = {"connected": False, "login": None, "server": None, "balance": None, "currency": None}
            return False, "MT5 ไม่ได้เชื่อมต่อ"
        try:
            info  = mt5.account_info()
            deals = mt5.history_deals_get(datetime.now() - timedelta(days=days), datetime.now())
        finally:
            mt5.shutdown()

    if info:
        _status = {
            "connected": True,
            "login":     info.login,
            "server":    info.server,
            "balance":   round(info.balance, 2),
            "currency":  info.currency,
        }
    trades = _deals_to_trades(deals) if deals is not None else []

    with _cache_lock:
        _cache["trades"]    = trades
        _cache["count"]     = len(trades)
        _cache["synced_at"] = datetime.now().strftime("%H:%M")

    if _icon_ref:
        try:
            _icon_ref.icon  = _build_icon()
            _icon_ref.title = _tray_title()
            _icon_ref.update_menu()
        except Exception:
            pass
    return True, None


# ── Background loops ──────────────────────────────────────────────────────────

def _status_loop():
    while True:
        _fetch_and_cache()
        time.sleep(15)

def _auto_sync_loop():
    last = 0.0
    while True:
        interval = _config.get("interval_secs", 0)
        if interval > 0 and (time.time() - last) >= interval:
            _fetch_and_cache()
            last = time.time()
        time.sleep(10)


# ── Flask ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"]          = "*"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    response.headers["Access-Control-Allow-Methods"]         = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"]         = (
        "Content-Type, Access-Control-Request-Private-Network"
    )
    return response

@app.route("/status")
def route_status():
    resp = dict(_status)
    resp["bridge"]   = "ok"
    resp["version"]  = VERSION
    resp["startup"]  = _startup_enabled()
    resp["config"]  = {
        "period_days":   _config.get("period_days", 30),
        "interval_secs": _config.get("interval_secs", 0),
    }
    with _cache_lock:
        if _cache["synced_at"]:
            resp["lastSync"] = {"time": _cache["synced_at"], "count": _cache["count"]}
    return jsonify(resp)

@app.route("/config", methods=["POST", "OPTIONS"])
def route_set_config():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json(force=True, silent=True) or {}
    if "period_days"   in data: _config["period_days"]   = int(data["period_days"])
    if "interval_secs" in data: _config["interval_secs"] = int(data["interval_secs"])
    _save_config()
    return jsonify({"ok": True, "config": _config})

@app.route("/startup", methods=["POST", "OPTIONS"])
def route_startup():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json(force=True, silent=True) or {}
    _set_startup(bool(data.get("enable", False)))
    return jsonify({"ok": True, "startup": _startup_enabled()})

@app.route("/sync")
def route_sync():
    with _cache_lock:
        cached = _cache["synced_at"] is not None

    if not cached:
        ok, err = _fetch_and_cache()
        if not ok:
            return jsonify({"error": err}), 503

    with _cache_lock:
        return jsonify({"trades": _cache["trades"], "count": _cache["count"],
                        "synced_at": _cache["synced_at"]})


# ── Startup registry ──────────────────────────────────────────────────────────

def _startup_enabled():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, "MT5Bridge"); return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False

def _set_startup(enable):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enable:
            exe = (f'"{Path(sys.executable).resolve()}"' if getattr(sys, "frozen", False)
                   else f'"{sys.executable}" "{Path(__file__).resolve()}"')
            winreg.SetValueEx(key, "MT5Bridge", 0, winreg.REG_SZ, exe)
        else:
            winreg.DeleteValue(key, "MT5Bridge")
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Startup error: {e}")

def _toggle_startup(icon, _item):
    _set_startup(not _startup_enabled())
    icon.update_menu()


# ── Tray ──────────────────────────────────────────────────────────────────────

def _set_period(days):
    _config["period_days"] = days
    _save_config()
    threading.Thread(target=_fetch_and_cache, daemon=True).start()
    if _icon_ref:
        _icon_ref.update_menu()

def _set_interval(secs):
    _config["interval_secs"] = secs
    _save_config()
    if _icon_ref:
        _icon_ref.update_menu()

def _build_icon():
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)
    bg   = "#7B2FBE" if _status["connected"] else "#64748b"
    d.ellipse([2, 2, size-2, size-2], fill=bg)
    w = "#ffffff"
    d.arc([13, 13, 51, 51], start=210, end=330, fill=w, width=5)
    d.polygon([(48, 27), (55, 21), (41, 18)], fill=w)
    d.arc([13, 13, 51, 51], start=30,  end=150, fill=w, width=5)
    d.polygon([(16, 37), (9, 43), (23, 46)], fill=w)
    return img

def _tray_title():
    if _status["connected"]:
        bal = f"  {_status['balance']:,.0f} {_status.get('currency','')}" if _status["balance"] else ""
        return f"MT5 Bridge v{VERSION}  •  #{_status['login']} @ {_status['server']}{bal}"
    return f"MT5 Bridge v{VERSION}  •  MT5 ไม่ได้เชื่อมต่อ"

def _period_label():
    days = _config.get("period_days", 30)
    return next((lbl for lbl, d in PERIOD_OPTIONS if d == days), "30 วัน")

def _interval_label():
    secs = _config.get("interval_secs", 0)
    return next((lbl for lbl, s in INTERVAL_OPTIONS if s == secs), "ปิด")

def _build_menu():
    if _status["connected"]:
        line1 = f"#{_status['login']}  {_status['server']}"
        line2 = f"Balance: {_status['balance']:,.0f} {_status.get('currency','')}" if _status["balance"] else None
    else:
        line1 = "MT5: ไม่ได้เชื่อมต่อ"
        line2 = None

    with _cache_lock:
        sync_line = (f"ซิงค์ล่าสุด {_cache['synced_at']}  ({_cache['count']} รายการ)"
                     if _cache["synced_at"] else "ยังไม่ได้ซิงค์")

    items = [
        pystray.MenuItem(f"MT5 Bridge  v{VERSION}  •  port {PORT}", None, enabled=False),
        pystray.MenuItem(line1, None, enabled=False),
    ]
    if line2:
        items.append(pystray.MenuItem(line2, None, enabled=False))
    items.append(pystray.MenuItem(sync_line, None, enabled=False))
    items.append(pystray.Menu.SEPARATOR)

    # Period submenu
    items.append(pystray.MenuItem(
        f"ช่วงเวลา: {_period_label()}",
        pystray.Menu(*[
            pystray.MenuItem(
                lbl,
                lambda icon, item, d=days: (_set_period(d), icon.update_menu()),
                checked=lambda item, d=days: _config.get("period_days") == d,
                radio=True,
            )
            for lbl, days in PERIOD_OPTIONS
        ])
    ))

    # Auto-sync submenu
    items.append(pystray.MenuItem(
        f"Auto sync: {_interval_label()}",
        pystray.Menu(*[
            pystray.MenuItem(
                lbl,
                lambda icon, item, s=secs: (_set_interval(s), icon.update_menu()),
                checked=lambda item, s=secs: _config.get("interval_secs") == s,
                radio=True,
            )
            for lbl, secs in INTERVAL_OPTIONS
        ])
    ))

    items += [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("เริ่มพร้อม Windows", _toggle_startup,
                         checked=lambda _: _startup_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("ออกจากโปรแกรม",
                         lambda icon, _: (icon.stop(), sys.exit(0))),
    ]
    return pystray.Menu(*items)

def _run_server():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _icon_ref
    _load_config()

    threading.Thread(target=_run_server,       daemon=True).start()
    threading.Thread(target=_status_loop,      daemon=True).start()
    threading.Thread(target=_auto_sync_loop,   daemon=True).start()
    time.sleep(1)

    if '--headless' in sys.argv:
        while True:
            time.sleep(3600)
        return

    icon = pystray.Icon("MT5 Bridge", _build_icon(), _tray_title(), menu=_build_menu())
    _icon_ref = icon
    icon.run()

if __name__ == "__main__":
    main()
