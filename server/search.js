import { basisPrice, inWindow, searchVariants } from './model.js';
import { ProviderError } from './provider-base.js';

export async function searchFlightOffers(trip, providers, config = {}, context = {}) {
  const provider = providers[trip.provider];
  if (!provider) throw new ProviderError('Unknown flight provider.', 400);
  const maxVariants = Math.min(provider.maxSearchVariants || 100, Math.max(1, Number(config.MAX_SEARCH_VARIANTS) || 25));
  const { variants, total, truncated } = searchVariants(trip, maxVariants);
  const warnings = [], found = [];
  let completed = 0, failures = 0;
  const deadline = Date.now() + 90000;
  for (const variant of variants) {
    if (Date.now() > deadline) { warnings.push('Search time limit reached; some variants were not checked.'); break; }
    try { found.push(...await provider.searchFlights(variant, context)); completed++; }
    catch (error) { failures++; if (completed === 0 && (error.status === 503 || variants.length === 1)) throw error; }
  }
  if (!completed) throw new ProviderError('All provider searches failed. Previous prices are preserved.');
  if (truncated) warnings.push(`Checked up to ${variants.length} of ${total} date/airport combinations. Increase MAX_SEARCH_VARIANTS on the server for wider coverage.`);
  if (failures) warnings.push(`${failures} search variant(s) failed; results have partial coverage.`);
  const offers = [...new Map(found.filter(o => Number.isFinite(o.priceTotal) && o.priceTotal > 0 && o.currency === 'KRW' && (!trip.directOnly || (o.stops === 0 && (o.returnStops === 0 || o.returnDetailsPending))) && inWindow(o.departureAt, trip.departureWindow) && inWindow(o.arrivalAt, trip.arrivalWindow) && (trip.maxPrice <= 0 || basisPrice(o.priceTotal, trip) <= trip.maxPrice)).map(o => [o.id, o])).values()].sort((a, b) => a.priceTotal - b.priceTotal).slice(0, 30);
  return { offers, warning: warnings.join(' ') || null, checkedAt: new Date().toISOString() };
}
