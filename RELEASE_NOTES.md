# AiSquared 2.0

## Source update

- Wayfinder (developed with Codex): latest local source, including language-switching UI updates.
- Travel Planner (developed with Claude): refreshed interface, charts, destination imagery, and booking-preparation workflow; updated backend and frontend tests.
- Existing Claude and OpenAI integrations remain available with user-supplied local credentials.
- Application package versions remain independent of the repository release tag `v2.0.0`.

## Sharing changes

- Research working notes under `docs/research/` are excluded, including the Naver and Google Flights browser notes.
- README documents personal learning use and the user's responsibility to comply with each website's terms.
- Naver remains disabled by default; the existing opt-in regression test passed.
- README screenshots are labeled as historical, pre-2.0 previews.

## Validation

- Wayfinder: production build and 45 tests passed.
- Travel Planner frontend: TypeScript/production build and 81 tests passed.
- Travel Planner backend: 89 focused tests passed (Naver provider, booking preparation API, and settings store).
- Builds used the existing local dependency installations. Live provider calls, paid AI calls, and the full end-to-end suite were not run.
