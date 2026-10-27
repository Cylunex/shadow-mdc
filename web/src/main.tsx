import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";

import { App } from "./App";
import { queryClient } from "./query/client";
import "./styles.css";

const root = document.getElementById("root");
if (root === null) {
  throw new Error("root element is missing");
}

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}><App /></QueryClientProvider>
  </StrictMode>
);
