import { test, expect } from '@playwright/test';

test('language selection persists and covers navigation, flights and trip forms', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByRole('combobox', { name: 'Language / 언어' }).selectOption('en');
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.getByRole('heading', { name: 'Your next journey starts here.' })).toBeVisible();
  await page.getByRole('button', { name: 'Live flights', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Search live flights', exact: true })).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Cabin class' })).toContainText('Business');
  await page.getByRole('button', { name: 'New trip', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Where shall we go?' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Trip name' })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.reload();
  await expect(page.getByRole('combobox', { name: 'Language / 언어' })).toHaveValue('en');
  await expect(page.getByRole('heading', { name: 'Your next journey starts here.' })).toBeVisible();
  await page.getByRole('combobox', { name: 'Language / 언어' }).selectOption('ko');
  await expect(page.getByRole('heading', { name: '다음 여행, 여기서 시작하세요.' })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'ko');
  expect(errors).toEqual([]);
});

test('language control remains visible on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('combobox', { name: 'Language / 언어' }).selectOption('en');
  await expect(page.getByRole('combobox', { name: 'Language / 언어' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
