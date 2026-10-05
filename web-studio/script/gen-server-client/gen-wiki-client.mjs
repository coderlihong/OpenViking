// Generate knowledge routes, keeping the shared transport instance.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
if (!process.argv[2]) throw new Error('Usage: node gen-wiki-client.mjs <server-openapi.json>');
const original = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const paths = Object.fromEntries(Object.entries(original.paths).filter(([key]) =>
  key.startsWith('/api/v1/kb/wiki/') ||
  key === '/api/v1/kb/namespaces' ||
  key.startsWith('/api/v1/kb/namespaces/') ||
  key === '/api/v1/kb/ingestion-jobs' ||
  key === '/api/v1/kb/jobs' ||
  key.startsWith('/api/v1/kb/jobs/')
));
const schemas = {};
function scan(value) {
  if (!value || typeof value !== 'object') return;
  const ref = value.$ref;
  if (typeof ref === 'string' && ref.startsWith('#/components/schemas/')) {
    const name = ref.split('/').at(-1);
    if (!(name in schemas)) {
      schemas[name] = original.components.schemas[name];
      if (!schemas[name]) throw new Error(`Missing schema: ${name}`);
      scan(schemas[name]);
    }
  }
  for (const item of Object.values(value)) scan(item);
}
scan(paths);
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'ov-wiki-client-'));
try {
  const input = path.join(temp, 'openapi.json');
  const formatted = path.join(temp, 'formatted.json');
  const generated = path.join(temp, 'generated');
  fs.writeFileSync(input, JSON.stringify({ openapi: original.openapi, info: original.info, paths, components: { schemas } }));
  execFileSync(path.join(root, 'node_modules/.bin/openapi-format'), [input, '--configFile', path.join(root, 'script/gen-server-client/oaf-generate-conf.json'), '--output', formatted], { cwd: root, stdio: 'inherit' });
  execFileSync(process.execPath, [path.join(root, 'script/gen-server-client/polishOpId.js'), formatted], { stdio: 'inherit' });
  execFileSync(path.join(root, 'node_modules/.bin/openapi-ts'), ['-i', formatted, '-o', generated, '-c', '@hey-api/client-axios'], { cwd: root, stdio: 'inherit' });
  const output = path.join(root, 'src/gen/wiki-client');
  fs.mkdirSync(output, { recursive: true });
  for (const name of ['sdk.gen.ts', 'types.gen.ts', 'index.ts']) {
    let text = fs.readFileSync(path.join(generated, name), 'utf8');
    if (name === 'sdk.gen.ts') text = text.replace("from './client';", "from '../ov-client/client';").replace("from './client.gen';", "from '../ov-client/client.gen';");
    fs.writeFileSync(path.join(output, name), text);
  }
} finally { fs.rmSync(temp, { recursive: true, force: true }); }
