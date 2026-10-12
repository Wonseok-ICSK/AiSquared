import express from 'express';
import { z } from 'zod';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { AIRPORTS, TripSchema } from './model.js';
import { generateItinerary } from './planner.js';
import { ProviderError } from './providers.js';
import { searchFlightOffers } from './search.js';

export function createApp(store, monitor, providers, config = process.env) {
  const app = express();
  app.disable('x-powered-by');
  app.use((req, res, next) => {
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'strict-origin-when-cross-origin');
    if (req.path.startsWith('/api')) res.setHeader('Cache-Control', 'no-store');
    // Local single-user server: reject browser mutations from unrelated websites.
    if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method) && req.headers.origin) {
      const allowed = [`http://127.0.0.1:${config.PORT || 3001}`, `http://localhost:${config.PORT || 3001}`, 'http://127.0.0.1:5173', 'http://localhost:5173'];
      if (!allowed.includes(req.headers.origin)) return res.status(403).json({ error: 'Origin is not allowed.' });
    }
    next();
  });
  app.use(express.json({ limit: '64kb' }));
  const requireTrip = id => { const trip = store.detail(id); if (!trip) throw new ProviderError('Trip not found.', 404); return trip; };
  app.get('/api/health', (_req, res) => res.json({ status: 'ok', scheduler: 'running', runningChecks: monitor.running.size }));
  app.get('/api/meta', (_req, res) => res.json({
    airports: AIRPORTS, notificationChannel: 'in_app', browserChannel: config.BROWSER_CHANNEL || 'msedge',
    providers: [{ id: 'google_flights', name: 'Google Flights · live', configured: providers.google_flights?.configured ?? false, live: true }, { id: 'demo', name: 'Demo · sample fares', configured: true, live: false }, { id: 'amadeus', name: config.AMADEUS_ENV === 'production' ? 'Amadeus · live' : 'Amadeus · test', configured: providers.amadeus.configured, live: config.AMADEUS_ENV === 'production' }],
    aiConfigured: Boolean(config.OPENAI_API_KEY && config.OPENAI_MODEL), placesConfigured: Boolean(config.GOOGLE_PLACES_API_KEY),
    timezone: 'Asia/Seoul', maxSearchVariants: Math.min(100, Math.max(1, Number(config.MAX_SEARCH_VARIANTS) || 25)),
  }));
  app.post('/api/flights/search', async (req, res) => {
    const trip = TripSchema.parse(req.body);
    if (trip.provider === 'demo' || (trip.provider === 'amadeus' && providers.amadeus.environment !== 'production')) throw new ProviderError('Real-price search requires a live provider. Demo and test fares are not real prices.', 422);
    if (!providers[trip.provider]?.configured) throw new ProviderError('The selected live flight provider is unavailable.', 503);
    const today = new Date().toLocaleDateString('en-CA', { timeZone: trip.schedule.timezone });
    if (trip.departureDate < today) throw new ProviderError('Choose a departure date today or later.', 400);
    const result = await searchFlightOffers(trip, providers, config);
    res.json({ ...result, search: trip, isDemo: false, source: trip.provider === 'google_flights' ? 'Google Flights' : 'Amadeus' });
  });
  app.get('/api/trips', (_req, res) => res.json(store.listTrips()));
  app.post('/api/trips', (req, res) => {
    const data = TripSchema.parse(req.body);
    if (data.provider === 'amadeus' && !providers.amadeus.configured) throw new ProviderError('Configure Amadeus server credentials before selecting this provider.', 422);
    const trip = store.createTrip(data); res.status(201).json(store.detail(trip.id));
  });
  app.get('/api/trips/:id', (req, res) => res.json(requireTrip(req.params.id)));
  app.put('/api/trips/:id', (req, res) => {
    requireTrip(req.params.id);
    const data = TripSchema.parse(req.body);
    if (data.provider === 'amadeus' && !providers.amadeus.configured) throw new ProviderError('Configure Amadeus server credentials before selecting this provider.', 422);
    store.updateTrip(req.params.id, data); res.json(store.detail(req.params.id));
  });
  app.patch('/api/trips/:id/status', (req, res) => {
    requireTrip(req.params.id); const { active } = z.object({ active: z.boolean() }).parse(req.body);
    store.setActive(req.params.id, active); res.json(store.detail(req.params.id));
  });
  app.delete('/api/trips/:id', (req, res) => { requireTrip(req.params.id); store.deleteTrip(req.params.id); res.status(204).end(); });
  app.post('/api/trips/:id/check', async (req, res) => res.json(await monitor.checkTrip(req.params.id)));
  const planning = new Set();
  app.post('/api/trips/:id/itinerary', async (req, res) => {
    const trip = requireTrip(req.params.id);
    const { flightId } = z.object({ flightId: z.string().min(1).max(100) }).parse(req.body);
    const listedOffer = trip.offers.find(o => o.id === flightId);
    if (!listedOffer) throw new ProviderError('Selected flight is no longer in the latest results. Choose a current flight.', 422);
    if (planning.has(trip.id)) throw new ProviderError('An itinerary is already being generated.', 409);
    planning.add(trip.id);
    try {
      const offer = await providers[trip.provider].getFlightDetails(listedOffer);
      if (offer.returnDetailsPending) throw new ProviderError('No matching return flight is available. Search prices again before generating a plan.', 422);
      const itinerary = await generateItinerary(trip, offer, { config });
      // Any edit during generation invalidates the result, including budget/preferences edits.
      const fresh = store.getTrip(trip.id);
      const keys = Object.keys(TripSchema.parse(trip));
      if (!fresh || fresh.version !== trip.version || keys.some(k => JSON.stringify(fresh[k]) !== JSON.stringify(trip[k]))) throw new ProviderError('Trip changed during planning. Generate a fresh itinerary.', 409);
      store.saveItinerary(trip.id, itinerary); res.json(itinerary);
    } finally { planning.delete(trip.id); }
  });
  app.post('/api/trips/:id/reprice', async (req, res) => {
    const trip = requireTrip(req.params.id), { flightId } = z.object({ flightId: z.string().min(1).max(100) }).parse(req.body);
    const offer = trip.offers.find(o => o.id === flightId);
    if (!offer) throw new ProviderError('Choose a flight from the latest results.', 422);
    const priceTotal = await providers[trip.provider].getPrice(offer);
    res.json({ priceTotal, currency: 'KRW', isDemo: offer.isDemo, checkedAt: new Date().toISOString(), message: offer.isDemo ? 'Sample fare; no live availability is confirmed.' : 'Provider fare rechecked. Availability and price can change before purchase.' });
  });
  app.get('/api/notifications', (_req, res) => res.json(store.notifications()));
  app.post('/api/notifications/read-all', (_req, res) => { store.readAllNotifications(); res.json({ ok: true }); });
  app.post('/api/notifications/:id/read', (req, res) => { if (!store.readNotification(req.params.id)) throw new ProviderError('Notification not found.', 404); res.json({ ok: true }); });
  app.use('/api', (_req, res) => res.status(404).json({ error: 'API route not found.' }));
  const dist = resolve('dist');
  if (existsSync(dist)) { app.use(express.static(dist)); app.get('/{*path}', (_req, res) => res.sendFile(resolve(dist, 'index.html'))); }
  app.use((error, _req, res, _next) => {
    if (error instanceof z.ZodError) return res.status(400).json({ error: error.issues.map(i => `${i.path.join('.')}: ${i.message}`).join(' '), issues: error.issues });
    const status = error.status || (error.type === 'entity.parse.failed' ? 400 : 500);
    res.status(status).json({ error: status === 500 ? 'Unexpected server error. Please try again.' : error.type === 'entity.parse.failed' ? 'Invalid JSON request.' : error.message });
  });
  return app;
}
