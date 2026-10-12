import assert from "node:assert/strict";
import { test } from "node:test";
import { josa } from "../src/lib/i18n";

test("josa picks the particle from the last syllable", () => {
  assert.equal(josa("인원", "을/를"), "인원을");
  assert.equal(josa("오는 날", "을/를"), "오는 날을");
  assert.equal(josa("도착지", "을/를"), "도착지를");
  assert.equal(josa("서울", "이/가"), "서울이");
  assert.equal(josa("도쿄", "이/가"), "도쿄가");
  assert.equal(josa("ICN", "이/가"), "ICN이(가)");
  assert.equal(josa("", "을/를"), "을(를)");
});
