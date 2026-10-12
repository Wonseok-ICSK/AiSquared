import { describe, test } from "node:test";
import assert from "node:assert/strict";
import type { MetaDefaults } from "../src/api/types";
import {
  applyTripPrefill,
  autoTripName,
  diffPayload,
  emptyTripForm,
  formToPayload,
  hasPrefill,
  isIsoDate,
  parseTripPrefill,
  routeOverlapError,
  tripToForm,
  validateTripForm,
} from "../src/lib/tripForm";
import { GMP, HND, ICN, ITM, KIX, NRT, OSA, SEL, TYO, makeTrip } from "./fixtures";

describe("trip name in edit mode", () => {
  test("clearing a custom name sends an explicit empty name", () => {
    const trip = makeTrip({ name: "Tokyo food trip" });
    const form = { ...tripToForm(trip), name: "" };
    assert.deepEqual(diffPayload(trip, formToPayload(form)), { name: "" });
  });

  test("an untouched auto name is not sent, so the backend can regenerate it", () => {
    const trip = makeTrip({ name: autoTripName("ICN", "TYO") });
    const form = tripToForm(trip);
    assert.equal(form.name, "", "auto names are shown as the placeholder");
    assert.deepEqual(diffPayload(trip, formToPayload(form)), {});
    const moved = { ...form, destination: "OSA" };
    assert.deepEqual(diffPayload(trip, formToPayload(moved)), { destination: "OSA" });
  });

  test("a renamed trip sends the new name, an unchanged custom name nothing", () => {
    const trip = makeTrip({ name: "Tokyo food trip" });
    const form = tripToForm(trip);
    assert.equal(form.name, "Tokyo food trip");
    assert.deepEqual(diffPayload(trip, formToPayload(form)), {});
    assert.deepEqual(diffPayload(trip, formToPayload({ ...form, name: "Sushi run" })), { name: "Sushi run" });
  });
});

describe("route overlap mirrors schemas._check_trip_consistency", () => {
  test("two airports of the same city are allowed", () => {
    assert.equal(routeOverlapError("ICN", "GMP", ICN, GMP), null);
    assert.equal(routeOverlapError("NRT", "HND", NRT, HND), null);
    assert.equal(routeOverlapError("KIX", "ITM", KIX, ITM), null);
    assert.equal(routeOverlapError("TYO", "OSA", TYO, OSA), null);
  });

  test("a city code and one of its airports, or the same code, are rejected", () => {
    assert.match(routeOverlapError("SEL", "ICN", SEL, ICN) ?? "", /SEL에 이미 ICN/);
    assert.match(routeOverlapError("ICN", "SEL", ICN, SEL) ?? "", /SEL에 이미 ICN/);
    assert.match(routeOverlapError("ICN", "ICN", ICN, ICN) ?? "", /달라야/);
    assert.match(routeOverlapError("TYO", "TYO", undefined, undefined) ?? "", /달라야/);
  });

  test("unknown lookups never block", () => {
    assert.equal(routeOverlapError("SEL", "ICN", undefined, undefined), null);
  });

  test("validateTripForm accepts ICN → GMP", () => {
    const defaults: MetaDefaults = {
      trip_type: "one_way",
      adults: 1,
      cabin: "economy",
      price_basis: "per_person",
      currency: "KRW",
      schedule_mode: "times",
      schedule_times: ["08:30"],
    } as MetaDefaults;
    const form = { ...emptyTripForm(defaults), origin: "ICN", destination: "GMP", depart_date: "2026-11-20" };
    const errors = validateTripForm(form, { origin: ICN, destination: GMP }, false);
    assert.equal(errors.destination, undefined);
    const bad = validateTripForm({ ...form, origin: "SEL" }, { origin: SEL, destination: GMP }, false);
    assert.match(bad.destination ?? "", /SEL에 이미 GMP/);
  });
});

