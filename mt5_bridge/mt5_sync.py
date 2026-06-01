import sys
import re
import json
import threading
import time
import os
from datetime import datetime, timedelta
from pathlib import Path

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: pip install MetaTrader5"); sys.exit(1)

try:
    import customtkinter as ctk
except ImportError:
    print("ERROR: pip install customtkinter"); sys.exit(1)

VERSION = "1.2"
CONFIG_PATH = Path(os.environ.get("APPDATA", "~")).expanduser() / "MT5Sync" / "config.json"

PERIOD_LABELS = ["7 วัน", "30 วัน", "90 วัน", "1 ปี", "ทั้งหมด"]
PERIOD_DAYS   = {"7 วัน": 7, "30 วัน": 30, "90 วัน": 90, "1 ปี": 365, "ทั้งหมด": 1095}

INTERVAL_LABELS = ["ปิด", "1 นาที", "5 นาที", "15 นาที", "30 นาที"]
INTERVAL_SECS   = {"ปิด": 0, "1 นาที": 60, "5 นาที": 300, "15 นาที": 900, "30 นาที": 1800}

_mt5_lock = threading.Lock()


# ── MT5 helpers ───────────────────────────────────────────────────────────────

def _strip_suffix(symbol):
    return re.sub(r"\.[a-zA-Z]+\d*$", "", symbol).upper()


def _get_mt5_info():
    with _mt5_lock:
        if not mt5.initialize():
            mt5.shutdown()
            return None
        info = mt5.account_info()
        mt5.shutdown()
    if not info:
        return None
    return {
        "login": info.login,
        "server": info.server,
        "balance": round(info.balance, 2),
        "currency": info.currency,
    }


def _fetch_trades(days):
    with _mt5_lock:
        if not mt5.initialize():
            mt5.shutdown()
            return None, "MT5 ไม่ได้เชื่อมต่อ — เปิด MT5 Terminal และล็อกอินก่อน"
        try:
            from_dt = datetime.now() - timedelta(days=days)
            deals = mt5.history_deals_get(from_dt, datetime.now())
        finally:
            mt5.shutdown()

    if deals is None:
        return [], None

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
        closes  = pos["closes"]
        total_comm  = sum(d.commission for d in pos["opens"]) + sum(d.commission for d in closes)
        total_swap  = sum(d.swap for d in closes)
        pnl         = round(sum(d.profit for d in closes) + total_comm + total_swap, 2)
        last_close  = max(closes, key=lambda d: d.time)
        trades.append({
            "id":        str(pid),
            "symbol":    _strip_suffix(open_d.symbol),
            "direction": "BUY" if open_d.type == mt5.DEAL_TYPE_BUY else "SELL",
            "openDt":    datetime.fromtimestamp(open_d.time).strftime("%Y-%m-%dT%H:%M"),
            "closeDt":   datetime.fromtimestamp(last_close.time).strftime("%Y-%m-%dT%H:%M"),
            "openPx":    round(open_d.price, 5),
            "closePx":   round(last_close.price, 5),
            "volume":    open_d.volume,
            "pnl":       pnl,
            "commission":round(total_comm, 2),
            "swap":      round(total_swap, 2),
            "tag":       "",
            "notes":     last_close.comment or "",
        })
    trades.sort(key=lambda t: t["openDt"])
    return trades, None


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}

def _save_config(period_label, interval_label):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({
            "period_label": period_label,
            "interval_label": interval_label,
        }))
    except Exception:
        pass


