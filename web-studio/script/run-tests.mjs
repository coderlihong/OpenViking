// Keep browser storage tests under jsdom instead of Node's server-side Web Storage.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const flags = process.allowedNodeEnvironmentFlags.has('--no-experimental-webstorage')
  ? ['--no-experimental-webstorage'] : [];
const vitest = fileURLToPath(new URL('../node_modules/vitest/vitest.mjs', import.meta.url));
const result = spawnSync(process.execPath, [...flags, vitest, 'run', ...process.argv.slice(2)], {
  stdio: 'inherit',
  // Vitest may replace execArgv when spawning workers. Propagate the storage
  // setting through their inherited environment as well.
  env: {
    ...process.env,
    NODE_OPTIONS: [process.env.NODE_OPTIONS, ...flags].filter(Boolean).join(' '),
  },
});
if (result.error) throw result.error;
process.exit(result.status ?? 1);
