import { contextBridge, ipcRenderer } from 'electron';
import { LogMessage, UserSettings } from '../common/types';

contextBridge.exposeInMainWorld('api', {
  getSettings: (): Promise<UserSettings> => ipcRenderer.invoke('get-settings'),
  setSettings: (data: UserSettings): Promise<void> =>
    ipcRenderer.invoke('set-settings', data),
  onLog: (callback: (msg: LogMessage) => void): void => {
    ipcRenderer.on('log', (_, msg) => callback(msg));
  },
});