# ── App ───────────────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("MT5 Sync")
        self.geometry("300x370")
        self.resizable(False, False)
        self.configure(fg_color="#0f0f1a")

        # Centre window
        self.update_idletasks()
        sx, sy = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"300x370+{(sx-300)//2}+{(sy-370)//2}")

        cfg = _load_config()
        self._auto_id   = None
        self._remaining = 0
        self._syncing   = False

        self._build_ui(cfg)
        threading.Thread(target=self._status_loop, daemon=True).start()

        # Restore auto-sync
        if INTERVAL_SECS.get(self.iv_var.get(), 0) > 0:
            self.after(1500, self._restart_auto)

    # ── UI build ─────────────────────────────────────────────────────────────

    def _build_ui(self, cfg):
        # Header bar
        hdr = ctk.CTkFrame(self, fg_color="#1a1a2e", corner_radius=0, height=46)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="MT5 Sync", font=ctk.CTkFont(size=15, weight="bold"),
                     text_color="#e2e8f0").place(x=14, rely=0.5, anchor="w")
        ctk.CTkLabel(hdr, text=f"v{VERSION}", font=ctk.CTkFont(size=10),
                     text_color="#475569").place(relx=1, x=-12, rely=0.5, anchor="e")

        # MT5 status card
        card = ctk.CTkFrame(self, fg_color="#1a1a2e", corner_radius=10)
        card.pack(fill="x", padx=14, pady=(14, 0))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=10)

        self.dot = ctk.CTkLabel(row, text="●", font=ctk.CTkFont(size=13),
                                text_color="#475569", width=18)
        self.dot.pack(side="left")

        col = ctk.CTkFrame(row, fg_color="transparent")
        col.pack(side="left", padx=8, fill="x", expand=True)
        self.mt5_main = ctk.CTkLabel(col, text="กำลังตรวจสอบ...",
                                     font=ctk.CTkFont(size=12, weight="bold"),
                                     text_color="#94a3b8", anchor="w")
        self.mt5_main.pack(fill="x")
        self.mt5_sub = ctk.CTkLabel(col, text="",
                                    font=ctk.CTkFont(size=10),
                                    text_color="#475569", anchor="w")
        self.mt5_sub.pack(fill="x")

        # Settings
        sf = ctk.CTkFrame(self, fg_color="transparent")
        sf.pack(fill="x", padx=14, pady=(12, 0))
        sf.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(sf, text="ช่วงเวลา", font=ctk.CTkFont(size=12),
                     text_color="#94a3b8").grid(row=0, column=0, sticky="w", pady=5)
        self.p_var = ctk.StringVar(value=cfg.get("period_label", "30 วัน"))
        ctk.CTkOptionMenu(sf, variable=self.p_var, values=PERIOD_LABELS, width=145,
                          fg_color="#1a1a2e", button_color="#2d2d4e",
                          button_hover_color="#3d3d6e",
                          command=lambda _: _save_config(self.p_var.get(), self.iv_var.get())
                          ).grid(row=0, column=1, sticky="e", pady=5)

        ctk.CTkLabel(sf, text="Auto sync", font=ctk.CTkFont(size=12),
                     text_color="#94a3b8").grid(row=1, column=0, sticky="w", pady=5)
        self.iv_var = ctk.StringVar(value=cfg.get("interval_label", "ปิด"))
        ctk.CTkOptionMenu(sf, variable=self.iv_var, values=INTERVAL_LABELS, width=145,
                          fg_color="#1a1a2e", button_color="#2d2d4e",
                          button_hover_color="#3d3d6e",
                          command=self._on_interval_change
                          ).grid(row=1, column=1, sticky="e", pady=5)

        # Sync button
        self.btn = ctk.CTkButton(
            self, text="Sync & Copy", font=ctk.CTkFont(size=14, weight="bold"),
            height=44, corner_radius=10,
            fg_color="#7B2FBE", hover_color="#6d28d9",
            command=self._on_sync,
        )
        self.btn.pack(fill="x", padx=14, pady=(14, 0))

        # Countdown + result
        self.countdown = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=10),
                                      text_color="#475569")
        self.countdown.pack(pady=(5, 0))

        self.result = ctk.CTkLabel(
            self,
            text='กด Sync & Copy แล้วไปกด "Paste MT5" ใน Dosho',
            font=ctk.CTkFont(size=11), text_color="#64748b",
            wraplength=268, justify="center",
        )
        self.result.pack(pady=(6, 14), padx=14)

    # ── Status loop ───────────────────────────────────────────────────────────

    def _status_loop(self):
        while True:
            info = _get_mt5_info()
            self.after(0, self._apply_status, info)
            time.sleep(12)

    def _apply_status(self, info):
        if info:
            self.dot.configure(text_color="#22c55e")
            self.mt5_main.configure(text="MT5: เชื่อมต่อแล้ว", text_color="#e2e8f0")
            bal = f"  •  {info['balance']:,.0f} {info['currency']}" if info.get("balance") else ""
            self.mt5_sub.configure(text=f"#{info['login']}  {info['server']}{bal}")
        else:
            self.dot.configure(text_color="#ef4444")
            self.mt5_main.configure(text="MT5: ไม่ได้เชื่อมต่อ", text_color="#94a3b8")
            self.mt5_sub.configure(text="เปิด MT5 Terminal และล็อกอิน")

    # ── Sync logic ────────────────────────────────────────────────────────────

    def _on_sync(self):
        if self._syncing:
            return
        self._syncing = True
        self.btn.configure(state="disabled", text="กำลังซิงค์...")
        self.result.configure(text="กำลังดึงข้อมูลจาก MT5...", text_color="#94a3b8")
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _do_sync(self):
        days = PERIOD_DAYS.get(self.p_var.get(), 30)
        trades, err = _fetch_trades(days)
        self.after(0, self._sync_done, trades, err)

    def _sync_done(self, trades, err):
        self._syncing = False
        self.btn.configure(state="normal", text="Sync & Copy")
        if err:
            self.result.configure(text=f"✗  {err}", text_color="#ef4444")
            return

        payload = json.dumps({
            "source":      "mt5_sync",
            "version":     VERSION,
            "period_days": PERIOD_DAYS.get(self.p_var.get(), 30),
            "synced_at":   datetime.now().isoformat(timespec="minutes"),
            "trades":      trades,
        }, ensure_ascii=False)

        self.clipboard_clear()
        self.clipboard_append(payload)
        self.update()

        now = datetime.now().strftime("%H:%M")
        self.result.configure(
            text=f'✓  คัดลอก {len(trades)} รายการแล้ว  ({now})\nเปิด Dosho แล้วกด "Paste MT5"',
            text_color="#22c55e",
        )

    # ── Auto-sync ─────────────────────────────────────────────────────────────

    def _on_interval_change(self, _val):
        _save_config(self.p_var.get(), self.iv_var.get())
        self._restart_auto()

    def _restart_auto(self):
        if self._auto_id:
            self.after_cancel(self._auto_id)
            self._auto_id = None
        secs = INTERVAL_SECS.get(self.iv_var.get(), 0)
        if secs > 0:
            self._remaining = secs
            self._tick()
        else:
            self.countdown.configure(text="")

    def _tick(self):
        if self._remaining <= 0:
            self._on_sync()
            secs = INTERVAL_SECS.get(self.iv_var.get(), 0)
            if secs > 0:
                self._remaining = secs
                self._auto_id = self.after(1000, self._tick)
        else:
            m, s = divmod(self._remaining, 60)
            self.countdown.configure(
                text=f"Auto sync ใน {'%d:%02d น.' % (m,s) if m else '%d วิ' % s}"
            )
            self._remaining -= 1
            self._auto_id = self.after(1000, self._tick)


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    App().mainloop()
