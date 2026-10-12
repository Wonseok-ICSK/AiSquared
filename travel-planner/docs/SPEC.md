# AI-Powered Flight Price Monitoring and Travel Planning Application — Original Specification

(Verbatim product spec provided by the user on 2026-10-06. Implementation decisions live in `../DESIGN.md`.)

## 1. Project Overview

The goal of this project is to build an application that automatically monitors flight prices for a user-defined trip and notifies the user when the airfare drops below a specified target price.

In addition to flight price monitoring, the application should provide AI-assisted travel planning based on the user's destination, travel dates, budget, and travel preferences.

The long-term goal is to create a personalized travel assistant that can continuously monitor travel costs and help users plan their entire trip.

## 2. Core User Flow

1. User enters the desired travel information.
2. User defines a target airfare price or acceptable price range.
3. The system automatically searches supported flight services on a scheduled basis.
4. The system stores and tracks airfare price changes.
5. If the airfare falls below the user's configured threshold, the system sends a notification.
6. Once suitable flights are identified, the system can generate a recommended travel itinerary based on: travel dates, destination, budget, travel style, preferred activities, food preferences.
7. The user can review the recommended flights and suggested travel itinerary.

## 3. Flight Search Requirements — 3.1 User Input

The user should be able to configure:
- Departure airport or city
- Destination airport or city
- Departure date
- Return date
- One-way or round-trip
- Number of passengers (1 or 2)
- Economy / Premium Economy / Business / First Class
- Direct flight only or connecting flights allowed
- Preferred departure time range
- Preferred arrival time range
- Maximum acceptable airfare
- Target airfare for notification

Optional flexible search settings:
- Flexible departure date: ±1 / ±3 / ±7 days
- Flexible return date
- Nearby airports
- Alternative destination airports

## 4. Flight Price Monitoring

The system should periodically check airfare prices. Example monitoring intervals: once per day, twice per day, every 6 hours, custom interval. The initial version can use a simple daily scheduled task.

The system should store historical price information such as: search timestamp, airline, flight number, departure time, arrival time, flight duration, number of stops, departure airport, arrival airport, fare type, baggage allowance if available, current price, previous price, lowest recorded price, booking URL, source of the price information.

The system should maintain a price history for each monitored trip, e.g.:

| Date | Flight | Airline | Price |
|---|---|---|---:|
| Oct 1 | ICN → NRT | Airline A | KRW 320,000 |
| Oct 2 | ICN → NRT | Airline A | KRW 298,000 |
| Oct 3 | ICN → NRT | Airline A | KRW 271,000 |

This historical data can later be used to visualize price trends.

## 5. Flight Search Sources

Potential sources: Skyscanner, Naver Flights, Google Flights if technically and legally feasible, airline websites, other flight comparison services. The architecture should allow additional providers to be added later. A provider abstraction layer is recommended:

FlightProvider — searchFlights(), getFlightDetails(), getPrice(), getBookingUrl().
Possible implementations: SkyscannerProvider, NaverFlightProvider, AirlineProvider, GoogleFlightProvider.
The system should not tightly couple the main application to a specific provider.

## 6. Airline Website Authentication

Some airline websites may provide better prices after login. The system may optionally support authenticated airline searches. Preferred authentication approaches:
1. Reuse an authenticated browser session.
2. Store encrypted cookies or session tokens.
3. Use OAuth or official airline APIs where available.
4. Use a secure credential vault if credentials must be stored.

Credentials must never be stored as plain text. Browser automation may be used when APIs are unavailable (Playwright, Chromium automation). The architecture should separate browser automation from the flight search logic.
**User preference: MS Edge as the default browser. If Edge cannot do this task while Chrome can, switch.**

## 7. Price Alert System

Users should be able to configure alert rules:
- Simple Price Alert — notify me when total flight price <= KRW 300,000
- Price Drop Alert — notify me when price decreases by more than 10%
- Lowest Price Alert — notify me when a new historical lowest price is detected

## 8. Notification Methods

At least one notification method in the MVP. Potential channels: in-app notification, email, KakaoTalk if a suitable API is available, push notification.

Example notification:

```
Flight Price Alert

Seoul (ICN) ↔ Tokyo (NRT)

Travel Dates:
Nov 20 – Nov 23

Previous Lowest Price:
KRW 320,000

Current Price:
KRW 278,000

Target Price:
KRW 300,000

The current price is below your target price.
```

The notification should contain a link that allows the user to open the booking page directly.

## 9. Travel Preference Configuration

Travel styles with weights: Sightseeing, Food-focused travel, Relaxation, Hotel / resort stay, Shopping, Nightlife, Nature, Photography, Cultural experiences, Local experiences.
Select one: Family travel, Couple travel, Solo travel.
The user should be able to assign priorities, e.g. Sightseeing 40%, Food 30%, Shopping 10%, Relaxation 20% — alternatively a simple 1–5 priority score.

