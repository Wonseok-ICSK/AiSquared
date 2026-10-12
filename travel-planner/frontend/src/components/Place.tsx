/** Route/place labels that resolve IATA codes to city names ("Seoul (ICN) → Tokyo (TYO)"). */

import { ArrowLeftRight, ArrowRight } from "lucide-react";
import type { TripType } from "../api/types";
import { cityName, useAirport } from "../lib/airports";
import { tr } from "../lib/i18n";

/** "Seoul (ICN)" — falls back to the bare code while loading/unknown. */
export function Place({ code, short = false }: { code: string; short?: boolean }) {
  const airport = useAirport(code);
  const city = cityName(airport);
  if (!city) return <span className="mono strong">{code}</span>;
  return (
    <span title={airport?.name}>
      {city}
      {!short && <span className="subtle"> ({code})</span>}
    </span>
  );
}

/** Origin → destination with a one-way or round-trip arrow. */
export function Route({
  origin,
  destination,
  tripType = "round_trip",
}: {
  origin: string;
  destination: string;
  tripType?: TripType;
}) {
  const Arrow = tripType === "round_trip" ? ArrowLeftRight : ArrowRight;
  return (
    <span className="route">
      <Place code={origin} />
      <Arrow
        size={15}
        aria-label={tripType === "round_trip" ? tr("round trip to", "왕복") : tr("to", "편도")}
        className="route__arrow"
      />
      <Place code={destination} />
    </span>
  );
}
