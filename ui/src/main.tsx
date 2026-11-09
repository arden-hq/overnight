import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./main.css";
import { App } from "./App";
import { ApiProvider } from "./lib/api";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ApiProvider>
      <App />
    </ApiProvider>
  </StrictMode>,
);
