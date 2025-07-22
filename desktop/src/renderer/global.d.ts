interface Window {
  api: {
    getSettings(): Promise<import('../common/types').UserSettings>;
    setSettings(data: import('../common/types').UserSettings): Promise<void>;
    onLog(cb: (msg: import('../common/types').LogMessage) => void): void;
  };
}
