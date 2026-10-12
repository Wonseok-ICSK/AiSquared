import { createStore } from './store.js';
import { createProviders } from './providers.js';
import { createMonitor } from './monitor.js';
import { createApp } from './app.js';
import { seedDemo } from './seed.js';
import { loadConfig } from './config.js';

const config = loadConfig();
const store = createStore(config.DATABASE_PATH);
if (config.SEED_DEMO !== 'false') await seedDemo(store);
const providers = createProviders(config);
const monitor = createMonitor(store, providers, config);
const app = createApp(store, monitor, providers, config);
const port = Number(config.PORT) || 3001;
const server = app.listen(port, config.HOST || '127.0.0.1', () => console.log(`Wayfinder: http://127.0.0.1:${port} · daily scheduler active`));
const stopScheduler = monitor.start();
async function shutdown() { stopScheduler(); server.close(() => { store.close(); process.exit(0); }); setTimeout(() => process.exit(0), 5000).unref(); }
process.on('SIGINT', shutdown); process.on('SIGTERM', shutdown);
