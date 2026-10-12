import { createHash } from 'node:crypto';
import { DateTime } from 'luxon';
import { airportInfo } from './model.js';
import { googleFlightSearchUrl } from '../shared/flight-search.js';
import { FlightProvider, ProviderError } from './provider-base.js';
import { GoogleFlightsProvider } from './google-flights.js';
export { FlightProvider, ProviderError } from './provider-base.js';

export function routeSearchUrl(trip) {
  return googleFlightSearchUrl(trip);
}
const hash = text => createHash('sha256').update(text).digest('hex').slice(0, 20);
export class DemoProvider extends FlightProvider {
  async searchFlights(trip, context = {}) {
    const prices = [338000, 326000, 319000, 307000, 298000, 289000, 303000, 278000, 291000, 272000];
    const cyclePrice = prices[(context.checkIndex || 0) % prices.length];
    const cabinFactor = { ECONOMY: 1, PREMIUM_ECONOMY: 1.6, BUSINESS: 2.8, FIRST: 4.5 }[trip.cabin];
    const routeFactor = ['NRT', 'HND', 'KIX', 'FUK', 'ICN', 'GMP'].includes(trip.destination) ? 1 : 2.2;
    const airlines = [{ code: '7C', name: 'Jeju Air', hour: 11, minute: 25, fare: 0 }, { code: 'KE', name: 'Korean Air', hour: 9, minute: 10, fare: 42000 }, { code: 'OZ', name: 'Asiana Airlines', hour: 14, minute: 30, fare: 27000 }, { code: 'TW', name: 'T’way Air', hour: 7, minute: 45, fare: 18000 }];
    const duration = routeFactor === 1 ? 150 : 420;
    return airlines.map((airline, i) => {
      const stops = !trip.directOnly && i === 3 ? 1 : 0;
      const minutes = duration + stops * 150;
      const departure = DateTime.fromISO(trip.departureDate, { zone: airportInfo(trip.origin).zone }).set({ hour: airline.hour, minute: airline.minute });
      const arrival = departure.plus({ minutes }).setZone(airportInfo(trip.destination).zone);
      const returnDeparture = trip.tripType === 'round_trip' ? DateTime.fromISO(trip.returnDate, { zone: airportInfo(trip.destination).zone }).set({ hour: 19, minute: 0 }) : null;
      const pricePerPerson = Math.round((cyclePrice + airline.fare) * cabinFactor * routeFactor * (returnDeparture ? 1 : 0.6));
      return {
        id: hash(`${trip.origin}-${trip.destination}-${trip.departureDate}-${trip.returnDate}-${airline.code}-${trip.cabin}`),
        airline: airline.name, airlineCode: airline.code, flightNumber: `${airline.code} ${1101 + i * 12}`,
        origin: trip.origin, destination: trip.destination, departureAt: departure.toISO(), arrivalAt: arrival.toISO(),
        returnDepartureAt: returnDeparture?.toISO() || null, returnArrivalAt: returnDeparture?.plus({ minutes }).setZone(airportInfo(trip.origin).zone).toISO() || null,
        durationMinutes: minutes, stops, returnStops: stops, cabin: trip.cabin, fareType: 'Sample standard fare', baggage: i === 0 ? '15 kg checked · sample' : '23 kg checked · sample',
        currency: 'KRW', priceTotal: pricePerPerson * trip.passengers, pricePerPerson,
        bookingUrl: routeSearchUrl(trip), linkType: 'search', source: 'Demo data', isDemo: true,
        segments: [{ origin: trip.origin, destination: trip.destination, departureAt: departure.toISO(), arrivalAt: arrival.toISO(), flightNumber: `${airline.code} ${1101 + i * 12}` }],
      };
    });
  }
}
function durationMinutes(text) {
  const m = /^PT(?:(\d+)H)?(?:(\d+)M)?$/.exec(text || '');
  return m ? Number(m[1] || 0) * 60 + Number(m[2] || 0) : null;
}
function localTimestamp(text, code) {
  const known = airportInfo(code);
  return known.zone === 'UTC' ? text : DateTime.fromISO(text, { zone: known.zone }).toISO();
}
export function normalizeAmadeusOffer(offer, carriers, trip, environment = 'test') {
  const outbound = offer.itineraries?.[0], inbound = offer.itineraries?.[1];
  if (!outbound?.segments?.length) throw new ProviderError('Flight provider returned an incomplete itinerary.');
  const first = outbound.segments[0], last = outbound.segments.at(-1);
  const total = Number(offer.price?.grandTotal || offer.price?.total);
  if (!Number.isFinite(total) || total <= 0 || offer.price?.currency !== 'KRW') throw new ProviderError('Provider returned an invalid fare or unexpected currency.');
  const fare = offer.travelerPricings?.[0]?.fareDetailsBySegment?.[0];
  const baggage = fare?.includedCheckedBags;
  const stops = segments => segments.reduce((n, s) => n + (s.numberOfStops || 0), segments.length - 1);
  const search = { ...trip, origin: first.departure.iataCode, destination: last.arrival.iataCode, departureDate: first.departure.at.slice(0, 10), returnDate: inbound?.segments[0]?.departure.at.slice(0, 10) || null, cabin: fare?.cabin || trip.cabin };
  return {
    id: hash(JSON.stringify({ segments: offer.itineraries.map(i => i.segments.map(s => [s.carrierCode, s.number, s.departure.at, s.arrival.at, s.departure.iataCode, s.arrival.iataCode])), cabin: fare?.cabin, fare: fare?.fareBasis })),
    airline: carriers[first.carrierCode] || first.carrierCode, airlineCode: first.carrierCode,
    flightNumber: outbound.segments.map(s => `${s.carrierCode} ${s.number}`).join(' / '),
    origin: first.departure.iataCode, destination: last.arrival.iataCode,
    departureAt: localTimestamp(first.departure.at, first.departure.iataCode), arrivalAt: localTimestamp(last.arrival.at, last.arrival.iataCode),
    returnDepartureAt: inbound ? localTimestamp(inbound.segments[0].departure.at, inbound.segments[0].departure.iataCode) : null,
    returnArrivalAt: inbound ? localTimestamp(inbound.segments.at(-1).arrival.at, inbound.segments.at(-1).arrival.iataCode) : null,
    durationMinutes: durationMinutes(outbound.duration), stops: stops(outbound.segments), returnStops: inbound ? stops(inbound.segments) : 0,
    cabin: fare?.cabin || trip.cabin, fareType: fare?.brandedFare || fare?.fareBasis || 'Fare rules unavailable',
    baggage: baggage?.weight != null ? `${baggage.weight} ${baggage.weightUnit || 'kg'} checked` : baggage?.quantity != null ? `${baggage.quantity} checked bag(s)` : 'Not provided',
    currency: 'KRW', priceTotal: total, pricePerPerson: total / trip.passengers,
    bookingUrl: routeSearchUrl(search), linkType: 'search', source: environment === 'production' ? 'Amadeus' : 'Amadeus test data', isDemo: environment !== 'production',
    segments: outbound.segments.map(s => ({ origin: s.departure.iataCode, destination: s.arrival.iataCode, departureAt: localTimestamp(s.departure.at, s.departure.iataCode), arrivalAt: localTimestamp(s.arrival.at, s.arrival.iataCode), flightNumber: `${s.carrierCode} ${s.number}` })),
    providerOffer: offer,
  };
}
export class AmadeusProvider extends FlightProvider {
  constructor(config = process.env, fetchImpl = fetch) {
    super(); this.config = config; this.fetch = fetchImpl;
    this.environment = config.AMADEUS_ENV === 'production' ? 'production' : 'test';
    this.base = this.environment === 'production' ? 'https://api.amadeus.com' : 'https://test.api.amadeus.com';
  }
  get configured() { return Boolean(this.config.AMADEUS_CLIENT_ID && this.config.AMADEUS_CLIENT_SECRET); }
  async token() {
    if (!this.configured) throw new ProviderError('Amadeus is not configured. Add API credentials on the server, then restart.', 503);
    if (this.access && Date.now() < this.expires) return this.access;
    if (this.tokenRequest) return this.tokenRequest;
    this.tokenRequest = (async () => {
      const response = await this.fetch(`${this.base}/v1/security/oauth2/token`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ grant_type: 'client_credentials', client_id: this.config.AMADEUS_CLIENT_ID, client_secret: this.config.AMADEUS_CLIENT_SECRET }), signal: AbortSignal.timeout(15000) });
      if (!response.ok) throw new ProviderError(`Amadeus authentication failed (${response.status}). Check server credentials.`, 503);
      const data = await response.json();
      if (!data.access_token) throw new ProviderError('Amadeus did not return an access token.');
      this.access = data.access_token; this.expires = Date.now() + Math.max(0, Number(data.expires_in || 0) - 60) * 1000;
      return this.access;
    })();
    try { return await this.tokenRequest; } finally { this.tokenRequest = null; }
  }
  async request(path, options = {}) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const access = await this.token();
      const response = await this.fetch(`${this.base}${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers, Authorization: `Bearer ${access}` }, signal: AbortSignal.timeout(15000) });
      if (response.status === 401 && attempt === 0) { this.access = null; continue; }
      if (!response.ok) throw new ProviderError(`Amadeus search failed (${response.status}). ${response.status === 429 ? 'API limit reached; try later.' : 'Check dates, provider access, and server configuration.'}`);
      return response.json();
    }
  }
  async searchFlights(trip) {
    const params = new URLSearchParams({ originLocationCode: trip.origin, destinationLocationCode: trip.destination, departureDate: trip.departureDate, adults: String(trip.passengers), travelClass: trip.cabin, nonStop: String(trip.directOnly), currencyCode: 'KRW', max: '30' });
    if (trip.tripType === 'round_trip') params.set('returnDate', trip.returnDate);
    const data = await this.request(`/v2/shopping/flight-offers?${params}`);
    return (data.data || []).map(offer => normalizeAmadeusOffer(offer, data.dictionaries?.carriers || {}, trip, this.environment));
  }
  async getPrice(offer) {
    if (!offer.providerOffer) throw new ProviderError('No provider offer is available for repricing.');
    const data = await this.request('/v1/shopping/flight-offers/pricing', { method: 'POST', body: JSON.stringify({ data: { type: 'flight-offers-pricing', flightOffers: [offer.providerOffer] } }) });
    const price = Number(data.data?.flightOffers?.[0]?.price?.grandTotal || data.data?.flightOffers?.[0]?.price?.total);
    if (!Number.isFinite(price) || price <= 0) throw new ProviderError('Provider could not confirm the current fare.');
    return price;
  }
}
export function createProviders(config = process.env) { return { demo: new DemoProvider(), amadeus: new AmadeusProvider(config), google_flights: new GoogleFlightsProvider(config) }; }
