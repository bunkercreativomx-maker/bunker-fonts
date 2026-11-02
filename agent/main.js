// Bunker Fonts — agente de escritorio (Windows + Linux).
// Lee /api/manifest del servidor y ACTIVA (no instala) las fuentes marcadas en verde, como FontBase:
//  - Windows: AddFontResourceW desde una caché local (temporal; se reactivan al iniciar sesión).
//  - Linux:   copia a ~/.local/share/fonts/bunker-fonts + fc-cache (se quitan al apagarlas).
const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, shell, session, net } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const { execFile } = require('child_process');

const IS_WIN = process.platform === 'win32';
const DEFAULT_SERVER = 'http://100.124.180.112:8099';
const POLL_MS = 15000;

if (!app.requestSingleInstanceLock()) { app.quit(); process.exit(0); }

const DATA = app.getPath('userData');
const CFG_FILE = path.join(DATA, 'config.json');
const MANIFEST_FILE = path.join(DATA, 'manifest.json');
const CACHE = path.join(DATA, 'cache');
const LINUX_DIR = path.join(os.homedir(), '.local', 'share', 'fonts', 'bunker-fonts');

let cfg = loadJSON(CFG_FILE, { server: DEFAULT_SERVER, token: '' });
let tray = null, win = null, settingsWin = null;
let active = new Map();   // faceId -> local path (activado ahora)
let status = { active: 0, last: null, error: null, online: false };
let syncing = false, pending = false, quitting = false;

function loadJSON(f, d) { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return d; } }
function saveJSON(f, v) { fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f + '.tmp', JSON.stringify(v)); fs.renameSync(f + '.tmp', f); }
const base = () => cfg.server.replace(/\/+$/, '');

// ---------------- activación por sistema ----------------
let gdi = null;
function winApi() {
  if (gdi) return gdi;
  const koffi = require('koffi');
  const g = koffi.load('gdi32.dll'), u = koffi.load('user32.dll');
  gdi = {
    add: g.func('int __stdcall AddFontResourceW(str16 file)'),
    remove: g.func('int __stdcall RemoveFontResourceW(str16 file)'),
    post: u.func('int __stdcall PostMessageW(intptr hwnd, uint msg, uintptr wp, intptr lp)'),
  };
  return gdi;
}
function notifyFontChange() {
  if (IS_WIN) { try { winApi().post(0xffff, 0x001d, 0, 0); } catch (e) { console.error(e); } }
  else execFile('fc-cache', ['-f', LINUX_DIR], () => {});
}
function activateFile(id, file) {
  if (IS_WIN) {
    const n = winApi().add(file);
    if (!n) throw new Error('Windows no aceptó ' + path.basename(file));
    return file;
  }
  fs.mkdirSync(LINUX_DIR, { recursive: true });
  const dest = path.join(LINUX_DIR, id + '-' + path.basename(file));
  if (!fs.existsSync(dest)) fs.copyFileSync(file, dest);
  return dest;
}
function deactivateFile(p) {
  if (IS_WIN) { try { winApi().remove(p); } catch {} }
  else { try { fs.unlinkSync(p); } catch {} }
}

// ---------------- red ----------------
async function fetchJSON(url) {
  const r = await net.fetch(url, { headers: cfg.token ? { Authorization: 'Bearer ' + cfg.token } : {}, cache: 'no-store' });
  if (r.status === 401) throw new Error('Contraseña requerida o incorrecta (Configuración)');
  if (!r.ok) throw new Error('Servidor respondió ' + r.status);
  return r.json();
}
async function download(f) {
  const dir = path.join(CACHE, f.hash || f.id);
  const dest = path.join(dir, f.file);
  if (fs.existsSync(dest) && fs.statSync(dest).size > 0) return dest;
  const r = await net.fetch(base() + f.url, { headers: cfg.token ? { Authorization: 'Bearer ' + cfg.token } : {} });
  if (!r.ok) throw new Error('No se pudo bajar ' + f.file + ' (' + r.status + ')');
  const buf = Buffer.from(await r.arrayBuffer());
  if (!buf.length) throw new Error('Archivo vacío: ' + f.file);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(dest + '.part', buf); fs.renameSync(dest + '.part', dest);
  return dest;
}

// ---------------- sincronización ----------------
async function sync() {
  if (syncing) { pending = true; return; }
  syncing = true;
  try {
    let manifest;
    try {
      manifest = await fetchJSON(base() + '/api/manifest');
      saveJSON(MANIFEST_FILE, manifest);
      status.online = true; status.error = null;
    } catch (e) {
      status.online = false; status.error = e.message;
      manifest = loadJSON(MANIFEST_FILE, null);   // sin red: usa la última lista y la caché
      if (!manifest) return;
    }
    const want = new Map(manifest.fonts.map(f => [f.id, f]));
    let changed = false, errors = [];
    for (const [id, p] of active) {
      if (!want.has(id)) { deactivateFile(p); active.delete(id); changed = true; }
    }
    for (const [id, f] of want) {
      if (active.has(id)) continue;
      try {
        const file = await download(f);
        active.set(id, activateFile(id, file)); changed = true;
      } catch (e) { errors.push(e.message); }
    }
    if (!IS_WIN) cleanLinux(want);
    if (changed) notifyFontChange();
    if (errors.length && status.online) status.error = errors[0] + (errors.length > 1 ? ` (+${errors.length - 1})` : '');
    status.active = active.size; status.last = new Date();
  } finally {
    syncing = false; updateTray();
    if (pending) { pending = false; setTimeout(sync, 200); }
  }
}
function cleanLinux(want) {
  // quita restos de sesiones anteriores que ya no están activos
  try {
    for (const n of fs.readdirSync(LINUX_DIR)) {
      const id = n.split('-')[0];
      if (!want.has(id)) { fs.unlinkSync(path.join(LINUX_DIR, n)); active.delete(id); }
    }
  } catch {}
}
function deactivateAll() {
  if (!IS_WIN) return;            // en Linux se quedan hasta que las apagues (no hay activación temporal)
  for (const p of active.values()) deactivateFile(p);
  active.clear(); notifyFontChange();
}

