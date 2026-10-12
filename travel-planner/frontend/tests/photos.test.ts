import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { PHOTO_KEYS, destinationPhoto, photoCity, photoKey } from "../src/lib/photos";
import { inLang } from "./fixtures";

const IMAGES = fileURLToPath(new URL("../public/images/", import.meta.url));

describe("destination photos", () => {
  test("airport and metro codes map to the same city photo", () => {
    const expected: Record<string, string> = {
      TFU: "chengdu",
      CTU: "chengdu",
      FUK: "fukuoka",
      CTS: "sapporo",
      SPK: "sapporo",
      OKA: "okinawa",
      BKK: "bangkok",
      DMK: "bangkok",
      DAD: "danang",
      TPE: "taipei",
      TSA: "taipei",
      HKG: "hongkong",
      SIN: "singapore",
      PVG: "shanghai",
      SHA: "shanghai",
      GUM: "guam",
      CEB: "cebu",
      CXR: "nhatrang",
      NRT: "tokyo",
      HND: "tokyo",
      TYO: "tokyo",
      KIX: "osaka",
      ITM: "osaka",
      OSA: "osaka",
    };
    for (const [code, key] of Object.entries(expected)) assert.equal(photoKey(code), key, code);
  });

  test("codes are normalised; unknown codes and blanks have no photo", () => {
    assert.equal(photoKey(" tfu "), "chengdu");
    assert.equal(photoKey("JFK"), null);
    assert.equal(photoKey(""), null);
    assert.equal(destinationPhoto(null), null);
    assert.equal(destinationPhoto("ICN"), null);
  });

  test("a photo carries its URL, focus and bilingual city name", () => {
    const photo = destinationPhoto("TFU");
    assert.equal(photo?.src, "/images/chengdu.jpg");
    assert.match(photo?.focus ?? "", /^\d+% \d+%$/);
    assert.equal(photoCity(photo!), "청두");
    assert.equal(inLang("en", () => photoCity(photo!)), "Chengdu");
  });

  test("every mapped photo exists and is credited", () => {
    const credits = readFileSync(`${IMAGES}CREDITS.md`, "utf8");
    for (const key of PHOTO_KEYS) {
      assert.ok(existsSync(`${IMAGES}${key}.jpg`), `${key}.jpg is missing`);
      assert.ok(credits.includes(`\`${key}.jpg\``), `${key}.jpg has no credit line`);
    }
  });
});
