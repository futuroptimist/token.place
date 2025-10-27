import assert from 'node:assert';
import fs from 'node:fs/promises';
import path from 'node:path';

async function runTokenPlaceClientPackageTest(): Promise<void> {
  const packageDir = path.resolve(__dirname, '../clients/token-place-client');
  const packageJsonPath = path.join(packageDir, 'package.json');

  const packageJsonRaw = await fs.readFile(packageJsonPath, 'utf-8');
  const packageJson = JSON.parse(packageJsonRaw) as { name?: string };

  assert.strictEqual(
    packageJson.name,
    'token.place-client',
    'Package name should be token.place-client'
  );

  const module = await import(packageDir);
  assert.ok(
    typeof module.TokenPlaceClient === 'function',
    'TokenPlaceClient export should be a constructor function'
  );

  console.log('✅ TokenPlaceClient npm package smoke test passed');
}

if (require.main === module) {
  runTokenPlaceClientPackageTest().catch(error => {
    console.error('❌ TokenPlaceClient package smoke test failed:', error);
    process.exitCode = 1;
  });
}

export { runTokenPlaceClientPackageTest };
