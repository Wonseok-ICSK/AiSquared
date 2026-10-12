import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { LangRoot } from "./lib/i18n";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/components.css";
import "./styles/pages/shared.css";
import "./styles/pages/dashboard.css";
import "./styles/pages/trip-form.css";
import "./styles/pages/trip-detail.css";
import "./styles/pages/charts.css";
import "./styles/pages/itinerary.css";
import "./styles/pages/notifications.css";
import "./styles/pages/settings.css";

const root = document.getElementById("root");
if (!root) throw new Error("#root element missing");

createRoot(root).render(
  <StrictMode>
    <LangRoot>
      <App />
    </LangRoot>
  </StrictMode>,
);
