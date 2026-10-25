"use client";

import { useEffect, useState, type FormEvent, type ReactNode } from "react";

import { getCurrentSession, unlockApp } from "@/lib/api";

type AccessState = "checking" | "locked" | "unlocked" | "unavailable";
const ENTRY_UNLOCK_KEY = "movie-agent-entry-unlocked";

function clearEntryUnlock() {
  try {
    window.sessionStorage.removeItem(ENTRY_UNLOCK_KEY);
  } catch {
    // The passcode form still gates the tab when browser storage is unavailable.
  }
}

export function AccessGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AccessState>("checking");
  const [passcode, setPasscode] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const timeout = window.setTimeout(() => {
      let entryIsUnlocked = false;
      try {
        // Browsers copy the opener's sessionStorage into a newly opened tab.
        // Treat that copied state as a fresh app entry, not a resumed tab.
        if (window.opener) {
          window.sessionStorage.removeItem(ENTRY_UNLOCK_KEY);
          window.sessionStorage.removeItem("movie-agent-conversation");
        }
        entryIsUnlocked = window.sessionStorage.getItem(ENTRY_UNLOCK_KEY) === "true";
      } catch {
        // Without tab storage, require the passcode for this in-memory entry.
      }
      if (!entryIsUnlocked) {
        setState("locked");
        return;
      }

      getCurrentSession()
        .then((session) => {
          if (!active) return;
          if (!session) {
            clearEntryUnlock();
            setState("locked");
            return;
          }
          setState("unlocked");
        })
        .catch(() => {
          if (!active) return;
          clearEntryUnlock();
          setError("The app could not check your session. Refresh to try again.");
          setState("unavailable");
        });
    }, 0);
    return () => {
      active = false;
      window.clearTimeout(timeout);
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await unlockApp(passcode);
      try {
        window.sessionStorage.setItem(ENTRY_UNLOCK_KEY, "true");
      } catch {
        // The app stays unlocked in memory; a reload will ask for the passcode again.
      }
      setPasscode("");
      setState("unlocked");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not unlock the app.");
    } finally {
      setPending(false);
    }
  }

  if (state === "unlocked") return children;
  if (state === "checking") {
    return (
      <main>
        <p role="status">Checking your session…</p>
      </main>
    );
  }

  return (
    <main>
      <section className="hero accessCard">
        <p className="eyebrow">Movie Agent</p>
        <h1>Welcome back.</h1>
        <p className="lede">Enter the shared passcode to start a new movie conversation in this tab.</p>
        <form onSubmit={handleSubmit}>
          <label htmlFor="app-passcode">Passcode</label>
          <div className="promptRow">
            <input
              autoComplete="current-password"
              id="app-passcode"
              maxLength={256}
              onChange={(event) => setPasscode(event.target.value)}
              required
              type="password"
              value={passcode}
            />
            <button disabled={pending || state === "unavailable"} type="submit">
              {pending ? "Checking…" : "Enter"}
            </button>
          </div>
        </form>
        {error ? <p className="accessError" role="alert">{error}</p> : null}
      </section>
    </main>
  );
}
