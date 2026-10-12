import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { GoogleFlightsProvider } from '../server/google-flights.js';
import { extractGoogleData, googleBlockReason, parseGoogleOffers } from '../server/google-flights-parse.js';
import { createStore } from '../server/store.js';
import { createMonitor } from '../server/monitor.js';
import { createProviders } from '../server/providers.js';
import { createApp } from '../server/app.js';
import { tripData } from './fixtures.js';

const twoAdults = JSON.parse(await readFile(new URL('./fixtures/google/round-trip-2.json', import.meta.url), 'utf8'));
const oneAdult = JSON.parse(await readFile(new URL('./fixtures/google/round-trip-1.json', import.meta.url), 'utf8'));
const returns = JSON.parse(await readFile(new URL('./fixtures/google/return-options.json', import.meta.url), 'utf8'));
const trip = tripData({ provider: 'google_flights', departureDate: '2026-11-20', returnDate: '2026-11-27', targetPrice: 400000, maxPrice: 0 });
const html = payload => `<script class="ds:1">AF_initDataCallback({key:'ds:1',data:${JSON.stringify(payload)},sideChannel:{}});</script>`;
function fixtureProvider(responses = [twoAdults, returns]) {
  let clock = Date.parse('2026-10-08T00:00:00Z'); const urls = [], waits = [];
  const provider = new GoogleFlightsProvider({}, {
    now: () => clock, sleep: async ms => { waits.push(ms); clock += ms; },
    fetchPage: async url => { urls.push(url); const data = responses[Math.min(urls.length - 1, responses.length - 1)]; return Array.isArray(data) ? { html: html(data), url, status: 200 } : { ...data, url: data.url || url }; },
  });
  return { provider, urls, waits, advance: ms => { clock += ms; } };
}

test('captured real Google results preserve total-party prices, local schedules, and real flight numbers', () => {
  const offers = parseGoogleOffers(twoAdults, trip);
  const single = parseGoogleOffers(oneAdult, { ...trip, passengers: 1 });
  const airPremia = offers.find(o => o.flightNumber === 'YP 735');
  assert.ok(airPremia); assert.equal(airPremia.priceTotal, 631400); assert.equal(airPremia.pricePerPerson, 315700);
  assert.equal(single.find(o => o.flightNumber === 'YP 735').priceTotal, 315700);
  assert.equal(airPremia.isDemo, false); assert.equal(airPremia.source, 'Google Flights');
  assert.match(airPremia.departureAt, /2026-11-20T13:55.*\+09:00$/);
  assert.equal(airPremia.returnDepartureAt, null); assert.equal(airPremia.returnDate, '2026-11-27');
  assert.equal(airPremia.returnDetailsPending, true);
  assert.ok(offers.every(o => o.priceTotal > 0 && o.origin === 'ICN' && o.destination === 'NRT'));
  assert.equal(new Set(offers.map(o => o.id)).size, offers.length);
});

test('JSON page extraction handles quoted brackets and fails explicitly for changed formats', () => {
  assert.deepEqual(extractGoogleData(html([null, 'text ] with \\"quote', [[], 0], null])), [null, 'text ] with \\"quote', [[], 0], null]);
  assert.throws(() => extractGoogleData('<html>new layout</html>'), /format changed/);
  assert.throws(() => parseGoogleOffers([1], trip), /format changed/);
  assert.deepEqual(parseGoogleOffers([], trip), []);
  assert.equal(googleBlockReason('<form id="captcha-form">'), 'Google access check');
});

test('Edge provider caches identical searches, spaces requests, and retrieves an actual paired return', async () => {
  const { provider, urls, waits } = fixtureProvider();
  const [a, b] = await Promise.all([provider.searchFlights(trip), provider.searchFlights(trip)]);
  assert.equal(urls.length, 1); assert.deepEqual(a, b);
  assert.equal(new URL(urls[0]).pathname, '/travel/flights');
  assert.equal(new URL(urls[0]).searchParams.get('curr'), 'KRW');
  const selected = a.find(o => o.flightNumber === 'YP 735');
  const detailed = await provider.getFlightDetails(selected);
  assert.equal(urls.length, 2); assert.deepEqual(waits, [5000]);
  assert.equal(detailed.returnDetailsPending, false); assert.equal(detailed.returnFlightNumber, 'YP 732');
  assert.match(detailed.returnDepartureAt, /2026-11-27T12:30/);
  assert.equal(detailed.priceTotal, 631400); assert.equal(detailed.pricePerPerson, 315700);
  assert.equal(new URL(detailed.bookingUrl).pathname, '/travel/flights/booking');
  const tfs = Buffer.from(new URL(detailed.bookingUrl).searchParams.get('tfs'), 'base64url').toString('utf8');
  assert.match(tfs, /735/); assert.match(tfs, /732/);
  assert.equal(selected.returnDetailsPending, true);
});

