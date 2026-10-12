/** Router + app-wide providers. Pages other than the dashboard are code-split. */

import { lazy } from "react";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { Layout } from "./components/Layout";
import { AppProvider } from "./lib/appContext";
import { Dashboard } from "./pages/Dashboard";
import { NotFound } from "./pages/NotFound";

const TripForm = lazy(() => import("./pages/TripForm").then((m) => ({ default: m.TripForm })));
const TripDetail = lazy(() => import("./pages/TripDetail").then((m) => ({ default: m.TripDetail })));
const BookingPrep = lazy(() => import("./pages/BookingPrep").then((m) => ({ default: m.BookingPrep })));
const ItineraryView = lazy(() => import("./pages/ItineraryView").then((m) => ({ default: m.ItineraryView })));
const Notifications = lazy(() => import("./pages/Notifications").then((m) => ({ default: m.Notifications })));
const SettingsPage = lazy(() => import("./pages/Settings").then((m) => ({ default: m.SettingsPage })));

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: "/", element: <Dashboard /> },
      { path: "/trips/new", element: <TripForm /> },
      { path: "/trips/:id/edit", element: <TripForm /> },
      { path: "/trips/:id", element: <TripDetail /> },
      { path: "/trips/:tripId/book/:offerId", element: <BookingPrep /> },
      { path: "/itineraries/:id", element: <ItineraryView /> },
      { path: "/notifications", element: <Notifications /> },
      { path: "/settings", element: <SettingsPage /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

/** Root component. */
export function App() {
  return (
    <AppProvider>
      <RouterProvider router={router} />
    </AppProvider>
  );
}
