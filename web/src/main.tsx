import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Inbox } from "./Inbox";
import { Suppressions } from "./Suppressions";
import "./style.css";

// Two pages, picked by the URL's #, so the server only ever serves index.html.
function App() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const update = () => setHash(location.hash);
    addEventListener("hashchange", update);
    return () => removeEventListener("hashchange", update);
  }, []);
  return hash === "#/suppressions" ? <Suppressions /> : <Inbox />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
