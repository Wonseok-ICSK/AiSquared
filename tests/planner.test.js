import test from 'node:test';
import assert from 'node:assert/strict';
import { DemoProvider } from '../server/providers.js';
import { generateItinerary, isOpen } from '../server/planner.js';
import { tripData } from './fixtures.js';

const time = t => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
test('plans respect actual flight boundaries, nearby grouping, and spending caps', async () => {
  const trip = tripData({ budget: { total: 1500000, flight: 350000, food: 180000, hotel: 300000, activities: 80000 } });
  const offer = (await new DemoProvider().searchFlights(trip))[0];
  const plan = await generateItinerary(trip, offer, { config: {} });
  assert.equal(plan.mode, 'rule_based'); assert.equal(plan.days.length, 4);
  assert.equal(plan.days[0].events[0].startTime, '13:55');
  assert.ok(plan.days[0].events.filter(e => e.placeId).every(e => time(e.startTime) >= 16 * 60 + 25));
  assert.ok(plan.days.at(-1).events.filter(e => e.placeId).every(e => time(e.endTime) <= 16 * 60));
  assert.equal(plan.days.at(-1).events.at(-1).endTime, '19:00');
  for (const day of plan.days) for (let i = 1; i < day.events.length; i++) assert.ok(time(day.events[i].startTime) >= time(day.events[i - 1].endTime), `${day.date}: ${day.events[i].title} overlaps`);
  assert.ok(plan.costs.food <= 180000); assert.ok(plan.estimatedTotal <= trip.budget.total);
  for (const day of plan.days) assert.ok(day.events.filter(e => e.category === 'restaurant' || e.category === 'meal_break').length <= 2);
  assert.ok(plan.warnings.some(w => w.includes('검증되지 않았습니다')));
});
test('early departures contain no sightseeing after the airport cutoff', async () => {
  const trip = tripData(), offer = (await new DemoProvider().searchFlights(trip))[0];
  offer.returnDepartureAt = '2030-11-23T07:00:00+09:00';
  const plan = await generateItinerary(trip, offer, { config: {} });
  assert.equal(plan.days.at(-1).events.filter(e => e.placeId).length, 0);
  assert.equal(plan.days.at(-1).events[0].startTime, '04:00');
});

test('Korean food preferences preserve the same ranking and itinerary boundaries', async () => {
  const trip = tripData(), offer = (await new DemoProvider().searchFlights(trip))[0];
  const english = await generateItinerary(trip, offer, { config: {} });
  const korean = await generateItinerary({ ...trip, preferences: { ...trip.preferences, foods: trip.preferences.foods.map(food => ({ Sushi: '스시', Ramen: '라멘', Yakitori: '야키토리' })[food] || food) } }, offer, { config: {} });
  assert.deepEqual(korean.days, english.days);
  assert.match(korean.title, /도쿄/);
  assert.match(korean.days[0].events[0].title, /공항 도착/);
});
test('late arrivals and midnight departures never generate invalid clock times or overlaps', async () => {
  const trip = tripData(), offer = (await new DemoProvider().searchFlights(trip))[0];
  offer.arrivalAt = '2030-11-20T23:50:00+09:00'; offer.returnDepartureAt = '2030-11-23T00:30:00+09:00';
  const plan = await generateItinerary(trip, offer, { config: {} });
  for (const day of plan.days) for (const event of day.events) { assert.ok(time(event.startTime) <= 1440); assert.ok(time(event.endTime) <= 1440); }
  assert.ok(plan.days[2].events.some(e => e.title === '공항으로 이동' && e.startTime === '21:30'));
  assert.equal(plan.days.at(-1).events.filter(e => e.placeId).length, 0);
});
test('unlimited budgets still produce reasonable estimates, and insufficient budgets are reported', async () => {
  const trip = tripData({ budget: { total: 0, flight: -1, food: 0 } }), offer = (await new DemoProvider().searchFlights(trip))[0];
  const plan = await generateItinerary(trip, offer, { config: {} });
  assert.ok(plan.estimatedTotal > 0 && plan.estimatedTotal < 2000000); assert.equal(plan.withinBudget, true);
  const low = await generateItinerary(tripData({ budget: { total: 100000, flight: 50000 } }), offer, { config: {} });
  assert.equal(low.withinBudget, false); assert.ok(low.warnings.some(w => w.includes('초과'))); assert.equal(low.costs.food, 0);
});
test('party estimates account for both passengers while per-person flight prices stay correct', async () => {
  const per = tripData(), offer = (await new DemoProvider().searchFlights(per))[0];
  const perPlan = await generateItinerary(per, offer, { config: {} });
  const totalPlan = await generateItinerary(tripData({ priceBasis: 'total', budget: { total: 3000000, flight: 700000 } }), offer, { config: {} });
  assert.equal(totalPlan.costs.flight, perPlan.costs.flight * 2); assert.equal(totalPlan.costs.food, perPlan.costs.food * 2);
});
test('regular hours exclude closed days, handle overnight hours, and accept 24-hour places', () => {
  const venue = { openingPeriods: [{ open: { day: 3, hour: 9 }, close: { day: 3, hour: 17 } }] };
  assert.ok(isOpen(venue, '2030-11-20', 600, 90)); assert.ok(!isOpen(venue, '2030-11-20', 1000, 60)); assert.ok(!isOpen(venue, '2030-11-21', 600, 90));
  const overnight = { openingPeriods: [{ open: { day: 3, hour: 22 }, close: { day: 4, hour: 2 } }] };
  assert.ok(isOpen(overnight, '2030-11-21', 60, 30));
  assert.ok(isOpen({ openingPeriods: [{ open: { day: 0, hour: 0 } }] }, '2030-11-20', 600, 60));
});
test('AI ranking is grounded to known IDs; unknown IDs and provider failures fall back explicitly', async () => {
  const trip = tripData(), offer = (await new DemoProvider().searchFlights(trip))[0];
  const config = { OPENAI_API_KEY: 'test-key', OPENAI_MODEL: 'test-model' };
  const aiResponse = rankedPlaceIds => new Response(JSON.stringify({ status: 'completed', output: [{ content: [{ type: 'output_text', text: JSON.stringify({ title: '도쿄에서의 순간', explanation: '맛집과 문화를 즐기는 여행.', rankedPlaceIds }) }] }] }));
  const plan = await generateItinerary(trip, offer, { config, fetchImpl: async (_url, opts) => { const body = JSON.parse(opts.body); assert.equal(body.text.format.strict, true); assert.equal(body.store, false); assert.match(body.input[0].content, /natural Korean/); return aiResponse(['sensoji', 'shibuya-ramen']); } });
  assert.equal(plan.mode, 'ai_assisted'); assert.equal(plan.title, '도쿄에서의 순간');
  const invalid = await generateItinerary(trip, offer, { config, fetchImpl: async () => aiResponse(['made-up-restaurant']) });
  assert.equal(invalid.mode, 'rule_based'); assert.ok(invalid.warnings.some(w => w.includes('AI 추천을 사용할 수 없어')));
});
test('unsupported sample destinations and impossibly short stays fail clearly', async () => {
  const trip = tripData(), offer = (await new DemoProvider().searchFlights(trip))[0];
  await assert.rejects(generateItinerary(trip, { ...offer, destination: 'LHR' }, { config: {} }), /sample guide covers/);
  await assert.rejects(generateItinerary(trip, { ...offer, returnDepartureAt: '2030-11-20T15:00:00+09:00' }, { config: {} }), /too short/);
});
