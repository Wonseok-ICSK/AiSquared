// Google Flights' shared search URL format, observed on its public UI on 2026-10-08.
// Keep the opt-in external browser check: query-text URLs silently lose the dates.
const cabinNumbers = { ECONOMY: 1, PREMIUM_ECONOMY: 2, BUSINESS: 3, FIRST: 4 };
const encoder = new TextEncoder();
const varint = value => {
  const bytes = [];
  do { const next = value % 128; value = Math.floor(value / 128); bytes.push(next | (value ? 128 : 0)); } while (value);
  return bytes;
};
const number = (field, value) => [...varint(field * 8), ...varint(value)];
const message = (field, bytes) => [...varint(field * 8 + 2), ...varint(bytes.length), ...bytes];
const text = (field, value) => message(field, [...encoder.encode(value)]);
const airport = code => [...number(1, 1), ...text(2, code)];
const selected = segment => message(4, [...text(1, segment.origin), ...text(2, segment.departureAt.slice(0, 10)), ...text(3, segment.destination), ...text(5, segment.airlineCode), ...text(6, segment.number)]);
const leg = (origin, destination, date, segments = [], directOnly = false) => message(3, [...text(2, date), ...segments.flatMap(selected), ...(directOnly ? number(5, 0) : []), ...message(13, airport(origin)), ...message(14, airport(destination))]);
const validDate = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;

export function bookingSearchContext(trip, offer = {}) {
  return {
    origin: offer.origin || trip.origin, destination: offer.destination || trip.destination,
    departureDate: offer.departureAt?.slice(0, 10) || trip.departureDate,
    returnDate: offer.returnDate || (Object.hasOwn(offer, 'returnDepartureAt') ? offer.returnDepartureAt?.slice(0, 10) || null : trip.returnDate || null),
    passengers: trip.passengers, cabin: offer.cabin || trip.cabin,
  };
}

export function googleFlightSearchUrl(search = {}, { outbound = [], inbound = [], mode = 'search', directOnly = false } = {}) {
  if (!/^[A-Z]{3}$/.test(search.origin || '') || !/^[A-Z]{3}$/.test(search.destination || '') || !validDate(search.departureDate)) return null;
  if (search.returnDate && (!validDate(search.returnDate) || search.returnDate < search.departureDate)) return null;
  const returning = Boolean(search.returnDate);
  const adults = Math.min(2, Math.max(1, Number(search.passengers) || 1));
  const bytes = [
    ...number(1, 28), ...number(2, 2), ...leg(search.origin, search.destination, search.departureDate, outbound, directOnly),
    ...(returning ? leg(search.destination, search.origin, search.returnDate, inbound, directOnly) : []),
    ...Array.from({ length: adults }, () => number(8, 1)).flat(),
    ...number(9, cabinNumbers[search.cabin] || 1), ...number(14, 1), ...number(19, returning ? 1 : 2),
  ];
  const tfs = btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
  const url = new URL(`https://www.google.com/travel/flights${mode === 'fetch' ? '' : mode === 'booking' ? '/booking' : '/search'}`);
  url.search = new URLSearchParams({ tfs, hl: mode === 'fetch' ? 'en' : 'ko', gl: 'KR', curr: 'KRW' });
  if (mode !== 'booking') url.searchParams.set('tfu', 'EgQIABABIgA');
  return url.href;
}

export function offerBookingUrl(trip, offer) {
  const search = bookingSearchContext(trip, offer);
  if (offer.source !== 'Google Flights') return googleFlightSearchUrl(search);
  return googleFlightSearchUrl(search, { outbound: offer.segments, inbound: offer.returnSegments || [], mode: !search.returnDate || offer.returnSegments?.length ? 'booking' : 'search', directOnly: trip.directOnly });
}
