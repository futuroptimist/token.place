import { Menu, Tray, nativeImage } from 'electron';
import Store from 'electron-store';
import path from 'path';
import { IdleScheduler } from './IdleScheduler';

export class AppTray {
  private tray: Tray | null = null;
  constructor(
    private scheduler: IdleScheduler,
    private store: Store,
  ) {}

  init(): void {
    const icon = nativeImage.createFromPath(
      path.join(__dirname, '..', '..', 'assets', 'icon.png'),
    );
    this.tray = new Tray(icon);
    const contextMenu = Menu.buildFromTemplate([
      {
        label: 'Start Inference Now',
        click: () => this.scheduler.emit('force-start'),
      },
      {
        label: 'Pause Inference',
        click: () => this.scheduler.emit('force-stop'),
      },
      { type: 'separator' },
      {
        label: 'Preferences…',
        click: () => {
          this.scheduler.emit('open-preferences');
        },
      },
      {
        label: 'Quit',
        click: () => {
          this.tray?.destroy();
          process.exit(0);
        },
      },
    ]);
    this.tray.setContextMenu(contextMenu);
    this.tray.on('click', () => {
      this.scheduler.emit('open-preferences');
    });
  }
}
