import { z } from 'zod';
import { DateTime } from 'luxon';

export const AIRPORTS = [
  { code: 'ICN', city: 'Seoul', name: 'Incheon', zone: 'Asia/Seoul' },
  { code: 'GMP', city: 'Seoul', name: 'Gimpo', zone: 'Asia/Seoul' },
  { code: 'NRT', city: 'Tokyo', name: 'Narita', zone: 'Asia/Tokyo' },
  { code: 'HND', city: 'Tokyo', name: 'Haneda', zone: 'Asia/Tokyo' },
  { code: 'KIX', city: 'Osaka', name: 'Kansai', zone: 'Asia/Tokyo' },
  { code: 'ITM', city: 'Osaka', name: 'Itami', zone: 'Asia/Tokyo' },
  { code: 'FUK', city: 'Fukuoka', name: 'Fukuoka', zone: 'Asia/Tokyo' },
  { code: 'BKK', city: 'Bangkok', name: 'Suvarnabhumi', zone: 'Asia/Bangkok' },
  { code: 'SIN', city: 'Singapore', name: 'Changi', zone: 'Asia/Singapore' },
  { code: 'CDG', city: 'Paris', name: 'Charles de Gaulle', zone: 'Europe/Paris' },
  { code: 'LHR', city: 'London', name: 'Heathrow', zone: 'Europe/London' },
  { code: 'JFK', city: 'New York', name: 'John F. Kennedy', zone: 'America/New_York' },
];
export const STYLES = ['sightseeing', 'food', 'relaxation', 'resort', 'shopping', 'nightlife', 'nature', 'photography', 'culture', 'local'];
const airport = z.string().trim().toUpperCase().regex(/^[A-Z]{3}$/, 'Use a 3-letter IATA airport or city code.');
const date = z.string().regex(/^\d{4}-\d{2}-\d{2}$/).refine(v => DateTime.fromISO(v).isValid, 'Enter a valid date.');
const time = z.string().regex(/^([01]\d|2[0-3]):[0-5]\d$/);
const amount = z.number().int().min(-1).max(1_000_000_000).refine(v => v >= 0 || v === -1, 'Use a positive amount, 0, or -1.');
const window = z.object({ start: time, end: time });
export const TripSchema = z.object({
  name: z.string().trim().min(1).max(80),
  origin: airport, destination: airport,
  departureDate: date, returnDate: date.nullable().default(null),
  tripType: z.enum(['round_trip', 'one_way']).default('round_trip'),
  passengers: z.number().int().min(1).max(2).default(1),
  cabin: z.enum(['ECONOMY', 'PREMIUM_ECONOMY', 'BUSINESS', 'FIRST']).default('ECONOMY'),
  directOnly: z.boolean().default(true),
  departureWindow: window.default({ start: '00:00', end: '23:59' }),
  arrivalWindow: window.default({ start: '00:00', end: '23:59' }),
  flexDeparture: z.union([z.literal(0), z.literal(1), z.literal(3), z.literal(7)]).default(0),
  flexReturn: z.union([z.literal(0), z.literal(1), z.literal(3), z.literal(7)]).default(0),
  nearbyAirports: z.boolean().default(false),
  alternativeDestinations: z.array(airport).max(4).default([]),
  targetPrice: z.number().positive().max(1_000_000_000),
  maxPrice: amount.default(0),
  priceBasis: z.enum(['per_person', 'total']).default('per_person'),
  provider: z.enum(['demo', 'amadeus', 'google_flights']).default('demo'),
  schedule: z.object({
    mode: z.enum(['daily', 'twice_daily', 'interval']).default('daily'),
    times: z.array(time).min(1).max(2).default(['08:30', '17:30']),
    intervalHours: z.number().int().min(1).max(168).default(6),
    timezone: z.string().refine(v => DateTime.now().setZone(v).isValid, 'Use an IANA timezone.').default('Asia/Seoul'),
  }).default({ mode: 'daily', times: ['08:30', '17:30'], intervalHours: 6, timezone: 'Asia/Seoul' }),
  alertRules: z.object({ target: z.boolean().default(true), drop: z.boolean().default(false), dropPercent: z.number().min(1).max(100).default(10), lowest: z.boolean().default(true) }).default({ target: true, drop: false, dropPercent: 10, lowest: true }),
  preferences: z.object({
    companion: z.enum(['solo', 'couple', 'family']).default('couple'),
    weights: z.partialRecord(z.enum(STYLES), z.number().min(0).max(100)).default(Object.fromEntries(STYLES.map(s => [s, s === 'food' || s === 'sightseeing' ? 40 : s === 'shopping' ? 20 : 0]))),
    foods: z.array(z.string().trim().min(1).max(60)).max(12).default(['Sushi', 'Ramen', 'Yakitori']),
    cuisine: z.string().trim().max(200).default('Local food'),
    mealBudget: amount.default(30000),
    notes: z.string().trim().max(1000).default(''),
  }).default({ companion: 'couple', weights: { sightseeing: 40, food: 40, shopping: 20 }, foods: ['Sushi', 'Ramen', 'Yakitori'], cuisine: 'Local food', mealBudget: 30000, notes: '' }),
  budget: z.object({ total: amount, flight: amount, hotel: amount.default(0), food: amount.default(0), transport: amount.default(0), shopping: amount.default(0), activities: amount.default(0), other: amount.default(0) }),
}).superRefine((v, ctx) => {
  if (v.origin === v.destination) ctx.addIssue({ code: 'custom', path: ['destination'], message: 'Choose a different destination.' });
  if (v.tripType === 'round_trip' && (!v.returnDate || v.returnDate < v.departureDate)) ctx.addIssue({ code: 'custom', path: ['returnDate'], message: 'Return date must be on or after departure.' });
  if (v.maxPrice > 0 && v.targetPrice > v.maxPrice) ctx.addIssue({ code: 'custom', path: ['targetPrice'], message: 'Target must not exceed your maximum fare.' });
  if (v.schedule.mode === 'twice_daily' && v.schedule.times.length < 2) ctx.addIssue({ code: 'custom', path: ['schedule', 'times'], message: 'Choose two check times for twice-daily monitoring.' });
});

