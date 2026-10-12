import { airportInfo } from './model.js';
import { ProviderError } from './providers.js';

const p = (id, name, area, lat, lng, category, cost, duration, tags, url) => ({ id, name, area, lat, lng, category, estimatedCost: cost, durationMinutes: duration, tags, url, rating: null, openingPeriods: null, openingText: null, source: 'Sample destination guide', isSample: true });
const guides = {
  Tokyo: [
    p('sensoji', 'Sensō-ji & Asakusa streets', 'Asakusa & Ueno', 35.7148, 139.7967, 'attraction', 0, 90, ['sightseeing', 'culture', 'photography'], 'https://www.senso-ji.jp/english/'),
    p('ueno', 'Ueno Park', 'Asakusa & Ueno', 35.7155, 139.7730, 'attraction', 0, 75, ['nature', 'relaxation', 'photography'], 'https://www.gotokyo.org/en/spot/22/index.html'),
    p('ameyoko', 'Ameyoko market', 'Asakusa & Ueno', 35.7102, 139.7743, 'attraction', 0, 60, ['shopping', 'local', 'food'], 'https://www.gotokyo.org/en/spot/14/index.html'),
    p('asakusa-lunch', 'Asakusa soba lunch', 'Asakusa & Ueno', 35.7113, 139.7943, 'restaurant', 18000, 60, ['food', 'local', 'Soba'], 'https://www.google.com/maps/search/Asakusa+soba/'),
    p('ueno-izakaya', 'Ueno izakaya dinner', 'Asakusa & Ueno', 35.7095, 139.7750, 'restaurant', 30000, 75, ['food', 'nightlife', 'Izakaya', 'Yakitori'], 'https://www.google.com/maps/search/Ueno+izakaya/'),
    p('meiji', 'Meiji Jingū & its forest', 'Shibuya & Harajuku', 35.6764, 139.6993, 'attraction', 0, 90, ['culture', 'nature', 'relaxation'], 'https://www.meijijingu.or.jp/en/'),
    p('harajuku', 'Harajuku backstreets', 'Shibuya & Harajuku', 35.6702, 139.7063, 'attraction', 0, 75, ['shopping', 'local', 'photography'], 'https://www.gotokyo.org/en/destinations/western-tokyo/harajuku/index.html'),
    p('shibuya', 'Shibuya crossing & neighborhood', 'Shibuya & Harajuku', 35.6595, 139.7005, 'attraction', 0, 75, ['sightseeing', 'photography', 'nightlife'], 'https://www.gotokyo.org/en/destinations/western-tokyo/shibuya/index.html'),
    p('shibuya-ramen', 'Shibuya ramen lunch', 'Shibuya & Harajuku', 35.6605, 139.6997, 'restaurant', 16000, 60, ['food', 'Ramen'], 'https://www.google.com/maps/search/Shibuya+ramen/'),
    p('shibuya-sushi', 'Shibuya sushi dinner', 'Shibuya & Harajuku', 35.6582, 139.7020, 'restaurant', 28000, 75, ['food', 'Sushi'], 'https://www.google.com/maps/search/Shibuya+sushi/'),
    p('shinjuku-gyoen', 'Shinjuku Gyoen garden', 'Shinjuku', 35.6852, 139.7100, 'attraction', 7000, 90, ['nature', 'relaxation', 'photography'], 'https://www.env.go.jp/garden/shinjukugyoen/english/index.html'),
    p('shinjuku-view', 'Tokyo Metropolitan Government observation decks', 'Shinjuku', 35.6896, 139.6920, 'attraction', 0, 60, ['sightseeing', 'photography'], 'https://www.yokoso.metro.tokyo.lg.jp/en/tenbou/index.html'),
    p('shinjuku-walk', 'Shinjuku neighborhood walk', 'Shinjuku', 35.6937, 139.7007, 'attraction', 0, 60, ['local', 'nightlife', 'shopping'], 'https://www.gotokyo.org/en/destinations/western-tokyo/shinjuku/index.html'),
    p('shinjuku-yakitori', 'Shinjuku yakitori dinner', 'Shinjuku', 35.6932, 139.6996, 'restaurant', 27000, 75, ['food', 'Yakitori', 'Izakaya'], 'https://www.google.com/maps/search/Shinjuku+yakitori/'),
    p('shinjuku-ramen', 'Shinjuku ramen lunch', 'Shinjuku', 35.6907, 139.7035, 'restaurant', 17000, 60, ['food', 'Ramen'], 'https://www.google.com/maps/search/Shinjuku+ramen/'),
  ],
  Osaka: [
    p('osaka-castle', 'Osaka Castle park', 'Castle & central Osaka', 34.6873, 135.5262, 'attraction', 0, 90, ['culture', 'nature', 'sightseeing'], 'https://www.osakacastle.net/'),
    p('nakanoshima', 'Nakanoshima riverside', 'Castle & central Osaka', 34.6929, 135.5010, 'attraction', 0, 75, ['relaxation', 'photography'], 'https://osaka-info.jp/en/'),
    p('osaka-ramen', 'Central Osaka ramen lunch', 'Castle & central Osaka', 34.6810, 135.5100, 'restaurant', 17000, 60, ['food', 'Ramen'], 'https://www.google.com/maps/search/Osaka+ramen/'),
    p('dotonbori', 'Dōtonbori food streets', 'Namba & Dōtonbori', 34.6687, 135.5013, 'attraction', 0, 90, ['food', 'nightlife', 'photography'], 'https://osaka-info.jp/en/'),
    p('kuromon', 'Kuromon market', 'Namba & Dōtonbori', 34.6654, 135.5062, 'attraction', 0, 60, ['food', 'local', 'shopping'], 'https://kuromon.com/en/'),
    p('osaka-sushi', 'Kuromon sushi lunch', 'Namba & Dōtonbori', 34.6655, 135.5063, 'restaurant', 25000, 60, ['food', 'Sushi'], 'https://www.google.com/maps/search/Kuromon+sushi/'),
    p('okonomiyaki', 'Namba okonomiyaki dinner', 'Namba & Dōtonbori', 34.6680, 135.5010, 'restaurant', 20000, 75, ['food', 'local', 'Okonomiyaki'], 'https://www.google.com/maps/search/Namba+okonomiyaki/'),
  ],
  Seoul: [
    p('gyeongbok', 'Gyeongbokgung neighborhood', 'Jongno', 37.5796, 126.9770, 'attraction', 6000, 90, ['culture', 'photography', 'sightseeing'], 'https://www.royalpalace.go.kr/'),
    p('bukchon', 'Bukchon neighborhood walk', 'Jongno', 37.5826, 126.9831, 'attraction', 0, 75, ['local', 'culture'], 'https://english.visitseoul.net/'),
    p('insadong', 'Insadong tea & craft streets', 'Jongno', 37.5743, 126.9848, 'attraction', 0, 60, ['shopping', 'local'], 'https://english.visitseoul.net/'),
    p('seoul-lunch', 'Jongno Korean lunch', 'Jongno', 37.5730, 126.9810, 'restaurant', 15000, 60, ['food', 'Korean'], 'https://www.google.com/maps/search/Jongno+Korean+restaurant/'),
    p('hongdae', 'Hongdae neighborhood', 'Hongdae', 37.5563, 126.9220, 'attraction', 0, 90, ['shopping', 'nightlife', 'local'], 'https://english.visitseoul.net/'),
    p('seoul-bbq', 'Hongdae barbecue dinner', 'Hongdae', 37.5550, 126.9230, 'restaurant', 30000, 75, ['food', 'Korean', 'Barbecue'], 'https://www.google.com/maps/search/Hongdae+barbecue/'),
  ],
};

