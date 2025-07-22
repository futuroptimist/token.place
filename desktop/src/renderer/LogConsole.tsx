import React from 'react';
import { LogMessage } from '../common/types';

interface Props {
  logs: LogMessage[];
}

const LogConsole: React.FC<Props> = ({ logs }) => (
  <pre
    style={{ background: '#000', color: '#0f0', height: 200, overflow: 'auto' }}
  >
    {logs.map((l, i) => (
      <div key={i}>{l.data}</div>
    ))}
  </pre>
);

export default LogConsole;
