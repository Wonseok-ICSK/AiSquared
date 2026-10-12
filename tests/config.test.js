import test from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../server/config.js';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('configuration uses process environment without creating plaintext secret files', () => {
  const config = loadConfig({ PORT: '3001', OPENAI_API_KEY: 'in-memory-value' }, { exists: () => false });
  assert.equal(config.OPENAI_API_KEY, 'in-memory-value');
});
test('encrypted vault loads over a hidden child-process pipe and environment takes precedence', () => {
  const config = loadConfig({ OPENAI_API_KEY: 'env-value' }, { exists: () => true, platform: 'win32', run: (command, args, options) => {
    assert.equal(command, 'powershell.exe'); assert.equal(options.windowsHide, true);
    assert.ok(!args.includes('env-value'));
    return { status: 0, stdout: JSON.stringify({ OPENAI_API_KEY: 'vault-value', AMADEUS_CLIENT_SECRET: 'encrypted-at-rest' }) };
  } });
  assert.equal(config.OPENAI_API_KEY, 'env-value'); assert.equal(config.AMADEUS_CLIENT_SECRET, 'encrypted-at-rest');
});
test('vault errors reveal no captured secret values', () => {
  assert.throws(() => loadConfig({}, { exists: () => true, platform: 'win32', run: () => ({ status: 1, stdout: 'private-key', stderr: 'private-key' }) }), error => !error.message.includes('private-key') && error.message.includes('unlock'));
});

test('Windows DPAPI encrypts at rest and the real configuration helper decrypts into memory', { skip: process.platform !== 'win32' }, t => {
  const directory = mkdtempSync(join(tmpdir(), 'wayfinder-vault-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const vault = join(directory, 'vault.json');
  const result = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', "$ErrorActionPreference='Stop'; Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1'); $secureTestValue=ConvertTo-SecureString 'dummy-integration-token' -AsPlainText -Force; $encryptedTestValue=ConvertFrom-SecureString $secureTestValue; @{OPENAI_API_KEY=$encryptedTestValue} | ConvertTo-Json | Set-Content -LiteralPath $env:WAYFINDER_TEST_VAULT -Encoding UTF8"], { env: { ...process.env, WAYFINDER_TEST_VAULT: vault }, encoding: 'utf8', windowsHide: true });
  assert.equal(result.status, 0, 'Test vault encryption failed');
  assert.ok(!readFileSync(vault, 'utf8').includes('dummy-integration-token'));
  const config = loadConfig({ CONFIG_VAULT_FILE: vault });
  assert.equal(config.OPENAI_API_KEY, 'dummy-integration-token');
});
