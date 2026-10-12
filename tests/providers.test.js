import test from 'node:test';
import assert from 'node:assert/strict';
import { AmadeusProvider, normalizeAmadeusOffer } from '../server/providers.js';
import { rawOffer, tripData } from './fixtures.js';

test('Amadeus normalization uses grand total for the entire party and includes return details', () => {
  const result = normalizeAmadeusOffer(rawOffer(), { KE: 'Korean Air' }, tripData(), 'production');
  assert.equal(result.priceTotal, 556000); assert.equal(result.pricePerPerson, 278000);
  assert.equal(result.airline, 'Korean Air'); assert.equal(result.durationMinutes, 150);
  assert.equal(result.returnStops, 0); assert.match(result.returnDepartureAt, /19:00:00/);
  assert.match(result.arrivalAt, /\+09:00$/); assert.equal(result.isDemo, false);
  assert.equal(result.linkType, 'search'); assert.equal(result.baggage, '23 KG checked');
  assert.throws(() => normalizeAmadeusOffer({ ...rawOffer(), price: { total: '100', currency: 'USD' } }, {}, tripData()), /currency/);
});
test('technical stops on either leg are counted as connections', () => {
  const raw = rawOffer(); raw.itineraries[1].segments[0].numberOfStops = 1;
  assert.equal(normalizeAmadeusOffer(raw, {}, tripData()).returnStops, 1);
});
test('Amadeus caches OAuth tokens and encodes required filters without sending keys to search', async () => {
  const calls = [];
  const provider = new AmadeusProvider({ AMADEUS_CLIENT_ID: 'test-key', AMADEUS_CLIENT_SECRET: 'test-secret', AMADEUS_ENV: 'test' }, async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify(url.includes('/token') ? { access_token: 'token', expires_in: 1800 } : { data: [rawOffer()], dictionaries: { carriers: { KE: 'Korean Air' } } }), { status: 200 });
  });
  const result = await provider.searchFlights(tripData()); await provider.searchFlights(tripData());
  assert.equal(calls.filter(c => c.url.includes('/token')).length, 1);
  const url = new URL(calls[1].url); assert.equal(url.searchParams.get('adults'), '2');
  assert.equal(url.searchParams.get('currencyCode'), 'KRW'); assert.equal(url.searchParams.get('nonStop'), 'true');
  assert.equal(url.searchParams.get('returnDate'), '2030-11-23'); assert.ok(!url.href.includes('test-secret'));
  assert.equal(result[0].isDemo, true);
});
test('unconfigured provider and rate limits fail explicitly, with no demo fallback', async () => {
  await assert.rejects(new AmadeusProvider({}).searchFlights(tripData()), /not configured/);
  const provider = new AmadeusProvider({ AMADEUS_CLIENT_ID: 'key', AMADEUS_CLIENT_SECRET: 'secret' }, async url => url.includes('/token') ? new Response(JSON.stringify({ access_token: 'token', expires_in: 1800 })) : new Response('{}', { status: 429 }));
  await assert.rejects(provider.searchFlights(tripData()), /API limit reached/);
});
test('repricing sends the original offer and returns a current party total', async () => {
  let body;
  const provider = new AmadeusProvider({ AMADEUS_CLIENT_ID: 'key', AMADEUS_CLIENT_SECRET: 'secret' }, async (url, options) => {
    if (url.includes('/token')) return new Response(JSON.stringify({ access_token: 'token', expires_in: 1800 }));
    body = JSON.parse(options.body); return new Response(JSON.stringify({ data: { flightOffers: [{ price: { grandTotal: '600000' } }] } }));
  });
  const offer = normalizeAmadeusOffer(rawOffer(), {}, tripData());
  assert.equal(await provider.getPrice(offer), 600000); assert.equal(body.data.type, 'flight-offers-pricing'); assert.equal(body.data.flightOffers[0].id, '1');
});
