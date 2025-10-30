const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const TOKEN_PLACE_PORT = Number(process.env.TOKEN_PLACE_PORT || 5555);
const DSPACE_PORT = Number(process.env.DSPACE_PORT || 4444);

const integrationRoot = process.env.TOKEN_PLACE_INTEGRATION_ROOT || __dirname;

const backupRecords = new Map();

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

function startTokenPlace(options = {}) {
  const {
    spawn: spawnImpl = spawn,
    projectRoot = path.join(integrationRoot, 'token.place'),
    pythonExecutable = 'python',
    port = TOKEN_PLACE_PORT,
    env: envOverrides = {},
    spawnOptions = {},
  } = options;

  const args = ['server.py', `--port=${port}`];
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

  return waitForSpawn(child);
}

function buildTokenPlaceClientSource(port, clientImportPath) {
  return `import TokenPlaceClient from '${clientImportPath}';\n\nconst client = new TokenPlaceClient({\n  baseUrl: 'http://localhost:${port}/v1',\n  // use /v1 so the OpenAI client works with token.place directly\n});\n\nvoid client.initialize();\n\nexport default client;\n`;
}

function startDspace(options = {}) {
  const {
    spawn: spawnImpl = spawn,
    dspaceRoot = path.join(integrationRoot, 'dspace'),
    port = DSPACE_PORT,
    clientImportPath = '../../../token.place-client',
    spawnOptions = {},
    fsImpl = fs,
  } = options;

  const openaiPath = path.join(dspaceRoot, 'src', 'lib', 'openai.js');
  const backupPath = `${openaiPath}.bak`;

  if (!fsImpl.existsSync(openaiPath)) {
    throw new Error(`Expected OpenAI client at ${openaiPath}`);
  }

  const originalSource = fsImpl.readFileSync(openaiPath, 'utf8');
  fsImpl.writeFileSync(backupPath, originalSource, 'utf8');
  backupRecords.set(openaiPath, backupPath);

  const rewrittenSource = buildTokenPlaceClientSource(port, clientImportPath);
  fsImpl.writeFileSync(openaiPath, rewrittenSource, 'utf8');

  const args = ['run', 'dev', '--', `--port=${port}`];
  const child = spawnImpl('npm', args, {
    cwd: dspaceRoot,
    env: process.env,
    ...spawnOptions,
  });

  return waitForSpawn(child);
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
  TOKEN_PLACE_PORT,
  DSPACE_PORT,
  startTokenPlace,
  startDspace,
  cleanup,
  buildTokenPlaceClientSource,
};
