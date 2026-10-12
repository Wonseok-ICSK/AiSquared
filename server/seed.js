import { DateTime } from 'luxon';
import { TripSchema, alertReasons } from './model.js';
import { DemoProvider } from './providers.js';
import { generateItinerary } from './planner.js';

export async function seedDemo(store) {
  if (store.demoSeeded()) return;
  if (store.listTrips().length) { store.markDemoSeeded(); return; }
  const nextMonth = DateTime.now().setZone('Asia/Seoul').plus({ months: 1 });
  const departureDate = nextMonth.set({ day: 20 }).toISODate(), returnDate = nextMonth.set({ day: 23 }).toISODate();
  const data = TripSchema.parse({ name: 'A long weekend in Tokyo', origin: 'ICN', destination: 'NRT', departureDate, returnDate, passengers: 2, targetPrice: 300000, maxPrice: 450000, budget: { total: 1500000, flight: 350000, hotel: 300000, food: 180000, transport: 70000, shopping: 100000, activities: 80000, other: 30000 }, schedule: { mode: 'twice_daily', times: ['08:30', '17:30'], timezone: 'Asia/Seoul', intervalHours: 6 } });
  const trip = store.createTrip(data);
  const provider = new DemoProvider();
  for (let i = 0; i < 8; i++) {
    const offers = (await provider.searchFlights(trip, { checkIndex: i })).sort((a, b) => a.priceTotal - b.priceTotal);
    const history = store.history(trip.id), prices = history.map(h => h.price);
    const reasons = alertReasons(offers[0].pricePerPerson, prices.at(-1) ?? null, prices.length ? Math.min(...prices) : null, trip);
    const time = DateTime.utc().minus({ days: 7 - i }).set({ second: 0, millisecond: 0 }).toISO();
    store.addCheck(trip, offers, reasons, time);
  }
  const detail = store.detail(trip.id);
  store.saveItinerary(trip.id, await generateItinerary(detail, detail.offers[0], { config: {} }));
  const osaka = store.createTrip(TripSchema.parse({ ...data, name: 'An Osaka food escape', destination: 'KIX', departureDate: nextMonth.set({ day: 27 }).toISODate(), returnDate: nextMonth.set({ day: 27 }).plus({ days: 3 }).toISODate(), targetPrice: 250000, passengers: 1, schedule: { ...data.schedule, mode: 'daily' } }));
  const offers = await provider.searchFlights(osaka, { checkIndex: 3 });
  store.addCheck(osaka, offers.sort((a, b) => a.priceTotal - b.priceTotal));
  store.markDemoSeeded();
}
