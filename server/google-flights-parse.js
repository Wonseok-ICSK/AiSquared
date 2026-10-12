// Adapted from travel-planner/backend/app/providers/flights/google_flights_parse.py.
// Field map verified against that project's captured Google pages and live Edge.
// Google is not an official API: reject a changed format rather than invent fares.
import { createHash } from 'node:crypto';
import { DateTime } from 'luxon';
import { airportInfo } from './model.js';
import { ProviderError } from './provider-base.js';
import { offerBookingUrl } from '../shared/flight-search.js';

export function googleBlockReason(html, url = '', status = 200) {
  if ([403, 429].includes(status)) return `HTTP ${status}`;
  if (/\/sorry\/|consent\.google\.com/.test(url)) return 'Google access check';
  if (/id=["']captcha-form|Our systems have detected unusual traffic|Before you continue to Google/.test(html)) return 'Google access check';
  return null;
}
export function extractGoogleData(html) {
  const script = /<script\b[^>]*\bclass=["']ds:1["'][^>]*>([\s\S]*?)<\/script>/.exec(html)?.[1];
  const start = script && /\bdata\s*:\s*(\[)/.exec(script);
  if (!start) throw new ProviderError('Google Flights page format changed. Open the search link to check prices.');
  if (/errorHasStatus\s*:\s*true/.test(script)) return [];
  const from = start.index + start[0].length - 1;
  let depth = 0, quoted = false, escaped = false;
  for (let i = from; i < script.length; i++) {
    const char = script[i];
    if (quoted) { if (escaped) escaped = false; else if (char === '\\') escaped = true; else if (char === '"') quoted = false; }
    else if (char === '"') quoted = true;
    else if (char === '[') depth++;
    else if (char === ']' && --depth === 0) {
      try { return JSON.parse(script.slice(from, i + 1)); } catch { break; }
    }
  }
  throw new ProviderError('Google Flights page format changed. Open the search link to check prices.');
}
const iata = value => { if (typeof value !== 'string' || !/^[A-Z]{3}$/.test(value)) throw new Error('Invalid airport'); return value; };
function localTime(day, clock, airport) {
  if (!Array.isArray(day) || day.length !== 3) throw new Error('Missing flight date');
  const time = DateTime.fromObject({ year: day[0], month: day[1], day: day[2], hour: clock?.[0] ?? 0, minute: clock?.[1] ?? 0 }, { zone: airportInfo(airport).zone });
  if (!time.isValid) throw new Error('Invalid flight time');
  return airportInfo(airport).zone === 'UTC' ? time.toFormat("yyyy-MM-dd'T'HH:mm:ss") : time.toISO();
}
export function parseGoogleLegs(payload) {
  if (!Array.isArray(payload)) throw new ProviderError('Google Flights page format changed. Open the search link to check prices.');
  if (!payload.length) return [];
  if (payload.length < 4) throw new ProviderError('Google Flights page format changed. Open the search link to check prices.');
  const legs = [], rows = [];
  for (const index of [2, 3]) {
    const group = payload[index]?.[0];
    if (group == null) continue;
    if (!Array.isArray(group)) throw new ProviderError('Google Flights page format changed. Open the search link to check prices.');
    rows.push(...group);
  }
  let readable = 0;
  for (const row of rows) {
    try {
      const raw = row[0];
      if (!Array.isArray(raw?.[2]) || !raw[2].length) continue;
      const segments = raw[2].map(s => {
        const origin = iata(s[3]), destination = iata(s[6]);
        const airlineCode = s[22]?.[0], number = String(s[22]?.[1] ?? '');
        if (!/^[A-Z0-9]{2}$/.test(airlineCode || '') || !/^\d{1,4}[A-Z]?$/.test(number)) throw new Error('Missing flight number');
        return { origin, destination, departureAt: localTime(s[20], s[8], origin), arrivalAt: localTime(s[21], s[10], destination), airlineCode, number, flightNumber: `${airlineCode} ${number}` };
      });
      const origin = iata(raw[3]), destination = iata(raw[6]);
      const departureAt = localTime(raw[4], raw[5], origin), arrivalAt = localTime(raw[7], raw[8], destination);
      if (segments[0].origin !== origin || segments.at(-1).destination !== destination) continue;
      readable++;
      const priceTotal = row[1]?.[0]?.[1];
      if (typeof priceTotal !== 'number' || !Number.isFinite(priceTotal) || priceTotal <= 0) continue;
      const durationMinutes = Number.isFinite(raw[9]) && raw[9] > 0 ? raw[9] : null;
      legs.push({ origin, destination, departureAt, arrivalAt, segments, priceTotal, durationMinutes, stops: segments.length - 1, airline: Array.isArray(raw[1]) ? raw[1].filter(x => typeof x === 'string').join(' / ') : segments[0].airlineCode, airlineCode: segments[0].airlineCode, flightNumber: segments.map(s => s.flightNumber).join(' / ') });
    } catch { /* Malformed rows cannot become fares. */ }
  }
  if (rows.length && !readable) throw new ProviderError('Google Flights page format changed. Open the search link to check prices.');
  return legs;
}
export function parseGoogleOffers(payload, trip, observedAt = new Date().toISOString()) {
  const search = Object.fromEntries(['origin', 'destination', 'departureDate', 'returnDate', 'tripType', 'passengers', 'cabin', 'directOnly'].map(k => [k, trip[k]]));
  const offers = new Map();
  for (const leg of parseGoogleLegs(payload)) {
    if (leg.origin !== trip.origin || leg.destination !== trip.destination || leg.departureAt.slice(0, 10) !== trip.departureDate || (trip.directOnly && leg.stops !== 0)) continue;
    const returnDate = trip.tripType === 'round_trip' ? trip.returnDate : null;
    const id = createHash('sha256').update(JSON.stringify([leg.segments, returnDate, trip.cabin])).digest('hex').slice(0, 20);
    const offer = { ...leg, id, search, returnDate, returnDepartureAt: null, returnArrivalAt: null, returnStops: returnDate ? null : 0, returnDetailsPending: Boolean(returnDate), cabin: trip.cabin, fareType: returnDate ? '왕복 최저 표시 요금 · 귀국편 선택 전' : '검색 시점 표시 요금', baggage: 'Not provided', currency: 'KRW', pricePerPerson: leg.priceTotal / trip.passengers, passengers: trip.passengers, source: 'Google Flights', isDemo: false, observedAt, linkType: 'selected_search' };
    offer.bookingUrl = offerBookingUrl(trip, offer);
    if (!offers.has(id) || offer.priceTotal < offers.get(id).priceTotal) offers.set(id, offer);
  }
  return [...offers.values()].sort((a, b) => a.priceTotal - b.priceTotal);
}