## 10. Travel Budget

Configurable: total travel budget, flight, accommodation, food, transportation, shopping, activities budget.
If input is 0 or -1 the budget is unlimited, but search in a reasonable range.
- Total Budget (required): KRW 1,500,000
- Flight (required): KRW 350,000
- Hotel (optional): KRW 500,000
- Food (optional): KRW 300,000
- Transportation (optional): KRW 100,000
- Activities (optional): KRW 150,000
- Other (optional): KRW 100,000
The itinerary generator should attempt to stay within the configured budget.

## 11. AI Travel Itinerary Generator

Once the destination and flight schedule are known, generate a recommended day-by-day itinerary. Example input: Destination Tokyo; Arrival Friday 14:00; Departure Monday 19:00; Travel Style Food-focused + Sightseeing; Budget KRW 1,500,000. Example output: Day 1 — 14:00 arrive at Narita, 16:00 hotel check-in, 17:30 explore Shinjuku, 19:00 dinner recommendation, 21:00 Tokyo Metropolitan Government Building observation deck. Day 2 — 09:00 breakfast, 10:00 Asakusa / Senso-ji, 12:00 local restaurant, 14:00 Akihabara, 18:00 dinner, 20:00 optional nightlife. The itinerary should consider realistic travel time between locations.

## 12. Restaurant Recommendations

Based on: location, travel route, user budget, cuisine preference, rating (Google Maps, Tabelog, etc.), popular local food, opening hours, distance from the current itinerary location. Users may configure foods they want to try (e.g. sushi, ramen, yakitori), preferred cuisine, restaurant budget (e.g. JPY 1,000–5,000 per meal).

## 13. Attraction Recommendations

Based on: travel style, available time, distance, opening hours, entrance fees, user preferences, route efficiency, weather where relevant. Categories: famous attractions, museums, historical sites, shopping districts, local neighborhoods, nature, observation decks, theme parks, markets, nightlife areas.

## 14. Location and Route Optimization

Avoid itineraries that repeatedly cross the city; group locations geographically.
Bad: Shinjuku → Asakusa → Shibuya → Ueno → Shinjuku.
Better: Day 1 Shinjuku → Shibuya → Harajuku; Day 2 Asakusa → Ueno → Akihabara.
Consider distance, public transportation time, opening hours, user's available time.

## 15. Accommodation Search – Future Feature

Not in the MVP, but the architecture should allow accommodation providers later (Booking.com, Agoda, Expedia, Hotels.com, Airbnb, Google Hotels). Future filters: price per night, total price, rating, location, distance from station, distance from itinerary locations, breakfast included, room type, cancellation policy. The itinerary generator should eventually consider hotel location.

## 16. Suggested System Architecture

Frontend → Backend API → Travel Planning Service (Flight Search Service with providers, Flight Monitoring Service, Price History Database, Notification Service, Travel Recommendation Service, Restaurant Recommendation Service, Attraction Recommendation Service, Future Accommodation Service). A scheduler or worker handles periodic monitoring. Suggested technologies: Python or TypeScript backend, FastAPI/Flask/Node.js, PostgreSQL, Redis, Celery/APScheduler/cron, Playwright, React/Next.js.

## 17. AI / LLM Integration

LLM for: understanding natural-language preferences, generating itineraries, summarizing destination info, restaurant recommendations, explaining why flights are recommended, adjusting itineraries based on user feedback. Factual information (current airfare, restaurant availability, opening hours, attraction closures, ticket prices, weather) should be retrieved from external data sources rather than generated solely by the LLM. The LLM should primarily perform reasoning, ranking, summarization and itinerary generation using retrieved data.

## 18. Example User Configuration

Departure Seoul; Destination Tokyo; Dates November 20–27; 2 adults; direct flights only; target airfare KRW 300,000 per person; monitoring twice per day (8:30am, 5:30pm); travel style Food High, Sightseeing High, Shopping Medium, Relaxation Low; total budget KRW 1,500,000 per person; food preferences sushi, izakaya, local Japanese food. Continuously monitor and notify when a matching flight falls below KRW 300,000; once a suitable flight is identified, generate an itinerary based on actual arrival and departure times.

## 19. Recommended MVP Scope

1. Create a monitored trip. 2. Configure origin, destination, dates, passengers. 3. Configure a target airfare. 4. Search one or two flight providers. 5. Automatically perform scheduled airfare searches. 6. Save airfare price history. 7. Detect prices below the configured threshold. 8. Send a notification. 9. Display the lowest available flights. 10. Allow the user to configure travel preferences. 11. Generate a basic AI-based itinerary.
Later: multiple flight provider integrations, airline login/session, Booking.com login.
