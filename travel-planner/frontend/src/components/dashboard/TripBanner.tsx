/** The picture at the top of a watched-trip card: a rounded photo of the destination with the
 * route codes and the city name over a soft bottom scrim, or, for a city without a photo, a
 * quiet ivory banner with the sunrise mark. Mouse users can click it to open the trip; it is
 * hidden from assistive tech and the tab order (the trip name link and the meta line carry the
 * same information). */

import { useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeftRight, ArrowRight } from "lucide-react";
import type { TripOut } from "../../api/types";
import { BrandMark } from "../BrandMark";
import { Skeleton } from "../ui";
import { cityName, useAirport } from "../../lib/airports";
import { destinationPhoto, photoCity } from "../../lib/photos";

/** City name to show for a trip's destination: the photo's name, else the airport lookup's
 * city, else the bare code. */
export function useDestinationCity(code: string): string {
  const photo = destinationPhoto(code);
  const airport = useAirport(photo ? null : code);
  return photo ? photoCity(photo) : (cityName(airport) ?? code);
}

export function TripBanner({ trip, city }: { trip: Pick<TripOut, "id" | "origin" | "destination" | "trip_type">; city: string }) {
  const photo = destinationPhoto(trip.destination);
  const [failed, setFailed] = useState<string | null>(null);
  const showPhoto = photo !== null && failed !== photo.src;
  const Arrow = trip.trip_type === "round_trip" ? ArrowLeftRight : ArrowRight;

  return (
    <Link
      to={`/trips/${trip.id}`}
      className={`trip-banner ${showPhoto ? "trip-banner--photo" : "trip-banner--plain"}`}
      tabIndex={-1}
      aria-hidden
    >
      {showPhoto ? (
        <img
          className="trip-banner__img"
          src={photo.src}
          alt=""
          loading="lazy"
          decoding="async"
          draggable={false}
          style={{ objectPosition: photo.focus }}
          onError={() => setFailed(photo.src)}
        />
      ) : (
        <BrandMark size={132} className="trip-banner__mark" />
      )}
      <span className="trip-banner__text">
        <span className="trip-banner__route">
          {trip.origin}
          <Arrow size={12} strokeWidth={2.25} />
          {trip.destination}
        </span>
        <span className="trip-banner__city">{city}</span>
      </span>
    </Link>
  );
}

/** Banner-shaped placeholder while the trips load. */
export function TripBannerSkeleton() {
  return (
    <div className="trip-banner trip-banner--loading" aria-hidden>
      <Skeleton width="100%" height="100%" radius={0} />
    </div>
  );
}
