import { describe, test } from "node:test";
import assert from "node:assert/strict";
import type { MetaDefaults } from "../src/api/types";
import {
  autoTripName,
  diffPayload,
  emptyTripForm,
  formToPayload,
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
