import { test } from "node:test";
import assert from "node:assert/strict";
import { isLastDefault, nextDefaultProviders } from "../src/lib/providerDefaults";

const providers = [
  { key: "google_flights", enabled: true },
  { key: "serpapi", enabled: true },
  { key: "naver", enabled: false },
];

test("switching sources on and off builds an explicit list", () => {
  assert.deepEqual(nextDefaultProviders(providers, "google_flights", false), ["serpapi"]);
  assert.deepEqual(nextDefaultProviders(providers, "naver", true), ["google_flights", "serpapi", "naver"]);
});

test("the last default source cannot be switched off ([] would mean built-in defaults)", () => {
  const one = [
    { key: "google_flights", enabled: false },
    { key: "serpapi", enabled: true },
  ];
  assert.equal(nextDefaultProviders(one, "serpapi", false), null);
  assert.ok(isLastDefault(one, "serpapi"));
  assert.ok(!isLastDefault(one, "google_flights"));
  assert.ok(!isLastDefault(providers, "serpapi"));
});
