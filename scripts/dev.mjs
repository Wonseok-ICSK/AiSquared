import { spawn } from 'node:child_process';
const children = [
  spawn(process.execPath, ['--watch', '--env-file-if-exists=.env', 'server/index.js'], { stdio: 'inherit' }),
  spawn(process.execPath, ['node_modules/vite/bin/vite.js'], { stdio: 'inherit' }),
];
let closing = false;
function close(code = 0) { if (closing) return; closing = true; children.forEach(p => p.kill()); process.exitCode = code; }
children.forEach(p => p.on('exit', code => close(code || 0)));
process.on('SIGINT', () => close()); process.on('SIGTERM', () => close());
