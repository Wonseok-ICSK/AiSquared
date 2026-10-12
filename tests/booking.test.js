import test from 'node:test';
import assert from 'node:assert/strict';
import { bookingSearchContext, googleFlightSearchUrl } from '../shared/flight-search.js';
import { createStore } from '../server/store.js';
import { DemoProvider, normalizeAmadeusOffer } from '../server/providers.js';
import { tripData, rawOffer } from './fixtures.js';

const payload = url => Buffer.from(new URL(url).searchParams.get('tfs'), 'base64url').toString('utf8');

test('booking uses the selected offer airports and local dates, including a one-way offer', () => {
  const trip = tripData();
  const offer = { origin: 'GMP', destination: 'HND', departureAt: '2030-11-19T00:20:00+09:00', returnDepartureAt: '2030-11-24T19:00:00+09:00', cabin: 'BUSINESS' };
  const context = bookingSearchContext(trip, offer);
  assert.deepEqual(context, { origin: 'GMP', destination: 'HND', departureDate: '2030-11-19', returnDate: '2030-11-24', passengers: 2, cabin: 'BUSINESS' });
  const url = googleFlightSearchUrl(context);
  assert.equal(new URL(url).pathname, '/travel/flights/search');
  assert.match(payload(url), /2030-11-19/); assert.match(payload(url), /2030-11-24/);
  assert.doesNotMatch(payload(url), /2030-11-20|2030-11-23|ICN|NRT/);
  const oneWay = googleFlightSearchUrl(bookingSearchContext(trip, { ...offer, returnDepartureAt: null }));
  assert.doesNotMatch(payload(oneWay), /2030-11-24|2030-11-23/);
});

test('missing or invalid booking dates cannot become misleading search links', () => {
  for (const change of [{ origin: 'Seoul' }, { departureDate: '' }, { departureDate: '2030-02-30' }, { returnDate: '2030-11-19' }, { returnDate: '2030-99-01' }]) {
    assert.equal(googleFlightSearchUrl({ ...tripData(), ...change }), null);
  }
  assert.equal(googleFlightSearchUrl(), null);
});

test('stored legacy links are repaired when read without rewriting history or alerts', async () => {
  const store = createStore(':memory:');
  try {
    const trip = store.createTrip(tripData({ cabin: 'BUSINESS' }));
    const [offer] = await new DemoProvider().searchFlights(trip);
    const oldUrl = 'https://www.google.com/travel/flights?q=ignored-search';
    offer.bookingUrl = oldUrl;
    store.addCheck(trip, [offer], ['target']);
    const alert = store.db.prepare('SELECT * FROM notifications').get();
    const oldData = JSON.parse(alert.data); delete oldData.cabin; oldData.bookingUrl = oldUrl;
    store.db.prepare('UPDATE notifications SET data=? WHERE id=?').run(JSON.stringify(oldData), alert.id);
    const detail = store.detail(trip.id);
    const notification = store.notifications()[0];
    assert.equal(detail.offers[0].bookingUrl, googleFlightSearchUrl(bookingSearchContext(trip, offer)));
    assert.equal(notification.cabin, 'BUSINESS');
    assert.equal(notification.bookingUrl, detail.offers[0].bookingUrl);
    assert.equal(notification.id, alert.id); assert.equal(detail.history.length, 1);
    assert.equal(JSON.parse(store.db.prepare('SELECT offers FROM checks').get().offers)[0].bookingUrl, oldUrl);
    assert.equal(JSON.parse(store.db.prepare('SELECT data FROM notifications').get().data).bookingUrl, oldUrl);
  } finally { store.close(); }
});

test('provider links use actual returned dates and airports for flexible searches', () => {
  const raw = rawOffer();
  raw.itineraries[0].segments[0].departure = { iataCode: 'GMP', at: '2030-11-19T11:25:00' };
  raw.itineraries[0].segments[0].arrival.iataCode = 'HND';
  raw.itineraries[1].segments[0].departure = { iataCode: 'HND', at: '2030-11-24T19:00:00' };
  raw.itineraries[1].segments[0].arrival.iataCode = 'GMP';
  const offer = normalizeAmadeusOffer(raw, {}, tripData());
  assert.match(payload(offer.bookingUrl), /2030-11-19/); assert.match(payload(offer.bookingUrl), /2030-11-24/);
  assert.doesNotMatch(payload(offer.bookingUrl), /2030-11-20|2030-11-23|ICN|NRT/);
});
