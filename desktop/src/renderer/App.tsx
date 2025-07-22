import React, { useEffect, useState } from 'react';
import PreferencesWindow from './PreferencesWindow';
import LogConsole from './LogConsole';
import { LogMessage, UserSettings } from '../common/types';

const App: React.FC = () => {
  const [settings, setSettings] = useState<UserSettings>();
  const [logs, setLogs] = useState<LogMessage[]>([]);

  useEffect(() => {
    window.api.getSettings().then(setSettings);
    window.api.onLog((msg) => {
      setLogs((prev) => [...prev.slice(-499), msg]);
    });
  }, []);

  const handleSave = (data: UserSettings): void => {
    window.api.setSettings(data).then(() => setSettings(data));
  };

  return (
    <div>
      {settings && (
        <PreferencesWindow settings={settings} onSave={handleSave} />
      )}
      <LogConsole logs={logs} />
    </div>
  );
};

export default App;
