import { spawnSync } from 'node:child_process';
import { cpSync, mkdirSync, readdirSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
const root = fileURLToPath(new URL('../', import.meta.url));
// Only the generated directory beside this build script is removed. A clean
// output prevents legacy browser gateway modules from surviving an older build.
const output = fileURLToPath(new URL('../dist/', import.meta.url));
if (output !== join(root, 'dist') + (process.platform === 'win32' ? '\\' : '/')) throw Error('QR_BUILD_OUTPUT');
rmSync(output, { recursive: true, force: true });
const result = spawnSync(process.execPath, [join(root, 'node_modules/typescript/bin/tsc'), '-p', join(root, 'tsconfig.qr.json')], { cwd: root, stdio: 'inherit' });
if (result.status !== 0) process.exit(result.status ?? 1);
// TypeScript does not emit the reviewed native .mjs modules when allowJs=false.
mkdirSync(join(root, 'dist'), { recursive: true });
for (const name of readdirSync(join(root, 'src'))) {
  if (name.endsWith('.mjs')) cpSync(join(root, 'src', name), join(root, 'dist', name));
}
console.log('QR TypeScript and native module build complete');
