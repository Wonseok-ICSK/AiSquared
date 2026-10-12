import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/external', fullyParallel: false, workers: 1, timeout: 60000,
  outputDir: './test-results/external-booking',
  use: { channel: 'msedge', headless: true, viewport: { width: 1440, height: 1080 }, screenshot: 'only-on-failure', trace: 'retain-on-failure' },
});
