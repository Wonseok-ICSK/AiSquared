import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/browser', fullyParallel: false, workers: 1, timeout: 30000,
  outputDir: './test-results/ui',
  use: { baseURL: 'http://127.0.0.1:3101', channel: 'msedge', headless: true, viewport: { width: 1440, height: 1080 }, screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: { command: 'node server/index.js', url: 'http://127.0.0.1:3101/api/health', reuseExistingServer: false, timeout: 20000, env: { PORT: '3101', DATABASE_PATH: ':memory:', SEED_DEMO: 'true', OPENAI_API_KEY: '', OPENAI_MODEL: '', GOOGLE_PLACES_API_KEY: '', AMADEUS_CLIENT_ID: '', AMADEUS_CLIENT_SECRET: '', CONFIG_VAULT_FILE: './test-results/no-vault.json' } },
});
