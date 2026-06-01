# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

No build step. Open `index.html` directly in a browser. There are no dependencies to install, no server to start, and no transpilation — the file is self-contained.

## Architecture

The entire application lives in a single file: **`index.html`** (~2,600 lines). It is a vanilla JS SPA written in Thai (ภาษาไทย), structured as:

1. **`<style>` block** — all CSS (~420 lines). Uses CSS custom properties (`--bg`, `--purple`, `--green`, etc.) declared on `:root` for theming.
2. **`<body>` HTML** — static shell: sidebar nav, topbar, panel containers, modal overlays.
3. **`<script>` block** — all application logic. No modules, no frameworks, no bundler.

External CDN dependencies (loaded from the network):
- `chart.js@4.4.0` — used for the radar (Zella Score), cumulative equity, and reports charts.
- Google Fonts `Mitr` — the app font.

## Data layer

All data is persisted in `localStorage`. Keys are namespaced per account via `acctKey(base, id)`:
- `dosho_accounts` — array of account objects `{ id, name, balance, currency, strategy }`
- `dosho_active_account` — active account ID string
- `dosho_trades` / `dosho_trades_<id>` — array of trade objects
- `dosho_journal` / `dosho_journal_<id>` — `{ [date]: htmlString }` daily journal entries
- `dosho_checklist` / `dosho_checklist_<id>` — `{ entry: [], exit: [] }` strategy checklist items
- `dosho_settings` — global settings (name, balance, strategy, currency)
- `dosho_tags` / `dosho_tag_pool` — selected tags and the tag pool

The `default` account ID is special: its keys have no suffix (`dosho_trades`, not `dosho_trades_default`).

## Trade object shape

```js
{ id, symbol, openDt, type, pnl, volume, openPx, closePx, notes, tags, commission, swap }
```
`type` is `'BUY'` or `'SELL'`. `pnl` is net P&L after commission and swap.

## Key functions

| Function | Purpose |
|---|---|
| `init()` | App bootstrap — loads state, wires calendar nav, renders everything |
| `calcStats(ts)` | Derives all KPI metrics from a trade array |
| `renderDashboard()` | Updates KPI cards + Chart.js radar/equity charts |
| `renderCalendar()` | Builds the monthly P&L calendar grid |
| `renderTradeTable()` | Renders the filtered/searched trades list |
| `renderReports()` | Renders the reports panel charts and stat rows |
| `acctKey(base, id)` | Returns the namespaced localStorage key for the active account |
| `persist()` | Saves `trades` to localStorage |
| `switchPanel(id, el)` | Shows/hides top-level panels, updates sidebar active state |
| `handleCSVImport(e)` | Entry point for MT5 file import (dispatches to HTML or CSV parser) |
| `parseMT5HTML(text)` | Parses MT5 UTF-16 HTML reports into trade objects |
| `parseMT5CSV(text)` | Parses MT5 CSV exports into trade objects |
| `exportBackup()` | Downloads full JSON backup |
| `handleBackupImport(e)` | Restores from JSON backup |

## UI panels

Navigation is handled by `switchPanel()`. Panel IDs: `dashboard`, `trades`, `journal`, `reports`, `accounts`, `settings`.

The dashboard has two main layout zones:
- **KPI row** — 4 metric cards (Net P&L, Profit Factor, Win Rate, Avg RR)
- **`dash-grid`** — 2-column CSS grid: left = P&L calendar + cumulative chart; right = recent trades + stats summary

The dashboard also has a bottom tab bar (`switchBottomTab()`) with sub-panels (Open Positions, etc.).

## Rendering pattern

There is no virtual DOM or reactivity. After any state change, call the relevant `render*()` functions explicitly. The full refresh sequence used after import/restore is:

```js
renderDashboard(); renderCalendar(); renderTradeTable(); renderReports();
```

## MT5 import notes

MT5 HTML reports are UTF-16 LE encoded (BOM `FF FE`). `handleCSVImport` detects this and decodes with `TextDecoder('utf-16le')`. Duplicate detection compares `openDt`, `symbol`, and `pnl` (within ±0.01).