test('a blocked search is never retried or converted to demo data', async () => {
  const { provider, urls } = fixtureProvider([{ html: '<form id="captcha-form">', status: 429 }]);
  await assert.rejects(provider.searchFlights(trip), /temporarily blocked/);
  await assert.rejects(provider.searchFlights({ ...trip, passengers: 1 }), /temporarily blocked/);
  assert.equal(urls.length, 1);
});

test('real monitoring keeps the round-trip quote and alerts, and preserves history on provider failure', async () => {
  const store = createStore(':memory:');
  try {
    const { provider } = fixtureProvider();
    const providers = { ...createProviders({}), google_flights: provider };
    const monitor = createMonitor(store, providers, {});
    const saved = store.createTrip(trip);
    const result = await monitor.checkTrip(saved.id);
    assert.ok(result.offers.length); assert.equal(result.sourceMode, 'google_flights:live');
    assert.equal(result.offers[0].isDemo, false); assert.equal(result.currentPrice, 315700);
    const alert = store.notifications()[0];
    assert.equal(alert.returnDate, '2026-11-27'); assert.equal(alert.passengers, 2); assert.equal(alert.isDemo, false);
    provider.searchFlights = async () => { throw new Error('network failure'); };
    await assert.rejects(monitor.checkTrip(saved.id), /Previous prices are preserved/);
    assert.equal(store.detail(saved.id).history.length, 1); assert.equal(store.detail(saved.id).currentPrice, 315700);
  } finally { store.close(); }
});

test('live search API forbids demo/test results and performs an ad-hoc search without saving a trip', async t => {
  const store = createStore(':memory:');
  const providers = createProviders({});
  let searches = 0;
  providers.google_flights.searchFlights = async query => { searches++; return [{ id: 'real-fixture', source: 'Google Flights', isDemo: false, currency: 'KRW', priceTotal: 620000, pricePerPerson: 310000, departureAt: `${query.departureDate}T11:00:00+09:00`, arrivalAt: `${query.departureDate}T13:30:00+09:00`, stops: 0, returnStops: null, returnDetailsPending: true }]; };
  const monitor = createMonitor(store, providers, {});
  const server = createApp(store, monitor, providers, {}).listen(0, '127.0.0.1');
  await new Promise(resolve => server.on('listening', resolve));
  t.after(() => new Promise(resolve => server.close(() => { store.close(); resolve(); })));
  const request = data => fetch(`http://127.0.0.1:${server.address().port}/api/flights/search`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  assert.equal((await request(tripData())).status, 422);
  assert.equal((await request(tripData({ provider: 'amadeus' }))).status, 422);
  assert.equal((await request(tripData({ provider: 'google_flights', departureDate: '2020-01-01' }))).status, 400);
  const response = await request(tripData({ provider: 'google_flights' }));
  assert.equal(response.status, 200); const result = await response.json();
  assert.equal(result.isDemo, false); assert.equal(result.offers[0].priceTotal, 620000);
  assert.equal(searches, 1); assert.equal(store.listTrips().length, 0);
});

test('real itinerary API retrieves the paired return before planning and rejects an unavailable return', async t => {
  const store = createStore(':memory:');
  const { provider } = fixtureProvider();
  const providers = { ...createProviders({}), google_flights: provider };
  const monitor = createMonitor(store, providers, {});
  const saved = store.createTrip(trip);
  const checked = await monitor.checkTrip(saved.id);
  const selected = checked.offers.find(o => o.flightNumber === 'YP 735');
  const server = createApp(store, monitor, providers, {}).listen(0, '127.0.0.1');
  await new Promise(resolve => server.on('listening', resolve));
  t.after(() => new Promise(resolve => server.close(() => { store.close(); resolve(); })));
  const request = () => fetch(`http://127.0.0.1:${server.address().port}/api/trips/${saved.id}/itinerary`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ flightId: selected.id }) });
  const response = await request(); assert.equal(response.status, 200);
  const plan = await response.json(); assert.match(plan.flightDeparture, /2026-11-27T12:30/);
  provider.getFlightDetails = async offer => offer;
  assert.equal((await request()).status, 422);
  assert.equal(store.detail(saved.id).itinerary.flightDeparture, plan.flightDeparture);
});
