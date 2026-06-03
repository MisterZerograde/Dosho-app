'use strict';
const { app, BrowserWindow, Tray, Menu, nativeImage } = require('electron');
const path     = require('path');
const http     = require('http');
const { spawn } = require('child_process');
const treeKill  = require('tree-kill');

if (!app.requestSingleInstanceLock()) { app.quit(); process.exit(0); }
app.on('second-instance', () => { if (win) { win.show(); win.focus(); } });

const BRIDGE_URL = 'http://127.0.0.1:5678';
const POLL_MS    = 15_000;
const BRIDGE_EXE = app.isPackaged
  ? path.join(process.resourcesPath, 'mt5_bridge.exe')
  : path.join(__dirname, 'mt5_bridge', 'dist', 'mt5_bridge.exe');

let win          = null;
let tray         = null;
let bridgeProc   = null;
let isQuitting   = false;
let rendererReady = false;
let lastSyncAt   = null;
let statusCache  = {};

// ── HTTP helpers ──────────────────────────────────────────────────────────────
function httpGet(url, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { timeout: timeoutMs }, res => {
      let body = '';
      res.on('data', d => body += d);
      res.on('end', () => { try { resolve(JSON.parse(body)); } catch(e) { reject(e); } });
    });
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
    req.on('error', reject);
  });
}

function httpPost(url, data) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(data);
    const u = new URL(url);
    const req = http.request({
      hostname: u.hostname, port: Number(u.port) || 80, path: u.pathname,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      timeout: 5000,
    }, res => {
      let b = '';
      res.on('data', d => b += d);
      res.on('end', () => { try { resolve(JSON.parse(b)); } catch(e) { reject(e); } });
    });
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ── Bridge ────────────────────────────────────────────────────────────────────
async function startBridge() {
  try {
    await httpGet(`${BRIDGE_URL}/status`, 2000);
    return; // already running
  } catch {}

  const fs = require('fs');
  if (!fs.existsSync(BRIDGE_EXE)) {
    console.warn('[bridge] not found:', BRIDGE_EXE);
    return;
  }
  bridgeProc = spawn(BRIDGE_EXE, ['--headless'], {
    detached: false, windowsHide: true, stdio: 'ignore',
  });
  bridgeProc.on('error', e => console.error('[bridge]', e.message));
  bridgeProc.on('exit',  c => { bridgeProc = null; console.log('[bridge] exited', c); });
}

function stopBridge() {
  if (!bridgeProc?.pid) return;
  treeKill(bridgeProc.pid, 'SIGTERM', err => { if (err && bridgeProc) bridgeProc.kill(); });
  bridgeProc = null;
}

// ── Tray menu ─────────────────────────────────────────────────────────────────
const PERIODS   = [['7 วัน',7],['30 วัน',30],['90 วัน',90],['1 ปี',365],['ทั้งหมด',1095]];
const INTERVALS = [['ปิด',0],['1 นาที',60],['5 นาที',300],['15 นาที',900],['30 นาที',1800]];

function buildMenu() {
  const s  = statusCache;
  const pd = s.config?.period_days   ?? 30;
  const iv = s.config?.interval_secs ?? 0;
  const pLabel = PERIODS.find(([,d]) => d === pd)?.[0]   ?? '30 วัน';
  const iLabel = INTERVALS.find(([,v]) => v === iv)?.[0] ?? 'ปิด';

  const line1 = s.connected
    ? `#${s.login}  ${s.server}  ${s.balance ?? ''} ${s.currency ?? ''}`.trim()
    : 'MT5: ยังไม่ได้เชื่อมต่อ';
  const line2 = s.lastSync
    ? `ซิงค์ล่าสุด ${s.lastSync.time}  (${s.lastSync.count} รายการ)`
    : 'ยังไม่ได้ซิงค์';

  return Menu.buildFromTemplate([
    { label: 'Dosho — บันทึกการเทรด', enabled: false },
    { label: line1, enabled: false },
    { label: line2, enabled: false },
    { type: 'separator' },
    { label: 'เปิดหน้าต่าง Dosho', click: showWindow },
    { label: 'Sync MT5 ทันที',      click: () => doSync(false) },
    { type: 'separator' },
    {
      label: `ช่วงเวลา: ${pLabel}`,
      submenu: PERIODS.map(([lbl, days]) => ({
        label: lbl, type: 'radio', checked: pd === days,
        click: () => httpPost(`${BRIDGE_URL}/config`, { period_days: days })
                       .then(pollStatus).catch(() => {}),
      })),
    },
    {
      label: `Auto sync: ${iLabel}`,
      submenu: INTERVALS.map(([lbl, secs]) => ({
        label: lbl, type: 'radio', checked: iv === secs,
        click: () => httpPost(`${BRIDGE_URL}/config`, { interval_secs: secs })
                       .then(pollStatus).catch(() => {}),
      })),
    },
    { type: 'separator' },
    {
      label: 'เริ่มพร้อม Windows',
      type: 'checkbox',
      checked: app.getLoginItemSettings().openAtLogin,
      click: item => app.setLoginItemSettings({ openAtLogin: item.checked, openAsHidden: true }),
    },
    { type: 'separator' },
    { label: 'ออกจากโปรแกรม', click: () => { isQuitting = true; app.quit(); } },
  ]);
}

