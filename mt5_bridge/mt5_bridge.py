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

import tkinter as tk
from tkinter import ttk

VERSION = "2.0"
PORT    = 5678

CONFIG_PATH = Path(os.environ.get("APPDATA", "~")).expanduser() / "MT5Bridge" / "config.json"

PERIOD_OPTIONS   = [("7 วัน", 7), ("30 วัน", 30), ("90 วัน", 90), ("1 ปี", 365), ("ทั้งหมด", 1095)]
INTERVAL_OPTIONS = [("ปิด", 0), ("1 นาที", 60), ("5 นาที", 300), ("15 นาที", 900), ("30 นาที", 1800)]

_mt5_lock    = threading.Lock()
_cache_lock  = threading.Lock()
_icon_ref    = None
_win_ref     = None

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
    resp["config"]   = {
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

def _show_window():
    if _win_ref:
        _win_ref.after(0, _win_ref.show)

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
    items.append(pystray.MenuItem("เปิดหน้าต่าง", lambda icon, item: _show_window()))
    items.append(pystray.Menu.SEPARATOR)

    def _period_item(lbl, days):
        def action(icon, item):
            _set_period(days)
            icon.update_menu()
        def checker(item):
            return _config.get("period_days") == days
        return pystray.MenuItem(lbl, action, checked=checker, radio=True)

    def _interval_item(lbl, secs):
        def action(icon, item):
            _set_interval(secs)
            icon.update_menu()
        def checker(item):
            return _config.get("interval_secs") == secs
        return pystray.MenuItem(lbl, action, checked=checker, radio=True)

    items.append(pystray.MenuItem(
        f"ช่วงเวลา: {_period_label()}",
        pystray.Menu(*[_period_item(lbl, days) for lbl, days in PERIOD_OPTIONS])
    ))

    items.append(pystray.MenuItem(
        f"Auto sync: {_interval_label()}",
        pystray.Menu(*[_interval_item(lbl, secs) for lbl, secs in INTERVAL_OPTIONS])
    ))

    items += [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("เริ่มพร้อม Windows", _toggle_startup,
                         checked=lambda item: _startup_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("ออกจากโปรแกรม",
                         lambda icon, item: (icon.stop(), sys.exit(0))),
    ]
    return pystray.Menu(*items)

def _run_server():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


# ── Tkinter UI ────────────────────────────────────────────────────────────────

BG      = "#0f0f13"
BG2     = "#1a1a24"
BG3     = "#252535"
PURPLE  = "#7B2FBE"
TEXT    = "#e2e8f0"
TEXT2   = "#94a3b8"
TEXT3   = "#64748b"
GREEN   = "#22c55e"
YELLOW  = "#f59e0b"
RED     = "#ef4444"
FONT    = ("Segoe UI", 10)
FONT_SM = ("Segoe UI", 9)
FONT_LG = ("Segoe UI", 13, "bold")
FONT_XS = ("Segoe UI", 8)


class BridgeWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"MT5 Bridge  v{VERSION}")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.geometry("400x520")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        try:
            icon_img = _build_icon()
            tk_icon = self._pil_to_tk(icon_img, 32)
            self.root.iconphoto(True, tk_icon)
        except Exception:
            pass

        self._syncing = False
        self._build_ui()
        self._schedule_refresh()

    def _pil_to_tk(self, img, size):
        img = img.resize((size, size), Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.ANTIALIAS)
        from io import BytesIO
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return tk.PhotoImage(data=buf.read())

    def _card(self, parent, pady=(0, 10)):
        f = tk.Frame(parent, bg=BG2, bd=0, highlightthickness=1,
                     highlightbackground=BG3)
        f.pack(fill="x", padx=20, pady=pady)
        return f

    def _label(self, parent, text, font=None, fg=None, anchor="w", pady=0, padx=0):
        lbl = tk.Label(parent, text=text, bg=parent["bg"],
                       fg=fg or TEXT, font=font or FONT,
                       anchor=anchor)
        lbl.pack(fill="x", padx=padx, pady=pady)
        return lbl

    def _build_ui(self):
        # ── Header ──
        hdr = tk.Frame(self.root, bg=BG, height=56)
        hdr.pack(fill="x", padx=20, pady=(16, 8))
        hdr.pack_propagate(False)

        self.dot = tk.Canvas(hdr, width=10, height=10, bg=BG,
                             highlightthickness=0)
        self.dot.place(x=0, y=22)
        self.dot.create_oval(1, 1, 9, 9, fill=TEXT3, outline="")

        tk.Label(hdr, text="MT5 Bridge", bg=BG, fg=TEXT,
                 font=FONT_LG).place(x=18, y=14)
        tk.Label(hdr, text=f"v{VERSION}  •  port {PORT}", bg=BG,
                 fg=TEXT3, font=FONT_XS).place(x=20, y=36)

        # ── Status card ──
        sc = self._card(self.root, pady=(0, 8))
        inner = tk.Frame(sc, bg=BG2)
        inner.pack(fill="x", padx=14, pady=12)

        self.lbl_status  = tk.Label(inner, text="กำลังเชื่อมต่อ...", bg=BG2,
                                    fg=TEXT2, font=FONT, anchor="w")
        self.lbl_status.pack(fill="x")
        self.lbl_account = tk.Label(inner, text="", bg=BG2,
                                    fg=TEXT, font=("Segoe UI", 10, "bold"), anchor="w")
        self.lbl_account.pack(fill="x")
        self.lbl_balance = tk.Label(inner, text="", bg=BG2,
                                    fg=TEXT2, font=FONT_SM, anchor="w")
        self.lbl_balance.pack(fill="x")
        self.lbl_sync    = tk.Label(inner, text="", bg=BG2,
                                    fg=TEXT3, font=FONT_XS, anchor="w")
        self.lbl_sync.pack(fill="x", pady=(4, 0))

        # ── Controls card ──
        cc = self._card(self.root, pady=(0, 8))
        cc_inner = tk.Frame(cc, bg=BG2)
        cc_inner.pack(fill="x", padx=14, pady=12)

        self._row(cc_inner, "ช่วงเวลาดึงข้อมูล")
        self.cb_period = self._combo(cc_inner, [l for l, _ in PERIOD_OPTIONS],
                                     self._on_period)
        self._spacer(cc_inner)

        self._row(cc_inner, "Auto sync")
        self.cb_interval = self._combo(cc_inner, [l for l, _ in INTERVAL_OPTIONS],
                                       self._on_interval)
        self._spacer(cc_inner, h=10)

        self.startup_var = tk.BooleanVar()
        chk_frame = tk.Frame(cc_inner, bg=BG2)
        chk_frame.pack(fill="x")
        tk.Label(chk_frame, text="เริ่มพร้อม Windows", bg=BG2,
                 fg=TEXT, font=FONT, anchor="w", width=22).pack(side="left")
        chk = tk.Checkbutton(chk_frame, variable=self.startup_var,
                              bg=BG2, fg=TEXT, selectcolor=PURPLE,
                              activebackground=BG2, activeforeground=TEXT,
                              command=self._on_startup, bd=0,
                              highlightthickness=0, cursor="hand2")
        chk.pack(side="left")

        # ── Sync button ──
        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(fill="x", padx=20, pady=(4, 0))
        self.btn_sync = tk.Button(
            btn_frame, text="ซิงค์ตอนนี้",
            bg=PURPLE, fg="#ffffff", font=("Segoe UI", 10, "bold"),
            relief="flat", cursor="hand2", bd=0,
            padx=0, pady=10,
            activebackground="#6d28a8", activeforeground="#ffffff",
            command=self._do_sync
        )
        self.btn_sync.pack(fill="x")

        # ── Footer ──
        tk.Label(self.root, text="ปิดหน้าต่างเพื่อย่อลง Tray", bg=BG,
                 fg=TEXT3, font=FONT_XS).pack(pady=(10, 0))

    def _row(self, parent, text):
        tk.Label(parent, text=text, bg=BG2, fg=TEXT,
                 font=FONT, anchor="w").pack(fill="x")

    def _combo(self, parent, values, cmd):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TCombobox",
                        fieldbackground=BG3, background=BG3,
                        foreground=TEXT, arrowcolor=TEXT2,
                        bordercolor=BG3, lightcolor=BG3, darkcolor=BG3,
                        selectbackground=PURPLE, selectforeground="#fff",
                        padding=6)
        style.map("Dark.TCombobox",
                  fieldbackground=[("readonly", BG3)],
                  background=[("readonly", BG3)],
                  foreground=[("readonly", TEXT)])
        cb = ttk.Combobox(parent, values=values, state="readonly",
                          style="Dark.TCombobox", font=FONT)
        cb.pack(fill="x", pady=(2, 0))
        cb.bind("<<ComboboxSelected>>", cmd)
        return cb

    def _spacer(self, parent, h=6):
        tk.Frame(parent, bg=BG2, height=h).pack()

    def _on_period(self, _=None):
        idx = self.cb_period.current()
        if idx >= 0:
            _set_period(PERIOD_OPTIONS[idx][1])

    def _on_interval(self, _=None):
        idx = self.cb_interval.current()
        if idx >= 0:
            _set_interval(INTERVAL_OPTIONS[idx][1])

    def _on_startup(self):
        _set_startup(self.startup_var.get())

    def _do_sync(self):
        if self._syncing:
            return
        self._syncing = True
        self.btn_sync.config(text="กำลังซิงค์...", state="disabled")
        def run():
            _fetch_and_cache()
            self._syncing = False
            self.root.after(0, lambda: self.btn_sync.config(
                text="ซิงค์ตอนนี้", state="normal"))
            self.root.after(0, self._refresh_ui)
        threading.Thread(target=run, daemon=True).start()

    def _refresh_ui(self):
        connected = _status.get("connected", False)
        dot_color = GREEN if connected else RED

        self.dot.delete("all")
        self.dot.create_oval(1, 1, 9, 9, fill=dot_color, outline="")

        if connected:
            self.lbl_status.config(text="เชื่อมต่อแล้ว", fg=GREEN)
            login  = _status.get("login", "")
            server = _status.get("server", "")
            self.lbl_account.config(text=f"#{login}  {server}")
            bal = _status.get("balance")
            cur = _status.get("currency", "")
            self.lbl_balance.config(
                text=f"Balance: {bal:,.2f} {cur}" if bal is not None else "")
        else:
            self.lbl_status.config(text="MT5 ไม่ได้เชื่อมต่อ", fg=RED)
            self.lbl_account.config(text="")
            self.lbl_balance.config(text="")

        with _cache_lock:
            st = _cache["synced_at"]
            ct = _cache["count"]
        self.lbl_sync.config(
            text=f"ซิงค์ล่าสุด {st}  ({ct} รายการ)" if st else "ยังไม่ได้ซิงค์")

        period_days = _config.get("period_days", 30)
        for i, (_, d) in enumerate(PERIOD_OPTIONS):
            if d == period_days:
                self.cb_period.current(i)
                break

        interval_secs = _config.get("interval_secs", 0)
        for i, (_, s) in enumerate(INTERVAL_OPTIONS):
            if s == interval_secs:
                self.cb_interval.current(i)
                break

        self.startup_var.set(_startup_enabled())

    def _schedule_refresh(self):
        self._refresh_ui()
        self.root.after(5000, self._schedule_refresh)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.root.withdraw()

    def run(self):
        self.root.mainloop()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _icon_ref, _win_ref
    _load_config()

    threading.Thread(target=_run_server,       daemon=True).start()
    threading.Thread(target=_status_loop,      daemon=True).start()
    threading.Thread(target=_auto_sync_loop,   daemon=True).start()
    time.sleep(1)

    if '--headless' in sys.argv:
        while True:
            time.sleep(3600)
        return

    win = BridgeWindow()
    _win_ref = win

    def run_tray():
        global _icon_ref
        icon = pystray.Icon("MT5 Bridge", _build_icon(), _tray_title(), menu=_build_menu())
        _icon_ref = icon
        icon.run()

    threading.Thread(target=run_tray, daemon=True).start()

    win.run()

if __name__ == "__main__":
    main()
