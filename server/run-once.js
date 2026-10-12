import { createStore } from './store.js';
import { createProviders } from './providers.js';
import { createMonitor } from './monitor.js';
import { loadConfig } from './config.js';
const config = loadConfig();
const store = createStore(config.DATABASE_PATH);
const monitor = createMonitor(store, createProviders(config), config);
try { const result = await monitor.tick(); console.log(`Due flight checks: ${result.checked} completed, ${result.failed} failed. See trip status for details.`); if (result.failed) process.exitCode = 1; }
finally { store.close(); }
