export class ProviderError extends Error { constructor(message, status = 502) { super(message); this.status = status; } }
export class FlightProvider {
  async searchFlights() { throw new Error('Provider must implement searchFlights.'); }
  async getFlightDetails(offer) { return offer; }
  async getPrice(offer) { return offer.priceTotal; }
  getBookingUrl(offer) { return offer.bookingUrl; }
}
