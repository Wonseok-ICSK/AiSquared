import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { nextCheck, basisPrice } from './model.js';
import { bookingSearchContext, googleFlightSearchUrl, offerBookingUrl } from '../shared/flight-search.js';

export function createStore(path = process.env.DATABASE_PATH || './data/wayfinder.sqlite') {
  if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true });
  const db = new DatabaseSync(path);
  db.exec(`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
    CREATE TABLE IF NOT EXISTS trips (id TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL, active INTEGER NOT NULL, created_at TEXT NOT NULL, next_check TEXT, last_error TEXT);
    CREATE TABLE IF NOT EXISTS checks (id TEXT PRIMARY KEY, trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE, version INTEGER NOT NULL, checked_at TEXT NOT NULL, price REAL, offers TEXT NOT NULL, warning TEXT);
    CREATE INDEX IF NOT EXISTS checks_trip ON checks(trip_id, version, checked_at);
    CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE, data TEXT NOT NULL, created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS itineraries (trip_id TEXT PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE, data TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS trips_due ON trips(active, next_check);
    CREATE TABLE IF NOT EXISTS leases (trip_id TEXT PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE, expires_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);`);
  const decodeTrip = row => row ? { ...JSON.parse(row.data), id: row.id, version: row.version, active: Boolean(row.active), createdAt: row.created_at, nextCheckAt: row.next_check, lastError: row.last_error } : null;
  const getTrip = id => decodeTrip(db.prepare('SELECT * FROM trips WHERE id=?').get(id));
  const getHistory = id => {
    const trip = getTrip(id);
    if (!trip) return [];
    return db.prepare('SELECT * FROM checks WHERE trip_id=? AND version=? ORDER BY checked_at, rowid').all(id, trip.version).map(row => ({ id: row.id, checkedAt: row.checked_at, price: row.price, offers: JSON.parse(row.offers).map(offer => ({ ...offer, bookingUrl: offerBookingUrl(trip, offer) })), warning: row.warning }));
  };
  const summarize = trip => {
    const history = getHistory(trip.id);
    const priced = history.filter(h => h.price != null);
    const latest = history.at(-1);
    return { ...trip, currentPrice: latest?.price ?? null, previousPrice: priced.at(-2)?.price ?? null, lowestPrice: priced.length ? Math.min(...priced.map(h => h.price)) : null, lastCheckedAt: latest?.checkedAt ?? null, checkCount: history.length, offerCount: latest?.offers.length || 0 };
  };
  return {
    db, close: () => db.close(), getTrip,
    demoSeeded: () => Boolean(db.prepare("SELECT value FROM app_meta WHERE key='demo_seeded'").get()),
    markDemoSeeded: () => db.prepare("INSERT OR REPLACE INTO app_meta VALUES ('demo_seeded', 'true')").run(),
    acquireLease: id => {
      const now = new Date().toISOString(), until = new Date(Date.now() + 600000).toISOString();
      return Boolean(db.prepare('INSERT INTO leases VALUES (?, ?) ON CONFLICT(trip_id) DO UPDATE SET expires_at=excluded.expires_at WHERE leases.expires_at < ?').run(id, until, now).changes);
    },
    releaseLease: id => db.prepare('DELETE FROM leases WHERE trip_id=?').run(id),
    listTrips: () => db.prepare('SELECT * FROM trips ORDER BY created_at').all().map(decodeTrip).map(summarize),
    detail: id => {
      const trip = getTrip(id); if (!trip) return null;
      const history = getHistory(id);
      const itinerary = db.prepare('SELECT * FROM itineraries WHERE trip_id=?').get(id);
      return { ...summarize(trip), history: history.map(({ offers, ...h }) => ({ ...h, airline: offers[0]?.airline, flightNumber: offers[0]?.flightNumber })), offers: history.at(-1)?.offers || [], warning: history.at(-1)?.warning || null, itinerary: itinerary ? JSON.parse(itinerary.data) : null };
    },
    createTrip: (data, opts = {}) => {
      const id = opts.id || randomUUID(), now = new Date().toISOString();
      data = { ...data, sourceMode: data.provider === 'demo' ? 'demo' : data.provider === 'google_flights' ? 'google_flights:live' : 'amadeus:test' };
      const next = nextCheck({ ...data, active: true });
      db.prepare('INSERT INTO trips VALUES (?, ?, 1, 1, ?, ?, NULL)').run(id, JSON.stringify(data), now, next);
      return getTrip(id);
    },
    updateTrip: (id, data) => {
      const old = getTrip(id); if (!old) return null;
      data = { ...data, sourceMode: data.provider === old.provider ? old.sourceMode : data.provider === 'demo' ? 'demo' : data.provider === 'google_flights' ? 'google_flights:live' : 'amadeus:test' };
      const searchKeys = ['origin', 'destination', 'departureDate', 'returnDate', 'tripType', 'passengers', 'cabin', 'directOnly', 'departureWindow', 'arrivalWindow', 'flexDeparture', 'flexReturn', 'nearbyAirports', 'alternativeDestinations', 'priceBasis', 'provider', 'maxPrice'];
      const changed = searchKeys.some(k => JSON.stringify(old[k]) !== JSON.stringify(data[k]));
      db.prepare('UPDATE trips SET data=?, version=?, next_check=?, last_error=NULL WHERE id=?').run(JSON.stringify(data), old.version + Number(changed), nextCheck({ ...data, active: old.active }), id);
      db.prepare('DELETE FROM itineraries WHERE trip_id=?').run(id);
      return getTrip(id);
    },
    setSourceMode: (id, mode) => {
      const trip = getTrip(id); if (!trip || trip.sourceMode === mode) return trip;
      const changed = Boolean(trip.sourceMode && trip.sourceMode !== mode);
      const { id: _id, version, active: _active, createdAt, nextCheckAt, lastError, ...data } = trip;
      db.prepare('UPDATE trips SET data=?, version=? WHERE id=?').run(JSON.stringify({ ...data, sourceMode: mode }), version + Number(changed), id);
      if (changed) db.prepare('DELETE FROM itineraries WHERE trip_id=?').run(id);
      return getTrip(id);
    },
    setActive: (id, active) => {
      const trip = getTrip(id); if (!trip) return null;
      db.prepare('UPDATE trips SET active=?, next_check=? WHERE id=?').run(Number(active), nextCheck({ ...trip, active }), id);
      return getTrip(id);
    },
    deleteTrip: id => db.prepare('DELETE FROM trips WHERE id=?').run(id).changes,
    history: getHistory,
    dueTrips: (now = new Date().toISOString()) => db.prepare('SELECT * FROM trips WHERE active=1 AND next_check IS NOT NULL AND next_check <= ?').all(now).map(decodeTrip),
    addCheck: (trip, offers, reasons = [], checkedAt = new Date().toISOString(), warning = null) => {
      if (getTrip(trip.id)?.version !== trip.version) throw new Error('Trip changed while searching. Run a fresh check.');
      const id = randomUUID(), price = offers.length ? basisPrice(offers[0].priceTotal, trip) : null;
      const previous = getHistory(trip.id).filter(h => h.price != null).at(-1)?.price ?? null;
      const prices = getHistory(trip.id).filter(h => h.price != null).map(h => h.price);
      db.exec('BEGIN IMMEDIATE');
      try {
        db.prepare('INSERT INTO checks VALUES (?, ?, ?, ?, ?, ?, ?)').run(id, trip.id, trip.version, checkedAt, price, JSON.stringify(offers), warning);
        const current = getTrip(trip.id);
        db.prepare('UPDATE trips SET next_check=?, last_error=NULL WHERE id=?').run(nextCheck(current), trip.id);
        if (reasons.length) {
          const notification = { tripName: trip.name, origin: offers[0].origin, destination: offers[0].destination, departureDate: offers[0].departureAt.slice(0, 10), returnDate: offers[0].returnDate || offers[0].returnDepartureAt?.slice(0, 10) || null, price, previousPrice: previous, previousLowest: prices.length ? Math.min(...prices) : null, targetPrice: trip.targetPrice, priceBasis: trip.priceBasis, passengers: trip.passengers, cabin: offers[0].cabin || trip.cabin, reasons, source: offers[0].source, isDemo: offers[0].isDemo, bookingUrl: googleFlightSearchUrl(bookingSearchContext(trip, offers[0])), linkType: offers[0].linkType };
          db.prepare('INSERT INTO notifications VALUES (?, ?, ?, ?, 0)').run(randomUUID(), trip.id, JSON.stringify(notification), checkedAt);
        }
        db.exec('COMMIT');
      } catch (error) { db.exec('ROLLBACK'); throw error; }
      return { id, price };
    },
    recordError: (id, message) => {
      const trip = getTrip(id); if (!trip) return;
      const retry = new Date(Date.now() + 60 * 60 * 1000).toISOString();
      db.prepare('UPDATE trips SET last_error=?, next_check=? WHERE id=?').run(message, trip.active ? retry : null, id);
    },
    notifications: () => db.prepare('SELECT * FROM notifications ORDER BY created_at DESC, rowid DESC').all().map(n => {
      const data = JSON.parse(n.data);
      const cabin = data.cabin || getTrip(n.trip_id)?.cabin || 'ECONOMY';
      return { ...data, cabin, bookingUrl: googleFlightSearchUrl({ ...data, cabin }), id: n.id, tripId: n.trip_id, createdAt: n.created_at, read: Boolean(n.read) };
    }),
    readNotification: id => db.prepare('UPDATE notifications SET read=1 WHERE id=?').run(id).changes,
    readAllNotifications: () => db.prepare('UPDATE notifications SET read=1').run(),
    saveItinerary: (id, itinerary) => db.prepare('INSERT OR REPLACE INTO itineraries VALUES (?, ?, ?)').run(id, JSON.stringify(itinerary), new Date().toISOString()),
  };
}
