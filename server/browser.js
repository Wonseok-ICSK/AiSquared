// Separate from providers. Future approved airline adapters can use this helper.
// Edge is the default. No automatic Chrome fallback and no personal profile access.
import { chromium } from '@playwright/test';
export async function launchBrowser({ channel = process.env.BROWSER_CHANNEL || 'msedge', headless = true } = {}) {
  return chromium.launch({ channel, headless });
}
