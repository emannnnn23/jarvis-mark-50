import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { takeToken } from "./services/token";
import "./styles/theme.css";

// Read once, before React renders: this strips the token from the address bar straight away.
const token = takeToken();

const root = document.getElementById("root");
if (root !== null) {
  createRoot(root).render(
    <StrictMode>
      <App token={token} />
    </StrictMode>,
  );
}