// ---------------- ventanas ----------------
async function setAuthCookie() {
  if (!cfg.token) return;
  try { await session.defaultSession.cookies.set({ url: base(), name: 'bf_auth', value: cfg.token, expirationDate: Date.now() / 1000 + 3600 * 24 * 365 }); } catch {}
}
async function openMain() {
  if (win) { win.show(); win.focus(); return; }
  win = new BrowserWindow({
    width: 1280, height: 820, title: 'Bunker Fonts', icon: iconPath(), autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js') },
  });
  await setAuthCookie();
  win.loadURL(base() + '/').catch(() => { win.loadFile(path.join(__dirname, 'offline.html')); });
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  win.webContents.on('will-navigate', (e, url) => { if (!url.startsWith(base())) { e.preventDefault(); } });
  win.webContents.session.on('will-download', () => {});
  win.on('close', e => { if (!quitting) { e.preventDefault(); win.hide(); } });
  win.on('closed', () => { win = null; });
}
function openSettings() {
  if (settingsWin) { settingsWin.focus(); return; }
  settingsWin = new BrowserWindow({
    width: 460, height: 430, resizable: false, title: 'Configuración — Bunker Fonts', icon: iconPath(), autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js') },
  });
  settingsWin.loadFile(path.join(__dirname, 'settings.html'));
  settingsWin.on('closed', () => { settingsWin = null; });
}

// ---------------- bandeja ----------------
function iconPath() { return path.join(__dirname, 'build', 'icon.png'); }
function trayImage() {
  const img = nativeImage.createFromPath(iconPath());
  return img.resize({ width: IS_WIN ? 16 : 22, height: IS_WIN ? 16 : 22 });
}
function autoStartOn() {
  if (IS_WIN) return app.getLoginItemSettings().openAtLogin;
  return fs.existsSync(linuxAutostartFile());
}
function linuxAutostartFile() { return path.join(os.homedir(), '.config', 'autostart', 'bunker-fonts.desktop'); }
function setAutoStart(on) {
  if (IS_WIN) { app.setLoginItemSettings({ openAtLogin: on, args: ['--hidden'] }); return; }
  const f = linuxAutostartFile();
  if (!on) { try { fs.unlinkSync(f); } catch {} return; }
  const exe = process.env.APPIMAGE || process.execPath;
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, `[Desktop Entry]\nType=Application\nName=Bunker Fonts\nExec="${exe}" --hidden\nX-GNOME-Autostart-enabled=true\n`);
}
function updateTray() {
  if (!tray) return;
  const st = status.online ? `● ${status.active} fuentes activas` : (status.error ? `⚠ ${status.error}` : 'Conectando…');
  tray.setToolTip('Bunker Fonts — ' + st);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Abrir Bunker Fonts', click: openMain },
    { label: st, enabled: false },
    { label: 'Sincronizar ahora', click: () => sync() },
    { type: 'separator' },
    { label: 'Configuración…', click: openSettings },
    { label: 'Iniciar con la computadora', type: 'checkbox', checked: autoStartOn(), click: m => setAutoStart(m.checked) },
    { type: 'separator' },
    { label: IS_WIN ? 'Salir (desactiva las fuentes)' : 'Salir', click: () => { quitting = true; app.quit(); } },
  ]));
}

// ---------------- IPC ----------------
ipcMain.handle('bf:sync', () => { sync(); return true; });
ipcMain.handle('bf:status', () => ({ ...status, server: base() }));
ipcMain.handle('bf:getConfig', () => ({ server: cfg.server, hasToken: !!cfg.token, version: app.getVersion() }));
ipcMain.handle('bf:saveConfig', async (_e, { server, password }) => {
  server = (server || '').trim().replace(/\/+$/, '') || DEFAULT_SERVER;
  if (!/^https?:\/\//.test(server)) server = 'https://' + server;
  let token = server === cfg.server ? cfg.token : '';
  if (password) {
    const r = await net.fetch(server + '/api/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: j.detail || ('Error ' + r.status) };
    token = j.token;
  }
  cfg = { server, token }; saveJSON(CFG_FILE, cfg);
  await setAuthCookie();
  active.forEach(p => deactivateFile(p)); active.clear();
  await sync();
  if (win) win.loadURL(base() + '/').catch(() => win.loadFile(path.join(__dirname, 'offline.html')));
  return { ok: status.online, error: status.error };
});

// ---------------- arranque ----------------
app.on('second-instance', () => openMain());
app.on('window-all-closed', e => { /* sigue en la bandeja */ });
app.on('before-quit', () => { quitting = true; deactivateAll(); });

app.whenReady().then(() => {
  if (IS_WIN) app.setAppUserModelId('mx.bunkercreativo.fonts');
  if (!fs.existsSync(CFG_FILE)) { saveJSON(CFG_FILE, cfg); setAutoStart(true); }
  tray = new Tray(trayImage());
  tray.on('click', openMain);
  updateTray();
  sync();
  setInterval(sync, POLL_MS);
  if (!process.argv.includes('--hidden')) openMain();
  try {
    const { autoUpdater } = require('electron-updater');
    autoUpdater.checkForUpdatesAndNotify().catch(() => {});
    setInterval(() => autoUpdater.checkForUpdatesAndNotify().catch(() => {}), 6 * 3600 * 1000);
  } catch {}
});
