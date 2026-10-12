import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../server/store.js';
import { createMonitor } from '../server/monitor.js';
import { createProviders } from '../server/providers.js';
import { createApp } from '../server/app.js';
import { tripData } from './fixtures.js';

async function setup(t) {
  const store = createStore(':memory:'), providers = createProviders({}), monitor = createMonitor(store, providers, {});
  const server = createApp(store, monitor, providers, {}).listen(0, '127.0.0.1');
  await new Promise(resolve => server.on('listening', resolve));
  t.after(() => new Promise(resolve => server.close(() => { store.close(); resolve(); })));
  const url = `http://127.0.0.1:${server.address().port}/api`;
  const request = (path, method = 'GET', data, headers = {}) => fetch(url + path, { method, headers: { 'Content-Type': 'application/json', ...headers }, body: data === undefined ? undefined : JSON.stringify(data) });
  return { store, request };
}
test('API creates a trip, records a check, sends an alert, saves a plan, pauses, and deletes', async t => {
  const { request } = await setup(t);
  let response = await request('/trips', 'POST', tripData({ targetPrice: 400000 })); assert.equal(response.status, 201);
  const trip = await response.json();
  response = await request(`/trips/${trip.id}/check`, 'POST'); assert.equal(response.status, 200);
  const checked = await response.json(); assert.equal(checked.history.length, 1); assert.equal(checked.offers.length, 4);
  const notifications = await (await request('/notifications')).json(); assert.equal(notifications.length, 1);
  await request(`/notifications/${notifications[0].id}/read`, 'POST');
  assert.equal((await (await request('/notifications')).json())[0].read, true);
  response = await request(`/trips/${trip.id}/itinerary`, 'POST', { flightId: checked.offers[0].id }); assert.equal(response.status, 200);
  assert.equal((await (await request(`/trips/${trip.id}`)).json()).itinerary.mode, 'rule_based');
  const paused = await (await request(`/trips/${trip.id}/status`, 'PATCH', { active: false })).json(); assert.equal(paused.nextCheckAt, null);
  assert.equal((await request(`/trips/${trip.id}`, 'DELETE')).status, 204);
  assert.equal((await request(`/trips/${trip.id}`)).status, 404);
  assert.equal((await (await request('/notifications')).json()).length, 0);
});
test('API rejects invalid input, unavailable sources, unknown flights, and unrelated origins', async t => {
  const { request } = await setup(t);
  assert.equal((await request('/trips', 'POST', {})).status, 400);
  assert.equal((await request('/trips', 'POST', tripData({ provider: 'amadeus' }))).status, 422);
  assert.equal((await request('/trips', 'POST', tripData(), { Origin: 'https://unrelated.example' })).status, 403);
  const trip = await (await request('/trips', 'POST', tripData())).json();
  assert.equal((await request(`/trips/${trip.id}/itinerary`, 'POST', { flightId: 'unknown' })).status, 422);
  assert.equal((await request('/trips/no-such-trip/check', 'POST')).status, 404);
  const meta = await (await request('/meta')).json(); assert.equal(meta.aiConfigured, false);
  assert.ok(!JSON.stringify(meta).includes('CLIENT_SECRET'));
});
