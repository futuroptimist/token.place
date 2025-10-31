const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

const DEFAULT_TOKEN_PLACE_PORT = 5555;
const DEFAULT_DSPACE_PORT = 4444;

const integrationRoot = process.env.TOKEN_PLACE_INTEGRATION_ROOT || __dirname;

const backupRecords = new Map();

async function isPortAvailable(port) {
  return await new Promise(resolve => {
    const server = net.createServer();

    server.once('error', () => {
      resolve(false);
    });

    server.listen(port, '127.0.0.1', () => {
      server.close(() => resolve(true));
    });
  });
}

async function findAvailablePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();

    server.once('error', reject);

    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : undefined;

      server.close(error => {
        if (error) {
          reject(error);
          return;
        }

        if (typeof port !== 'number') {
          reject(new Error('Unable to determine available port'));
          return;
        }

        resolve(port);
      });
    });
  });
}

function normalizePort(value, label) {
  const port = Number(value);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`${label} must be an integer between 1 and 65535`);
  }
  return port;
}

async function resolvePort({ explicitPort, envKey, defaultPort }) {
  if (explicitPort !== undefined && explicitPort !== null) {
    const port = normalizePort(explicitPort, 'Explicit port');
    if (!(await isPortAvailable(port))) {
      throw new Error(`Requested port ${port} is already in use`);
    }
    return port;
  }

  const envValue = process.env[envKey];
  if (envValue) {
    const port = normalizePort(envValue, `${envKey} value`);
    if (!(await isPortAvailable(port))) {
      throw new Error(`Port ${port} specified by ${envKey} is not available`);
    }
    return port;
  }

  if (await isPortAvailable(defaultPort)) {
    return defaultPort;
  }

  return await findAvailablePort();
}

function waitForSpawn(child) {
  return new Promise((resolve, reject) => {
    const handleError = error => {
      child.removeListener('spawn', handleSpawn);
      reject(error);
    };

    const handleSpawn = () => {
      child.removeListener('error', handleError);
      resolve(child);
    };

    child.once('error', handleError);
    child.once('spawn', handleSpawn);
  });
}

async function startTokenPlace(options = {}) {
  const {
    spawn: spawnImpl = spawn,
    projectRoot = path.join(integrationRoot, 'token.place'),
    pythonExecutable = 'python',
    port,
    env: envOverrides = {},
    spawnOptions = {},
  } = options;

  const resolvedPort = await resolvePort({
    explicitPort: port,
    envKey: 'TOKEN_PLACE_PORT',
    defaultPort: DEFAULT_TOKEN_PLACE_PORT,
  });

  const args = ['server.py', `--port=${resolvedPort}`];
  const env = {
    ...process.env,
    ...envOverrides,
    USE_MOCK_LLM: '1',
  };

  const child = spawnImpl(pythonExecutable, args, {
    cwd: projectRoot,
    env,
    ...spawnOptions,
  });

  const readyProcess = await waitForSpawn(child);
  return { process: readyProcess, port: resolvedPort };
}

function buildTokenPlaceClientSource(port, clientImportPath) {
  return `import TokenPlaceClient from '${clientImportPath}';\n\nconst client = new TokenPlaceClient({\n  baseUrl: 'http://localhost:${port}/v1',\n  // use /v1 so the OpenAI client works with token.place directly\n});\n\nvoid client.initialize();\n\nexport default client;\n`;
}

async function startDspace(options = {}) {
  const {
    spawn: spawnImpl = spawn,
    dspaceRoot = path.join(integrationRoot, 'dspace'),
    port,
    clientImportPath = '../../../token.place-client',
    spawnOptions = {},
    fsImpl = fs,
  } = options;

  const resolvedPort = await resolvePort({
    explicitPort: port,
    envKey: 'DSPACE_PORT',
    defaultPort: DEFAULT_DSPACE_PORT,
  });

  const openaiPath = path.join(dspaceRoot, 'src', 'lib', 'openai.js');
  const backupPath = `${openaiPath}.bak`;

  if (!fsImpl.existsSync(openaiPath)) {
    throw new Error(`Expected OpenAI client at ${openaiPath}`);
  }

  const originalSource = fsImpl.readFileSync(openaiPath, 'utf8');
  fsImpl.writeFileSync(backupPath, originalSource, 'utf8');
  backupRecords.set(openaiPath, backupPath);

  const rewrittenSource = buildTokenPlaceClientSource(resolvedPort, clientImportPath);
  fsImpl.writeFileSync(openaiPath, rewrittenSource, 'utf8');

  const args = ['run', 'dev', '--', `--port=${resolvedPort}`];
  const child = spawnImpl('npm', args, {
    cwd: dspaceRoot,
    env: process.env,
    ...spawnOptions,
  });

  const readyProcess = await waitForSpawn(child);
  return { process: readyProcess, port: resolvedPort };
}

async function cleanup(processes = []) {
  for (const proc of processes) {
    if (proc && typeof proc.kill === 'function') {
      try {
        proc.kill('SIGTERM');
      } catch (error) {
        // eslint-disable-next-line no-console
        console.warn('Failed to terminate process during cleanup:', error);
      }
    }
  }

  for (const [originalPath, backupPath] of backupRecords) {
    try {
      if (fs.existsSync(backupPath)) {
        const originalSource = fs.readFileSync(backupPath, 'utf8');
        fs.writeFileSync(originalPath, originalSource, 'utf8');
        fs.rmSync(backupPath);
      }
    } catch (error) {
      // eslint-disable-next-line no-console
      console.warn(`Failed to restore ${originalPath}:`, error);
    }
  }

  backupRecords.clear();
}

module.exports = {
  DEFAULT_TOKEN_PLACE_PORT,
  DEFAULT_DSPACE_PORT,
  TOKEN_PLACE_PORT: DEFAULT_TOKEN_PLACE_PORT,
  DSPACE_PORT: DEFAULT_DSPACE_PORT,
  startTokenPlace,
  startDspace,
  cleanup,
  buildTokenPlaceClientSource,
};
