import type { ChildProcess, SpawnOptions } from 'node:child_process';

export interface StartTokenPlaceOptions {
  spawn?: (command: string, args?: readonly string[], options?: SpawnOptions) => ChildProcess;
  projectRoot?: string;
  pythonExecutable?: string;
  port?: number;
  env?: NodeJS.ProcessEnv;
  spawnOptions?: SpawnOptions;
}

export interface StartDspaceOptions {
  spawn?: (command: string, args?: readonly string[], options?: SpawnOptions) => ChildProcess;
  dspaceRoot?: string;
  port?: number;
  clientImportPath?: string;
  spawnOptions?: SpawnOptions;
  fsImpl?: typeof import('node:fs');
}

export const TOKEN_PLACE_PORT: number;
export const DSPACE_PORT: number;

export function startTokenPlace(options?: StartTokenPlaceOptions): Promise<ChildProcess>;
export function startDspace(options?: StartDspaceOptions): Promise<ChildProcess>;
export function cleanup(processes?: Array<ChildProcess | null | undefined>): Promise<void>;
export function buildTokenPlaceClientSource(port: number, clientImportPath: string): string;
