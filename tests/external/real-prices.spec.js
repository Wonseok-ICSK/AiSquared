import { test, expect } from '@playwright/test';
import { DateTime } from 'luxon';
import { GoogleFlightsProvider } from '../../server/google-flights.js';

test('real Edge provider returns actual fares and a paired return flight', async () => {
  const departure = DateTime.now().setZone('Asia/Seoul').plus({ months: 1 }).set({ day: 20 });
  const trip = { origin: 'ICN', destination: 'NRT', departureDate: departure.toISODate(), returnDate: departure.plus({ days: 3 }).toISODate(), tripType: 'round_trip', passengers: 2, cabin: 'ECONOMY', directOnly: true };
  const provider = new GoogleFlightsProvider({ BROWSER_CHANNEL: 'msedge' });
  const offers = await provider.searchFlights(trip);
  expect(offers.length).toBeGreaterThan(0);
  expect(offers.every(o => !o.isDemo && o.source === 'Google Flights' && o.priceTotal > 0 && o.priceTotal === o.pricePerPerson * 2)).toBe(true);
  expect(offers[0].flightNumber).toMatch(/[A-Z0-9]{2} \d+/);
  const detailed = await provider.getFlightDetails(offers[0]);
  expect(detailed.returnDetailsPending).toBe(false);
  expect(detailed.returnDepartureAt.slice(0, 10)).toBe(trip.returnDate);
  expect(new URL(detailed.bookingUrl).pathname).toBe('/travel/flights/booking');
});

test('real one-way fares have no invented return flight', async () => {
  const departure = DateTime.now().setZone('Asia/Seoul').plus({ months: 1 }).set({ day: 27 });
  const provider = new GoogleFlightsProvider({ BROWSER_CHANNEL: 'msedge' });
  const offers = await provider.searchFlights({ origin: 'ICN', destination: 'KIX', departureDate: departure.toISODate(), returnDate: null, tripType: 'one_way', passengers: 1, cabin: 'ECONOMY', directOnly: true });
  expect(offers.length).toBeGreaterThan(0);
  expect(offers.every(o => !o.isDemo && !o.returnDetailsPending && o.returnDate === null && o.returnDepartureAt === null && o.pricePerPerson === o.priceTotal)).toBe(true);
  expect(new URL(offers[0].bookingUrl).pathname).toBe('/travel/flights/booking');
});