describe("new-trip prefill from the URL (dashboard composer)", () => {
  const defaults: MetaDefaults = {
    trip_type: "round_trip",
    adults: 1,
    cabin: "economy",
    price_basis: "per_person",
    currency: "KRW",
    schedule_mode: "times",
    schedule_times: ["08:30"],
  } as MetaDefaults;

  test("the full composer link fills every field", () => {
    const prefill = parseTripPrefill(
      "origin=ICN&destination=NRT&depart=2026-11-20&return=2026-11-27&adults=2&trip_type=round_trip",
    );
    assert.deepEqual(prefill, {
      origin: "ICN",
      destination: "NRT",
      depart_date: "2026-11-20",
      return_date: "2026-11-27",
      adults: 2,
      trip_type: "round_trip",
      ignored: [],
    });
    const form = applyTripPrefill(emptyTripForm(defaults), prefill);
    assert.equal(form.origin, "ICN");
    assert.equal(form.return_date, "2026-11-27");
    assert.equal(form.adults, 2);
    assert.equal(form.currency, "KRW", "fields the link does not carry keep their defaults");
    assert.deepEqual(validateTripForm(form, {}, false), {});
  });

  test("city codes are accepted and codes are upper-cased", () => {
    const prefill = parseTripPrefill(new URLSearchParams({ origin: " sel ", destination: "tyo" }));
    assert.equal(prefill.origin, "SEL");
    assert.equal(prefill.destination, "TYO");
    assert.equal(prefill.trip_type, undefined, "no dates: the default trip type stays");
    assert.equal(parseTripPrefill("destination=OSA").destination, "OSA");
  });

  test("trip type follows the dates when the link does not say", () => {
    assert.equal(parseTripPrefill("depart=2026-11-20&return=2026-11-27").trip_type, "round_trip");
    assert.equal(parseTripPrefill("depart=2026-11-20").trip_type, "one_way");
    assert.equal(parseTripPrefill("return=2026-11-27").trip_type, "round_trip");
    const oneWay = parseTripPrefill("depart=2026-11-20&return=2026-11-27&trip_type=one_way");
    assert.equal(oneWay.trip_type, "one_way", "an explicit trip type wins");
    assert.equal(formToPayload(applyTripPrefill(emptyTripForm(defaults), oneWay)).return_date, null);
  });

  test("invalid values are ignored, valid ones still apply", () => {
    const prefill = parseTripPrefill(
      "origin=Seoul&destination=NRT&depart=2026-02-30&return=2026-11-27&adults=3&trip_type=multi_city",
    );
    assert.deepEqual(prefill, {
      destination: "NRT",
      return_date: "2026-11-27",
      trip_type: "round_trip",
      ignored: ["origin", "depart", "adults", "trip_type"],
    });
    assert.deepEqual(parseTripPrefill("adults=0").ignored, ["adults"]);
    assert.deepEqual(parseTripPrefill("adults=1.5").ignored, ["adults"]);
    assert.equal(parseTripPrefill("adults=1").adults, 1);
    assert.deepEqual(parseTripPrefill("depart=20261120").ignored, ["depart"]);
  });

  test("a return before the departure is ignored, so the trip becomes one way", () => {
    const prefill = parseTripPrefill("depart=2026-11-27&return=2026-11-20");
    assert.equal(prefill.depart_date, "2026-11-27");
    assert.equal(prefill.return_date, undefined);
    assert.equal(prefill.trip_type, "one_way");
    assert.deepEqual(prefill.ignored, ["return"]);
    assert.equal(parseTripPrefill("depart=2026-11-20&return=2026-11-20").return_date, "2026-11-20");
  });

  test("empty and unrelated parameters prefill nothing", () => {
    for (const query of ["", "origin=&adults=", "tab=flights", "trip_type=bogus"]) {
      const prefill = parseTripPrefill(query);
      assert.equal(hasPrefill(prefill), false, query);
      assert.deepEqual(applyTripPrefill(emptyTripForm(defaults), prefill), emptyTripForm(defaults));
    }
    assert.equal(hasPrefill(parseTripPrefill("adults=2")), true);
  });

  test("isIsoDate checks the calendar", () => {
    assert.equal(isIsoDate("2028-02-29"), true);
    assert.equal(isIsoDate("2026-02-29"), false);
    assert.equal(isIsoDate("2026-13-01"), false);
    assert.equal(isIsoDate("2026-1-05"), false);
  });
});
