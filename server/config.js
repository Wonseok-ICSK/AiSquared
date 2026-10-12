import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

export function loadConfig(env = process.env, { exists = existsSync, run = spawnSync, platform = process.platform } = {}) {
  const vault = resolve(env.CONFIG_VAULT_FILE || '.secrets/integrations.json');
  if (!exists(vault)) return { ...env };
  if (platform !== 'win32') throw new Error('The DPAPI vault needs the Windows account that created it. Use a managed secret store or process environment on other platforms.');
  const result = run('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', resolve('scripts/configure-integrations.ps1'), '-Action', 'Get', '-VaultPath', vault], { encoding: 'utf8', windowsHide: true, timeout: 10000 });
  if (result.status !== 0) throw new Error('Could not unlock the encrypted integration vault. Run Wayfinder under the Windows account that configured it.');
  let secrets;
  try { secrets = JSON.parse(result.stdout.trim().replace(/^\uFEFF/, '')); }
  catch { throw new Error('The encrypted integration vault could not be read. Reconfigure the integrations.'); }
  const config = { ...env };
  for (const name of ['AMADEUS_CLIENT_ID', 'AMADEUS_CLIENT_SECRET', 'OPENAI_API_KEY', 'GOOGLE_PLACES_API_KEY']) if (!config[name] && typeof secrets[name] === 'string') config[name] = secrets[name];
  return config;
}
