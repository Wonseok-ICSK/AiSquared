/** Destination photos for the trip banners: IATA airport or metro codes ("TFU", "CTU") map to a
 * bundled photo in public/images (credits in public/images/CREDITS.md), the city name shown over
 * it and the part of the photo a wide banner should keep. No DOM or API access (unit tested in
 * tests/photos.test.ts). */

import { tr } from "./i18n";

export interface DestinationPhoto {
  /** Photo key, also the file name: /images/<key>.jpg */
  key: string;
  /** Public URL of the photo. */
  src: string;
  /** City name as [English, Korean]; read it with `photoCity` during render. */
  city: readonly [en: string, ko: string];
  /** CSS object-position that keeps the subject inside a wide, short banner. */
  focus: string;
}

interface Place {
  city: readonly [en: string, ko: string];
  focus: string;
  /** Airport and metro (city) codes served by this photo. */
  codes: readonly string[];
}

const PLACES: Record<string, Place> = {
  tokyo: { city: ["Tokyo", "도쿄"], focus: "50% 46%", codes: ["TYO", "NRT", "HND"] },
  osaka: { city: ["Osaka", "오사카"], focus: "50% 12%", codes: ["OSA", "KIX", "ITM"] },
  chengdu: { city: ["Chengdu", "청두"], focus: "50% 48%", codes: ["CTU", "TFU"] },
  fukuoka: { city: ["Fukuoka", "후쿠오카"], focus: "50% 55%", codes: ["FUK"] },
  sapporo: { city: ["Sapporo", "삿포로"], focus: "50% 42%", codes: ["SPK", "CTS", "OKD"] },
  okinawa: { city: ["Okinawa", "오키나와"], focus: "50% 58%", codes: ["OKA"] },
  bangkok: { city: ["Bangkok", "방콕"], focus: "50% 56%", codes: ["BKK", "DMK"] },
  danang: { city: ["Da Nang", "다낭"], focus: "50% 56%", codes: ["DAD"] },
  taipei: { city: ["Taipei", "타이베이"], focus: "50% 52%", codes: ["TPE", "TSA"] },
  hongkong: { city: ["Hong Kong", "홍콩"], focus: "50% 45%", codes: ["HKG"] },
  singapore: { city: ["Singapore", "싱가포르"], focus: "50% 50%", codes: ["SIN"] },
  shanghai: { city: ["Shanghai", "상하이"], focus: "50% 46%", codes: ["SHA", "PVG"] },
  guam: { city: ["Guam", "괌"], focus: "50% 55%", codes: ["GUM"] },
  cebu: { city: ["Cebu", "세부"], focus: "50% 60%", codes: ["CEB"] },
  nhatrang: { city: ["Nha Trang", "나트랑"], focus: "50% 55%", codes: ["CXR", "NHA"] },
};

const BY_CODE = new Map<string, string>();
for (const [key, place] of Object.entries(PLACES)) {
  for (const code of place.codes) BY_CODE.set(code, key);
}

/** Every photo key, e.g. for checking that each file exists. */
export const PHOTO_KEYS: readonly string[] = Object.keys(PLACES);

/** Photo key for an airport or metro code ("tfu" -> "chengdu"), or null without a photo. */
export function photoKey(code: string | null | undefined): string | null {
  if (!code) return null;
  return BY_CODE.get(code.trim().toUpperCase()) ?? null;
}

/** The destination photo for an airport or metro code, or null when there is none. */
export function destinationPhoto(code: string | null | undefined): DestinationPhoto | null {
  const key = photoKey(code);
  if (!key) return null;
  const place = PLACES[key];
  return { key, src: `/images/${key}.jpg`, city: place.city, focus: place.focus };
}

/** City name of a photo in the UI language ("청두" / "Chengdu"). Call during render. */
export function photoCity(photo: Pick<DestinationPhoto, "city">): string {
  return tr(photo.city[0], photo.city[1]);
}