export function airportInfo(code) { return AIRPORTS.find(a => a.code === code) || { code, city: code, name: code, zone: 'UTC' }; }
export function basisPrice(total, trip) { return trip.priceBasis === 'per_person' ? total / trip.passengers : total; }
export function nextCheck(trip, from = DateTime.utc()) {
  if (!trip.active || trip.departureDate < from.setZone(trip.schedule.timezone).toISODate()) return null;
  const schedule = trip.schedule;
  if (schedule.mode === 'interval') return from.plus({ hours: schedule.intervalHours }).toUTC().toISO();
  const local = from.setZone(schedule.timezone);
  const times = schedule.mode === 'daily' ? schedule.times.slice(0, 1) : schedule.times;
  const candidates = [0, 1].flatMap(days => times.map(t => {
    const [hour, minute] = t.split(':').map(Number);
    return local.startOf('day').plus({ days }).set({ hour, minute });
  })).filter(t => t > local).sort((a, b) => a.toMillis() - b.toMillis());
  return candidates[0].toUTC().toISO();
}

function offsets(n) { return [0, ...Array.from({ length: n }, (_, i) => [-(i + 1), i + 1]).flat()]; }
export function searchVariants(trip, limit = 25) {
  const origins = trip.nearbyAirports ? AIRPORTS.filter(a => a.city === airportInfo(trip.origin).city).map(a => a.code) : [trip.origin];
  const destinations = [...new Set([trip.destination, ...(trip.nearbyAirports ? AIRPORTS.filter(a => a.city === airportInfo(trip.destination).city).map(a => a.code) : []), ...trip.alternativeDestinations])];
  const variants = [];
  for (const d of offsets(trip.flexDeparture)) for (const r of offsets(trip.tripType === 'round_trip' ? trip.flexReturn : 0)) for (const origin of origins.length ? origins : [trip.origin]) for (const destination of destinations) {
    const departureDate = DateTime.fromISO(trip.departureDate).plus({ days: d }).toISODate();
    const returnDate = trip.tripType === 'round_trip' ? DateTime.fromISO(trip.returnDate).plus({ days: r }).toISODate() : null;
    if (origin !== destination && (!returnDate || returnDate >= departureDate)) variants.push({ ...trip, origin, destination, departureDate, returnDate });
  }
  return { variants: variants.slice(0, limit), total: variants.length, truncated: variants.length > limit };
}
export function inWindow(iso, range) {
  const t = iso.slice(11, 16);
  return range.start <= range.end ? t >= range.start && t <= range.end : t >= range.start || t <= range.end;
}
export function alertReasons(current, previous, lowest, trip) {
  const rules = trip.alertRules;
  const reasons = [];
  if (rules.target && current <= trip.targetPrice && (previous == null || previous > trip.targetPrice)) reasons.push('target');
  if (rules.drop && previous > 0 && (previous - current) / previous * 100 >= rules.dropPercent) reasons.push('drop');
  if (rules.lowest && lowest != null && current < lowest) reasons.push('lowest');
  return reasons;
}
