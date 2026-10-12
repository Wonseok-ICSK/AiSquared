import { test } from "node:test";
import assert from "node:assert/strict";
import { airportForText, looksLikeCode } from "../src/lib/airportMatch";
import { ICN, ITM, KIX, OSA, makeAirport } from "./fixtures";

const osakaOptions = [OSA, KIX, ITM];

test("Tab with suggestions loaded for the text takes the highlighted option", () => {
  assert.equal(airportForText("Osaka", osakaOptions, 0, true)?.code, "OSA");
  assert.equal(airportForText("Osaka", osakaOptions, 1, true)?.code, "KIX");
});

test("an exact code wins over the highlight, even with stale suggestions", () => {
  assert.equal(airportForText("kix", osakaOptions, 0, true)?.code, "KIX");
  assert.equal(airportForText("itm", osakaOptions, 0, false)?.code, "ITM");
});

test("stale suggestions (loaded for other text) are not picked by highlight", () => {
  assert.equal(airportForText("Osaka", osakaOptions, 0, false), null);
});

test("without a highlight: metro by city name, then a single exact name, then a lone option", () => {
  assert.equal(airportForText("osaka", [KIX, OSA, ITM], -1, true)?.code, "OSA");
  const incheon = makeAirport("ICN", "Seoul", "SEL", false, "Incheon");
  assert.equal(airportForText("incheon", [incheon, makeAirport("GMP", "Seoul", "SEL")], -1, true)?.code, "ICN");
  assert.equal(airportForText("Inch", [ICN], -1, true)?.code, "ICN");
  assert.equal(airportForText("Seo", [ICN, makeAirport("GMP", "Seoul", "SEL")], -1, true), null);
});

test("empty text or no options resolve to nothing", () => {
  assert.equal(airportForText("", osakaOptions, 0, true), null);
  assert.equal(airportForText("Osaka", [], 0, true), null);
});

test("looksLikeCode", () => {
  assert.ok(looksLikeCode(" icn "));
  assert.ok(!looksLikeCode("Osaka"));
  assert.ok(!looksLikeCode("IC1"));
});
