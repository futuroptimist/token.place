import { ChildProcessWithoutNullStreams, spawn } from 'child_process';
import { EventEmitter } from 'events';
import { powerMonitor } from 'electron';
import { LogMessage } from '../common/types';

export class IdleScheduler extends EventEmitter {
  private proc: ChildProcessWithoutNullStreams | null = null;
  private interval: NodeJS.Timeout | null = null;

  constructor(private idleMinutes: number) {
    super();
  }

  start(): void {
    this.interval = setInterval(() => {
      const state = powerMonitor.getSystemIdleState(this.idleMinutes * 60);
      if (state === 'idle' && !this.proc) {
        this.startProcess();
      } else if (state !== 'idle' && this.proc) {
        this.stopProcess();
      }
    }, 15000);
  }

  private startProcess(): void {
    this.proc = spawn('python', ['./server.py', '--port', '7600'], {
      stdio: 'pipe',
    });
    this.proc.stdout.on('data', (data: Buffer) => {
      this.emit('log', { type: 'stdout', data: data.toString() } as LogMessage);
    });
    this.proc.stderr.on('data', (data: Buffer) => {
      this.emit('log', { type: 'stderr', data: data.toString() } as LogMessage);
    });
    this.proc.on('close', () => {
      this.proc = null;
    });
  }

  private stopProcess(): void {
    if (!this.proc) return;
    this.proc.kill('SIGINT');
    setTimeout(() => {
      if (this.proc) {
        this.proc.kill('SIGKILL');
      }
    }, 5000);
  }
}
