import React, { useState } from 'react';
import { UserSettings } from '../common/types';

interface Props {
  settings: UserSettings;
  onSave: (data: UserSettings) => void;
}

const PreferencesWindow: React.FC<Props> = ({ settings, onSave }) => {
  const [idleMinutes, setIdleMinutes] = useState(settings.idleMinutes);
  const [gpuThreshold, setGpuThreshold] = useState(settings.gpuThreshold);
  const [autoLaunch, setAutoLaunch] = useState(settings.autoLaunch);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSave({ idleMinutes, gpuThreshold, autoLaunch });
  };

  return (
    <form onSubmit={handleSubmit}>
      <h2>Preferences</h2>
      <label>
        Idle Minutes
        <input
          type="number"
          value={idleMinutes}
          onChange={(e) => setIdleMinutes(Number(e.target.value))}
        />
      </label>
      <br />
      <label>
        GPU Threshold
        <input
          type="number"
          value={gpuThreshold}
          onChange={(e) => setGpuThreshold(Number(e.target.value))}
        />
      </label>
      <br />
      <label>
        Auto Launch
        <input
          type="checkbox"
          checked={autoLaunch}
          onChange={(e) => setAutoLaunch(e.target.checked)}
        />
      </label>
      <br />
      <button type="submit">Save</button>
    </form>
  );
};

export default PreferencesWindow;
