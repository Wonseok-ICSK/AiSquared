import { alertReasons, basisPrice } from './model.js';
import { ProviderError } from './providers.js';
import { searchFlightOffers } from './search.js';

export function createMonitor(store, providers, config = process.env) {
  const running = new Set();
  async function checkTrip(id, { scheduled = false } = {}) {
    let trip = store.getTrip(id);
    if (!trip) throw new ProviderError('Trip not found.', 404);
    if (scheduled && !trip.active) return null;
    if (running.has(id) || !store.acquireLease(id)) throw new ProviderError('A price check is already running for this trip.', 409);
    running.add(id);
    try {
      const sourceMode = trip.provider === 'demo' ? 'demo' : `${trip.provider}:${providers[trip.provider].environment || 'test'}`;
      trip = store.setSourceMode(id, sourceMode);
      const history = store.history(id), priced = history.filter(h => h.price != null);
      const { offers, warning, checkedAt } = await searchFlightOffers(trip, providers, config, { checkIndex: history.length });
      const fresh = store.getTrip(id);
      if (!fresh || fresh.version !== trip.version) throw new ProviderError('Trip changed while searching. Run a fresh check.', 409);
      const reasons = offers.length ? alertReasons(basisPrice(offers[0].priceTotal, fresh), priced.at(-1)?.price ?? null, priced.length ? Math.min(...priced.map(h => h.price)) : null, fresh) : [];
      store.addCheck(fresh, offers, reasons, checkedAt, warning);
      return store.detail(id);
    } catch (error) {
      const message = error.name === 'TimeoutError' ? 'Provider timed out. Previous prices are preserved; the scheduler will retry.' : error instanceof ProviderError ? error.message : 'Price search failed. Previous prices are preserved; check the server configuration.';
      store.recordError(id, message);
      if (error instanceof ProviderError) throw error;
      throw new ProviderError(message);
    } finally { running.delete(id); store.releaseLease(id); }
  }
  let ticking = false;
  async function tick() {
    if (ticking) return { checked: 0, failed: 0 }; ticking = true;
    let checked = 0, failed = 0;
    try {
      for (const trip of store.dueTrips()) {
        if (running.has(trip.id)) continue;
        try { await checkTrip(trip.id, { scheduled: true }); checked++; }
        catch (error) { if (error.status !== 409) failed++; console.warn(`Scheduled check ${trip.id}: ${error.message}`); }
      }
    } finally { ticking = false; }
    return { checked, failed };
  }
  return { checkTrip, tick, running, start: () => { const timer = setInterval(tick, 30000); timer.unref(); void tick(); return () => clearInterval(timer); } };
}
