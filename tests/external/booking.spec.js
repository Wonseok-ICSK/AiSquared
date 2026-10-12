import { test, expect } from '@playwright/test';
import { DateTime } from 'luxon';
import { googleFlightSearchUrl } from '../../shared/flight-search.js';

// These opt-in checks visit the real provider. An anti-bot page is a failure,
// never a reason to bypass it or accept an unverified date-only URL assertion.
const departure = DateTime.now().setZone('Asia/Seoul').plus({ months: 1 }).set({ day: 20 });
const returning = departure.plus({ days: 3 });
const cases = [
  { name: 'round trip / two adults / economy', destination: 'NRT', passengers: 2, cabin: 'ECONOMY', label: '이코노미', roundTrip: true },
  { name: 'one way / one adult / business', destination: 'KIX', passengers: 1, cabin: 'BUSINESS', label: '비즈니스', roundTrip: false },
  { name: 'round trip / one adult / premium economy', destination: 'NRT', passengers: 1, cabin: 'PREMIUM_ECONOMY', label: '프리미엄 이코노미', roundTrip: true },
  { name: 'round trip / two adults / first', destination: 'NRT', passengers: 2, cabin: 'FIRST', label: '퍼스트', roundTrip: true },
];
for (const scenario of cases) {
  test(`real Google booking search keeps ${scenario.name}`, async ({ page }, testInfo) => {
    const search = { origin: 'ICN', ...scenario, departureDate: departure.toISODate(), returnDate: scenario.roundTrip ? returning.toISODate() : null };
    await page.goto(googleFlightSearchUrl(search), { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('combobox', { name: /출발지가 어디인가요.*ICN/ })).toBeVisible({ timeout: 30000 });
    await expect(page.getByRole('combobox', { name: new RegExp(`목적지가 어디인가요.*${scenario.destination}`) })).toBeVisible();
    await expect(page.getByRole('combobox', { name: /티켓 유형/ })).toHaveAccessibleName(new RegExp(scenario.roundTrip ? '왕복$' : '편도$'));
    await expect(page.getByRole('button', { name: new RegExp(`승객 ${scenario.passengers}명, 승객 수`) })).toBeVisible();
    await expect(page.getByRole('combobox', { name: /좌석 등급/ })).toHaveAccessibleName(new RegExp(`${scenario.label}$`));
    await expect(page.getByRole('textbox', { name: '출발', exact: true })).toHaveValue(new RegExp(`${departure.month}월\\s*${departure.day}일`));
    const returnInput = page.getByRole('textbox', { name: '도착', exact: true });
    if (scenario.roundTrip) await expect(returnInput).toHaveValue(new RegExp(`${returning.month}월\\s*${returning.day}일`));
    else await expect(returnInput).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath('prefilled-search.png'), fullPage: true });
  });
}
