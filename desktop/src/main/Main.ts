import { app, BrowserWindow, ipcMain, Event } from 'electron';
import Store from 'electron-store';
import path from 'path';
import { AppTray } from './Tray';
import { IdleScheduler } from './IdleScheduler';
import { LogMessage, UserSettings } from '../common/types';

let mainWindow: BrowserWindow | null = null;
const store = new Store<UserSettings>({
  defaults: {
    idleMinutes: 10,
    gpuThreshold: 40,
    autoLaunch: false,
  },
});

const scheduler = new IdleScheduler(store.get('idleMinutes'));
const tray = new AppTray(scheduler, store);

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 600,
    height: 400,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
    },
    show: false,
  });

  mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'));

  mainWindow.on('close', (e: Event) => {
    e.preventDefault();
    mainWindow?.hide();
  });
}

app.whenReady().then(() => {
  createWindow();
  tray.init();
  scheduler.start();

  if (store.get('autoLaunch')) {
    app.setLoginItemSettings({ openAtLogin: true });
  }
});

scheduler.on('log', (msg: LogMessage) => {
  mainWindow?.webContents.send('log', msg);
});

scheduler.on('open-preferences', () => {
  mainWindow?.show();
});

ipcMain.handle('get-settings', () => store.store);
ipcMain.handle('set-settings', (_, data: UserSettings) => {
  store.set(data);
  scheduler.removeAllListeners();
});

app.on('window-all-closed', (e: Event) => {
  e.preventDefault();
});
