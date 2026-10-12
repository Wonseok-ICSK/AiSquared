import { chromium } from 'playwright';
import { existsSync } from 'node:fs';
import { FlightProvider, ProviderError } from './provider-base.js';
import { extractGoogleData, googleBlockReason, parseGoogleLegs, parseGoogleOffers } from './google-flights-parse.js';
import { googleFlightSearchUrl, offerBookingUrl } from '../shared/flight-search.js';

// Same base-path fetch and selected-leg lookup as the neighboring travel-planner.
// One isolated Edge context per request; no personal profile, stealth, or CAPTCHA retries.
export class GoogleFlightsProvider extends FlightProvider {
  constructor(config = process.env, { fetchPage, now = Date.now, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)) } = {}) {
    super(); this.channel = config.BROWSER_CHANNEL || 'msedge'; this.now = now; this.sleep = sleep; this.fetchPage = fetchPage || this.browserPage.bind(this);
    this.cache = new Map(); this.queue = Promise.resolve(); this.nextRequest = 0; this.blockedUntil = 0;
    this.maxSearchVariants = 4; this.environment = 'live';
  }
  get configured() {
    if (this.channel !== 'msedge' || process.platform !== 'win32') return true;
    return [process.env['ProgramFiles(x86)'], process.env.ProgramFiles, process.env.LOCALAPPDATA].filter(Boolean).some(root => existsSync(`${root}/Microsoft/Edge/Application/msedge.exe`));
  }
  async browserPage(url) {
    let browser;
    try { browser = await chromium.launch({ channel: this.channel, headless: true, timeout: 15000 }); }
    catch { throw new ProviderError('Microsoft Edge is unavailable. Install Edge to search real flight prices.', 503); }
    try {
      const page = await browser.newPage({ locale: 'en-US' });
      const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      const html = await page.content();
      return { html, url: page.url(), status: response?.status() || 200 };
    } finally { await browser.close(); }
  }
  async data(search, outbound = []) {
    const url = googleFlightSearchUrl(search, { mode: 'fetch', outbound, directOnly: search.directOnly });
    if (!url) throw new ProviderError('Invalid flight search dates or airports.', 400);
    const cached = this.cache.get(url);
    if (cached && this.now() - cached.time < 60000) return cached;
    const job = this.queue.then(async () => {
      const again = this.cache.get(url);
      if (again && this.now() - again.time < 60000) return again;
      if (this.now() < this.blockedUntil) throw new ProviderError('Google Flights temporarily blocked searches. Try later or open Google Flights directly.', 503);
      const wait = this.nextRequest - this.now(); if (wait > 0) await this.sleep(wait);
      let page;
      try { page = await this.fetchPage(url); }
      finally { this.nextRequest = this.now() + 5000; }
      const blocked = googleBlockReason(page.html, page.url, page.status);
      if (blocked) {
        this.blockedUntil = this.now() + 4 * 60 * 60 * 1000;
        throw new ProviderError('Google Flights temporarily blocked searches. Try later or open Google Flights directly.', 503);
      }
      if (page.status >= 400) throw new ProviderError(`Google Flights search failed (${page.status}).`, 502);
      const result = { payload: extractGoogleData(page.html), observedAt: new Date(this.now()).toISOString(), time: this.now() };
      this.cache.set(url, result);
      if (this.cache.size > 50) this.cache.delete(this.cache.keys().next().value);
      return result;
    });
    this.queue = job.catch(() => {});
    return job;
  }
  async searchFlights(trip) {
    const data = await this.data(trip);
    return parseGoogleOffers(data.payload, trip, data.observedAt);
  }
  async getFlightDetails(offer) {
    if (!offer.returnDetailsPending) return offer;
    const data = await this.data(offer.search, offer.segments);
    const options = parseGoogleLegs(data.payload).filter(o => o.origin === offer.destination && o.destination === offer.origin && o.departureAt.slice(0, 10) === offer.returnDate && (!offer.search.directOnly || o.stops === 0)).sort((a, b) => a.priceTotal - b.priceTotal);
    const selected = options.find(o => o.priceTotal === offer.priceTotal) || options[0];
    if (!selected) throw new ProviderError('No matching return flight is available. Search prices again before generating a plan.', 422);
    const result = { ...offer, priceTotal: selected.priceTotal, pricePerPerson: selected.priceTotal / offer.passengers, returnDepartureAt: selected.departureAt, returnArrivalAt: selected.arrivalAt, returnStops: selected.stops, returnSegments: selected.segments, returnFlightNumber: selected.flightNumber, returnDetailsPending: false, observedAt: data.observedAt, fareType: '왕복 항공편 표시 요금', linkType: 'booking_options' };
    result.bookingUrl = offerBookingUrl(offer.search, result);
    return result;
  }
  async getPrice(offer) {
    this.cache.delete(googleFlightSearchUrl(offer.search, { mode: 'fetch', directOnly: offer.search.directOnly }));
    const offers = await this.searchFlights(offer.search);
    const current = offers.find(o => o.id === offer.id);
    if (!current) throw new ProviderError('This flight is no longer priced. Run a new search.', 422);
    return current.priceTotal;
  }
}
