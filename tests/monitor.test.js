import test from 'node:test';
import assert from 'node:assert/strict';
import { DateTime } from 'luxon';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createStore } from '../server/store.js';
import { createMonitor } from '../server/monitor.js';
import { DemoProvider, ProviderError } from '../server/providers.js';
import { TripSchema, alertReasons, basisPrice, inWindow, nextCheck, searchVariants } from '../server/model.js';
import { seedDemo } from '../server/seed.js';
import { tripData } from './fixtures.js';

test('trip validation rejects invalid dates, passengers, routes, negative budgets, and incomplete schedules', () => {
  for (const bad of [{ departureDate: '2030-02-30' }, { returnDate: '2030-01-01' }, { passengers: 3 }, { destination: 'ICN' }, { budget: { total: -2, flight: 200000 } }, { schedule: { mode: 'twice_daily', times: ['08:30'] } }]) assert.throws(() => tripData(bad));
  assert.doesNotThrow(() => tripData({ budget: { total: 0, flight: -1 } }));
  assert.throws(() => TripSchema.parse({ ...tripData(), targetPrice: 500000, maxPrice: 300000 }));
});
test('daily, twice-daily, intervals, and paused schedules preserve timezone intent', () => {
  const base = { ...tripData(), active: true };
  const now = DateTime.fromISO('2030-11-01T00:00:00Z');
  assert.equal(nextCheck(base, now), '2030-11-01T23:30:00.000Z');
  assert.equal(nextCheck({ ...base, schedule: { ...base.schedule, mode: 'twice_daily' } }, now), '2030-11-01T08:30:00.000Z');
  assert.equal(nextCheck({ ...base, schedule: { ...base.schedule, mode: 'interval', intervalHours: 6 } }, now), '2030-11-01T06:00:00.000Z');
  assert.equal(nextCheck({ ...base, active: false }, now), null);
  assert.equal(nextCheck(base, DateTime.fromISO('2030-11-21T00:00:00Z')), null);
});
test('alert rules fire on equality, threshold crossings, percentage drops, and new lows', () => {
  const trip = tripData({ alertRules: { target: true, drop: true, dropPercent: 10, lowest: true } });
  assert.deepEqual(alertReasons(300000, null, null, trip), ['target']);
  assert.deepEqual(alertReasons(270000, 300000, 280000, trip), ['drop', 'lowest']);
  assert.deepEqual(alertReasons(270000, 270000, 270000, trip), []);
  assert.deepEqual(alertReasons(295000, 310000, 290000, trip), ['target']);
  assert.equal(basisPrice(556000, trip), 278000);
  assert.equal(basisPrice(556000, { ...trip, priceBasis: 'total' }), 556000);
});
test('flexible searches are bounded, keep exact dates first, and never return impossible date pairs', () => {
  const trip = tripData({ flexDeparture: 7, flexReturn: 7, nearbyAirports: true, alternativeDestinations: ['KIX'] });
  const result = searchVariants(trip, 25);
  assert.equal(result.variants.length, 25); assert.ok(result.truncated);
  assert.equal(result.variants[0].departureDate, trip.departureDate);
  assert.equal(result.variants[0].destination, 'NRT');
  assert.ok(result.variants.every(t => t.returnDate >= t.departureDate));
  assert.ok(inWindow('2030-11-20T01:00:00+09:00', { start: '22:00', end: '06:00' }));
  assert.ok(!inWindow('2030-11-20T12:00:00+09:00', { start: '22:00', end: '06:00' }));
});
test('monitor persists observations and sends one combined alert per qualifying check', async t => {
  const store = createStore(':memory:'); t.after(() => store.close());
  const trip = store.createTrip(tripData({ alertRules: { target: true, drop: false, lowest: false } }));
  const monitor = createMonitor(store, { demo: new DemoProvider() }, {});
  for (let i = 0; i < 6; i++) await monitor.checkTrip(trip.id);
  const detail = store.detail(trip.id);
  assert.equal(detail.history.length, 6); assert.equal(detail.currentPrice, 289000);
  assert.equal(detail.lowestPrice, 289000); assert.equal(detail.previousPrice, 298000);
  assert.equal(store.notifications().length, 1); assert.deepEqual(store.notifications()[0].reasons, ['target']);
  assert.equal(store.notifications()[0].passengers, 2);
});
test('total-party thresholds do not confuse per-person fares', async t => {
  const store = createStore(':memory:'); t.after(() => store.close());
  const trip = store.createTrip(tripData({ priceBasis: 'total' }));
  await createMonitor(store, { demo: new DemoProvider() }, {}).checkTrip(trip.id);
  assert.equal(store.detail(trip.id).currentPrice, 676000); assert.equal(store.notifications().length, 0);
});
test('time and maximum-price filters yield no matches without fabricating alerts', async t => {
  const store = createStore(':memory:'); t.after(() => store.close());
  const trip = store.createTrip(tripData({ departureWindow: { start: '22:00', end: '23:59' } }));
  await createMonitor(store, { demo: new DemoProvider() }, {}).checkTrip(trip.id);
  assert.equal(store.detail(trip.id).currentPrice, null); assert.equal(store.notifications().length, 0);
});
test('a live-provider error preserves historical fares and records retry status', async t => {
  const store = createStore(':memory:'); t.after(() => store.close());
  const trip = store.createTrip(tripData({ provider: 'amadeus' }));
  store.addCheck(trip, await new DemoProvider().searchFlights(trip));
  const monitor = createMonitor(store, { amadeus: { searchFlights: async () => { throw new ProviderError('API unavailable'); } } }, {});
  await assert.rejects(monitor.checkTrip(trip.id), /API unavailable/);
  assert.equal(store.detail(trip.id).history.length, 1); assert.equal(store.detail(trip.id).currentPrice, 338000);
  assert.match(store.getTrip(trip.id).lastError, /API unavailable/);
  assert.ok(store.getTrip(trip.id).nextCheckAt);
});
test('overlapping checks are rejected and search edits discard stale results', async t => {
  const store = createStore(':memory:'); t.after(() => store.close());
  const trip = store.createTrip(tripData());
  let release;
  const monitor = createMonitor(store, { demo: { searchFlights: () => new Promise(resolve => { release = resolve; }) } }, {});
  const first = monitor.checkTrip(trip.id);
  await assert.rejects(monitor.checkTrip(trip.id), /already running/);
  store.updateTrip(trip.id, tripData({ destination: 'KIX' }));
  release(await new DemoProvider().searchFlights(trip));
  await assert.rejects(first, /Trip changed/);
  assert.equal(store.detail(trip.id).history.length, 0);
  assert.equal(store.db.prepare('SELECT COUNT(*) AS n FROM leases').get().n, 0);
});
test('preference edits preserve fare history; changing provider starts a separate history', async t => {
  const store = createStore(':memory:'); t.after(() => store.close());
  const trip = store.createTrip(tripData());
  await createMonitor(store, { demo: new DemoProvider() }, {}).checkTrip(trip.id);
  store.updateTrip(trip.id, tripData({ targetPrice: 280000 }));
  assert.equal(store.detail(trip.id).history.length, 1);
  store.updateTrip(trip.id, tripData({ provider: 'amadeus' }));
  assert.equal(store.detail(trip.id).history.length, 0);
  assert.equal(store.db.prepare('SELECT COUNT(*) AS n FROM checks WHERE trip_id=?').get(trip.id).n, 1);
});
test('scheduler handles only due active trips and advances their persisted schedule', async t => {
  const store = createStore(':memory:'); t.after(() => store.close());
  const trip = store.createTrip(tripData());
  const paused = store.createTrip(tripData({ name: 'Paused' })); store.setActive(paused.id, false);
  store.db.prepare('UPDATE trips SET next_check=? WHERE id=?').run('2000-01-01T00:00:00.000Z', trip.id);
  await createMonitor(store, { demo: new DemoProvider() }, {}).tick();
  assert.equal(store.history(trip.id).length, 1); assert.equal(store.history(paused.id).length, 0);
  assert.ok(store.getTrip(trip.id).nextCheckAt > new Date().toISOString());
});
test('durable storage survives restart and cascades deletes', async t => {
  const dir = mkdtempSync(join(tmpdir(), 'wayfinder-')); t.after(() => rmSync(dir, { recursive: true, force: true }));
  const path = join(dir, 'test.sqlite'); let store = createStore(path);
  const trip = store.createTrip(tripData({ targetPrice: 400000 }));
  await createMonitor(store, { demo: new DemoProvider() }, {}).checkTrip(trip.id);
  store.saveItinerary(trip.id, { title: 'saved plan' }); store.close(); store = createStore(path);
  try {
    assert.equal(store.detail(trip.id).history.length, 1); assert.equal(store.notifications().length, 1);
    assert.equal(store.detail(trip.id).itinerary.title, 'saved plan');
    store.deleteTrip(trip.id); assert.equal(store.notifications().length, 0); assert.equal(store.detail(trip.id), null);
  } finally { store.close(); }
});
test('demo is seeded once and deleting all trips remains durable', async t => {
  const store = createStore(':memory:'); t.after(() => store.close());
  await seedDemo(store); assert.equal(store.listTrips().length, 2);
  store.listTrips().forEach(trip => store.deleteTrip(trip.id));
  await seedDemo(store); assert.equal(store.listTrips().length, 0);
});

test('changing Amadeus environment does not mix test fares with production history', async t => {
  const store = createStore(':memory:'); t.after(() => store.close());
  const trip = store.createTrip(tripData({ provider: 'amadeus' }));
  const provider = { environment: 'test', searchFlights: (...args) => new DemoProvider().searchFlights(...args) };
  const monitor = createMonitor(store, { amadeus: provider }, {});
  await monitor.checkTrip(trip.id); assert.equal(store.history(trip.id).length, 1);
  provider.environment = 'production'; await monitor.checkTrip(trip.id);
  assert.equal(store.history(trip.id).length, 1); assert.equal(store.getTrip(trip.id).sourceMode, 'amadeus:production');
  assert.equal(store.db.prepare('SELECT COUNT(*) AS n FROM checks WHERE trip_id=?').get(trip.id).n, 2);
});