function rebuildMenu() {
  if (tray) tray.setContextMenu(buildMenu());
}

function iconPath(syncing = false) {
  return path.join(__dirname, 'assets', syncing ? 'tray-icon-syncing.png' : 'tray-icon.png');
}

// ── Window ────────────────────────────────────────────────────────────────────
function createWindow() {
  win = new BrowserWindow({
    width: 1320, height: 840, minWidth: 960, minHeight: 640,
    show: false,
    icon: path.join(__dirname, 'assets', 'icon.ico'),
    autoHideMenuBar: true,
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  win.loadFile('index.html');
  win.webContents.on('did-finish-load', () => {
    rendererReady = true;
    win.webContents.executeJavaScript('window.__ELECTRON__ = true;').catch(() => {});
  });
  win.on('close', e => { if (!isQuitting) { e.preventDefault(); win.hide(); } });
}

function showWindow() {
  if (!win) return;
  if (!win.isVisible()) win.show();
  win.focus();
}

// ── Sync ──────────────────────────────────────────────────────────────────────
async function doSync(silent) {
  if (!win || !rendererReady) return;
  if (tray) tray.setImage(nativeImage.createFromPath(iconPath(true)));
  try {
    const data = await httpGet(`${BRIDGE_URL}/sync`, 20000);
    const imported = data.trades || [];
    if (imported.length === 0 && silent) return;
    await win.webContents.executeJavaScript(
      `window._bgSyncTrades(${JSON.stringify(imported)})`
    );
    if (!silent) showWindow();
  } catch (e) {
    if (!silent) console.error('[sync]', e.message);
  } finally {
    if (tray) tray.setImage(nativeImage.createFromPath(iconPath(false)));
    rebuildMenu();
  }
}

// ── Status poll ───────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const data = await httpGet(`${BRIDGE_URL}/status`, 4000);
    statusCache = data;
    if (tray) tray.setToolTip(
      data.connected
        ? `Dosho  •  #${data.login} @ ${data.server}`
        : 'Dosho  •  MT5 ยังไม่ได้เชื่อมต่อ'
    );
    const interval = data.config?.interval_secs ?? 0;
    if (interval > 0 && data.lastSync?.time && data.lastSync.time !== lastSyncAt) {
      lastSyncAt = data.lastSync.time;
      doSync(true);
    }
  } catch {
    statusCache = {};
    if (tray) tray.setToolTip('Dosho  •  Bridge ออฟไลน์');
  }
  rebuildMenu();
}

// ── App lifecycle ─────────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  createWindow();
  await startBridge();

  tray = new Tray(nativeImage.createFromPath(iconPath(false)));
  tray.setToolTip('Dosho — บันทึกการเทรด');
  tray.setContextMenu(buildMenu());
  tray.on('double-click', showWindow);

  const { wasOpenedAsHidden } = app.getLoginItemSettings();
  if (!wasOpenedAsHidden) showWindow();

  setTimeout(pollStatus, 2500);
  setInterval(pollStatus, POLL_MS);
});

app.on('before-quit', () => { isQuitting = true; stopBridge(); });
app.on('window-all-closed', e => e.preventDefault());