export class DestinationProvider {
  async getPlaces() { throw new Error('Destination provider must implement getPlaces.'); }
}
export class SampleDestinationProvider extends DestinationProvider {
  async getPlaces(trip) {
    const city = airportInfo(trip.destination).city;
    if (!guides[city]) throw new ProviderError(`The sample guide covers Tokyo, Osaka, and Seoul. Configure Google Places for ${city}.`, 422);
    return { places: structuredClone(guides[city]), source: 'Sample destination guide', retrievedAt: null, warnings: ['Sample guide: opening hours, closures, availability, ratings, and costs have not been verified. Restaurant entries are neighborhood search suggestions.', 'Transit times and KRW costs are planning estimates. Check source links before traveling.'] };
  }
}
export class GooglePlacesProvider extends DestinationProvider {
  constructor(config = process.env, fetchImpl = fetch) { super(); this.config = config; this.fetch = fetchImpl; }
  async getPlaces(trip) {
    const city = airportInfo(trip.destination).city;
    const queries = [{ category: 'attraction', query: `${city} tourist attractions ${Object.entries(trip.preferences.weights).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([k]) => k).join(' ')}` }, { category: 'restaurant', query: `${city} ${trip.preferences.foods.join(' ')} ${trip.preferences.cuisine} restaurants` }];
    const results = await Promise.all(queries.map(async ({ category, query }) => {
      const response = await this.fetch('https://places.googleapis.com/v1/places:searchText', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Goog-Api-Key': this.config.GOOGLE_PLACES_API_KEY, 'X-Goog-FieldMask': 'places.id,places.displayName,places.location,places.formattedAddress,places.googleMapsUri,places.rating,places.regularOpeningHours,places.businessStatus,places.types' }, body: JSON.stringify({ textQuery: query, pageSize: 15, languageCode: 'ko' }), signal: AbortSignal.timeout(15000) });
      if (!response.ok) throw new ProviderError(`Google Places lookup failed (${response.status}). Check API access on the server.`);
      const data = await response.json();
      return (data.places || []).filter(x => (!x.businessStatus || x.businessStatus === 'OPERATIONAL') && x.location).map(x => ({ id: x.id, name: x.displayName?.text || '이름 없는 장소', area: x.formattedAddress || city, lat: x.location.latitude, lng: x.location.longitude, category, estimatedCost: category === 'restaurant' ? 25000 : 12000, durationMinutes: category === 'restaurant' ? 60 : 90, tags: category === 'restaurant' ? ['food', ...trip.preferences.foods] : ['sightseeing', ...(x.types || [])], url: x.googleMapsUri || `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(x.displayName?.text || city)}&query_place_id=${x.id}`, rating: x.rating || null, openingPeriods: x.regularOpeningHours?.periods || null, openingText: x.regularOpeningHours?.weekdayDescriptions?.join(' · ') || null, source: 'Google Places', isSample: false }));
    }));
    const places = [...new Map(results.flat().map(p => [p.id, p])).values()];
    if (!places.length) throw new ProviderError('No destination places found. Try a supported airport or different food preferences.', 422);
    return { places, source: 'Google Places', retrievedAt: new Date().toISOString(), warnings: ['Google Places hours are regular weekly hours; verify holiday changes and reservations. Future availability and weather are not confirmed.', 'Transit times, admission costs, meal costs, and lodging allowances are KRW estimates; no live exchange rate is used.'] };
  }
}
export function destinationProvider(config = process.env) { return config.GOOGLE_PLACES_API_KEY ? new GooglePlacesProvider(config) : new SampleDestinationProvider(); }
