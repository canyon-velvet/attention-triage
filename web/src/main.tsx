import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Inbox } from "./Inbox";
import { Session } from "./Session";
import { Suppressions } from "./Suppressions";
import "./style.css";

// Pages picked by the URL's #, so the server only ever serves index.html.
function App() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const update = () => setHash(location.hash);
    addEventListener("hashchange", update);
    return () => removeEventListener("hashchange", update);
  }, []);
  if (hash === "#/suppressions") return <Suppressions />;
  if (hash.startsWith("#/sessions/")) return <Session id={decodeURIComponent(hash.slice(11))} />;
  return <Inbox />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
