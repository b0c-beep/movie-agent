"use client";

import { useEffect, useState, type FormEvent, type ReactNode } from "react";

import { getCurrentSession, unlockApp } from "@/lib/api";

type AccessState = "checking" | "locked" | "unlocked" | "unavailable";

export function AccessGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AccessState>("checking");
  const [passcode, setPasscode] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getCurrentSession()
      .then((session) => {
        if (active) setState(session ? "unlocked" : "locked");
      })
      .catch(() => {
        if (active) {
          setState("unavailable");
          setError("The app could not check your session. Refresh to try again.");
        }
      });
    return () => {
      active = false;
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await unlockApp(passcode);
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
        <p className="lede">Enter the shared passcode to start a movie session.</p>
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
