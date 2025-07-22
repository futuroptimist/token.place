export interface UserSettings {
  idleMinutes: number;
  gpuThreshold: number;
  autoLaunch: boolean;
}

export interface LogMessage {
  type: 'stdout' | 'stderr';
  data: string;
}
