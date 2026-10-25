"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import { ActiveFilters } from "@/components/active-filters";
import { ConversationThread } from "@/components/conversation-thread";
import { ProgressTrace } from "@/components/progress-trace";
import { RecommendationResults } from "@/components/recommendation-results";
import {
  getConversation,
  getRecommendationCapabilities,
  getRunStatus,
  refreshConversationAvailability,
  streamRecommendations,
  type ClarificationOption,
  type ConversationSnapshot,
  type FilterEdit,
  type RecommendationCapabilities,
  type RecommendationResult,
  type TraceEvent,
} from "@/lib/api";
import { countryName } from "@/lib/countries";
import {
  activeFilters,
  choiceMessage,
  clearLocalTurns,
  eventsFromTurns,
  failureMessage,
  filterEditMessage,
  interruptionMessage,
  latestAnswerFromTurns,
  latestResultFromTurns,
  mergeRunEvents,
  normalizeResult,
  readLocalTurns,
  requestErrorMessage,
  saveLocalTurns,
  statusLabel,
  turnsFromSnapshot,
  type ThreadTurn,
} from "@/lib/workspace";

const CONVERSATION_STORAGE_KEY = "movie-agent-conversation";

type SavedConversation = { id: string; version: number };

function readSavedConversation(): SavedConversation | null {
  try {
    const raw = window.sessionStorage.getItem(CONVERSATION_STORAGE_KEY);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (
      typeof value === "object" &&
      value !== null &&
      "id" in value &&
      typeof value.id === "string" &&
      "version" in value &&
      typeof value.version === "number"
    ) {
      return { id: value.id, version: value.version };
    }
  } catch {
    // A fresh conversation is safer than restoring a malformed tab pointer.
  }
  saveConversation(null);
  return null;
}

function saveConversation(conversation: SavedConversation | null) {
  try {
    if (conversation) {
      window.sessionStorage.setItem(CONVERSATION_STORAGE_KEY, JSON.stringify(conversation));
    } else {
      window.sessionStorage.removeItem(CONVERSATION_STORAGE_KEY);
    }
  } catch {
    // The current tab continues in memory if browser storage is unavailable.
  }
}

function resultFromSnapshot(snapshot: ConversationSnapshot): RecommendationResult | null {
  if (snapshot.result) {
    return normalizeResult(
      {
        ...snapshot.result,
        intent: snapshot.intent,
        clarification: snapshot.clarification ?? undefined,
        clarification_options: snapshot.clarification_options,
        issues: snapshot.issues ?? [],
        provider_lookups: snapshot.provider_lookups ?? [],
      },
      snapshot.intent,
    );
  }
  if (snapshot.clarification) {
    return normalizeResult(
      {
        intent: snapshot.intent,
        recommendations: [],
        requested_count:
          typeof snapshot.intent.requested_count === "number"
            ? snapshot.intent.requested_count
            : 5,
        shortfall_reason: null,
        clarification: snapshot.clarification,
        clarification_options: snapshot.clarification_options,
        issues: snapshot.issues ?? [],
        provider_lookups: snapshot.provider_lookups ?? [],
      },
      snapshot.intent,
    );
  }
  return null;
}

export function RecommendationWorkspace() {
  const [message, setMessage] = useState("");
  const [conversation, setConversation] = useState<SavedConversation | null>(null);
  const [turns, setTurns] = useState<ThreadTurn[]>([]);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [result, setResult] = useState<RecommendationResult | null>(null);
  const [answer, setAnswer] = useState<string | null>(null);
  const [status, setStatus] = useState("Ready");
  const [isRunning, setIsRunning] = useState(false);
  const [isRefreshingAvailability, setIsRefreshingAvailability] = useState(false);
  const [isRecovering, setIsRecovering] = useState(false);
  const [historyRestored, setHistoryRestored] = useState(false);
  const [capabilities, setCapabilities] = useState<RecommendationCapabilities | null>(null);
  const [availabilityError, setAvailabilityError] = useState<string | null>(null);
  const [availabilityNotice, setAvailabilityNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const turnsRef = useRef<ThreadTurn[]>([]);
  const conversationId = conversation?.id;

  function commitTurns(next: ThreadTurn[]) {
    turnsRef.current = next;
    setTurns(next);
  }

  useEffect(() => {
    let active = true;
    getRecommendationCapabilities().then((next) => {
      if (active) setCapabilities(next);
    });
    const timeout = window.setTimeout(() => {
      const saved = readSavedConversation();
      if (saved) setConversation(saved);
    }, 0);
    return () => {
      active = false;
      window.clearTimeout(timeout);
    };
  }, []);

  useEffect(() => {
    if (!conversationId) return;
    let active = true;
    getConversation(conversationId)
      .then((snapshot) => {
        if (!active) return;
        if (!snapshot) {
          setConversation(null);
          setResult(null);
          setTurns([]);
          turnsRef.current = [];
          saveConversation(null);
          return;
        }
        const restoredConversation = {
          id: snapshot.conversation_id,
          version: snapshot.version,
        };
        setConversation(restoredConversation);
        saveConversation(restoredConversation);
        const serverTurns = turnsFromSnapshot(snapshot);
        const localTurns = serverTurns.length ? [] : readLocalTurns(conversationId);
        const nextTurns = serverTurns.length ? serverTurns : localTurns;
        turnsRef.current = nextTurns;
        setTurns(nextTurns);
        setHistoryRestored(serverTurns.length > 0);
        const restoredResult =
          latestResultFromTurns(nextTurns) ?? resultFromSnapshot(snapshot);
        setResult(restoredResult);
        setAnswer(latestAnswerFromTurns(nextTurns));
        const restoredEvents = eventsFromTurns(nextTurns);
        if (restoredEvents.length) setEvents(restoredEvents);
      })
      .catch((reason) => {
        if (active) {
          setError(
            requestErrorMessage(reason, "Could not restore this conversation. Start a new search."),
          );
        }
      });
    return () => {
      active = false;
    };
  }, [conversationId]);

  async function submitTurn(options: {
    message: string;
    clarificationChoiceId?: string | null;
    filterEdit?: FilterEdit | null;
    clearPrompt?: boolean;
  }) {
    const text = options.message.trim();
    if (!text || isRunning) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const requestId = window.crypto.randomUUID();
    const turnId = requestId;
    const pendingTurn: ThreadTurn = {
      id: turnId,
      runId: null,
      userMessage: text,
      status: "Starting",
      result: null,
      answer: null,
      errorMessage: null,
      events: [],
      restored: false,
    };
    commitTurns([...turnsRef.current, pendingTurn]);
    setError(null);
    setAvailabilityError(null);
    setAvailabilityNotice(null);
    setAnswer(null);
    setStatus("Starting");
    setIsRunning(true);
    const runEvents: TraceEvent[] = [];
    const terminal: { event: TraceEvent | null } = { event: null };
    let runId: string | null = null;
    const currentIntent = result?.intent ?? {};
    const updateCurrentTurn = (updater: (turn: ThreadTurn) => ThreadTurn) => {
      commitTurns(
        turnsRef.current.map((turn) => (turn.id === turnId ? updater(turn) : turn)),
      );
    };

    try {
      const response = await streamRecommendations(
        {
          message: text,
          clientRequestId: requestId,
          conversationId: conversation?.id ?? null,
          expectedVersion: conversation?.version ?? null,
          clarificationChoiceId: options.clarificationChoiceId ?? null,
          filterEdit: options.filterEdit ?? null,
        },
        (nextEvent) => {
          runEvents.push(nextEvent);
          setEvents((current) => [...current, nextEvent]);
          if (nextEvent.type === "failed") {
            terminal.event = nextEvent;
            const failure = failureMessage(nextEvent);
            setStatus("Failed");
            setError(failure);
            updateCurrentTurn((turn) => ({
              ...turn,
              status: "Failed",
              errorMessage: failure,
              events: [...runEvents],
            }));
            return;
          }
          if (nextEvent.type === "cancelled") {
            terminal.event = nextEvent;
            setStatus("Cancelled");
            updateCurrentTurn((turn) => ({
              ...turn,
              status: "Cancelled",
              events: [...runEvents],
            }));
            return;
          }
          if (nextEvent.type === "needs_clarification") {
            terminal.event = nextEvent;
            const normalized = normalizeResult(nextEvent.data, currentIntent);
            setStatus("Needs clarification");
            if (normalized) setResult(normalized);
            updateCurrentTurn((turn) => ({
              ...turn,
              status: "Needs clarification",
              result: normalized,
              events: [...runEvents],
            }));
            return;
          }
          if (nextEvent.type === "recommendations") {
            const normalized = normalizeResult(nextEvent.data, currentIntent);
            if (normalized) {
              setResult(normalized);
              updateCurrentTurn((turn) => ({
                ...turn,
                result: normalized,
                status: "Preparing results",
                events: [...runEvents],
              }));
            }
            return;
          }
          if (nextEvent.type === "completed") {
            const nextAnswer =
              typeof nextEvent.data?.answer === "string" ? nextEvent.data.answer : null;
            if (nextAnswer) setAnswer(nextAnswer);
            setStatus("Complete");
            updateCurrentTurn((turn) => ({
              ...turn,
              status: "Complete",
              answer: nextAnswer ?? turn.answer,
              events: [...runEvents],
            }));
            return;
          }
          setStatus(statusLabel(nextEvent.type));
          updateCurrentTurn((turn) => ({
            ...turn,
            status: statusLabel(nextEvent.type),
            events: [...runEvents],
          }));
        },
        controller.signal,
        (ids) => {
          runId = ids.runId;
          updateCurrentTurn((turn) => ({ ...turn, runId: ids.runId }));
        },
      );

      if (terminal.event?.type === "failed" || terminal.event?.type === "cancelled") return;
      const nextConversation = {
        id: response.conversationId,
        version: (conversation?.version ?? 1) + 1,
      };
      setConversation(nextConversation);
      saveConversation(nextConversation);
      const finalStatus =
        terminal.event?.type === "needs_clarification" ? "Needs clarification" : "Complete";
      setStatus(finalStatus);
      updateCurrentTurn((turn) => ({ ...turn, status: finalStatus }));
      saveLocalTurns(nextConversation.id, turnsRef.current);
      if (options.clearPrompt !== false) setMessage("");
    } catch (reason) {
      if (terminal.event) return;
      if (controller.signal.aborted) {
        setStatus("Cancelled");
        updateCurrentTurn((turn) => ({ ...turn, status: "Cancelled" }));
        return;
      }

      let recovered = false;
      if (runId) {
        let supportsRunStatus = capabilities?.run_status === true;
        if (!capabilities) {
          const lateCapabilities = await getRecommendationCapabilities();
          if (lateCapabilities) {
            setCapabilities(lateCapabilities);
            supportsRunStatus = lateCapabilities.run_status;
          }
        }
        if (supportsRunStatus) {
          setIsRecovering(true);
          setStatus("Checking saved run");
          try {
            const recoveredRun = await getRunStatus(runId);
            if (recoveredRun) {
              recovered = true;
              const recoveredEvents = Array.isArray(recoveredRun.events)
                ? recoveredRun.events
                : [];
              if (recoveredEvents.length) {
                setEvents((current) => mergeRunEvents(current, runId as string, recoveredEvents));
              }
              const recoveredResult = normalizeResult(recoveredRun.response, currentIntent);
              const recoveredLabel = statusLabel(recoveredRun.status);
              if (recoveredRun.status === "failed") {
                const failedEvent = [...recoveredEvents]
                  .reverse()
                  .find((event) => event.type === "failed");
                const failure = failedEvent
                  ? failureMessage(failedEvent)
                  : "The recommendation run could not be completed.";
                setStatus("Failed");
                setError(failure);
                updateCurrentTurn((turn) => ({
                  ...turn,
                  status: "Failed",
                  errorMessage: failure,
                  events: recoveredEvents,
                  runId,
                }));
              } else if (recoveredRun.status === "cancelled") {
                setStatus("Cancelled");
                updateCurrentTurn((turn) => ({
                  ...turn,
                  status: "Cancelled",
                  events: recoveredEvents,
                  runId,
                }));
              } else if (
                recoveredRun.status === "completed" ||
                recoveredRun.status === "needs_clarification"
              ) {
                if (recoveredResult) setResult(recoveredResult);
                setStatus(recoveredLabel);
                setError(null);
                updateCurrentTurn((turn) => ({
                  ...turn,
                  status: recoveredLabel,
                  result: recoveredResult ?? turn.result,
                  events: recoveredEvents,
                  runId,
                }));
                const nextConversation = {
                  id: recoveredRun.conversation_id,
                  version: recoveredRun.version,
                };
                setConversation(nextConversation);
                saveConversation(nextConversation);
                saveLocalTurns(nextConversation.id, turnsRef.current);
              } else {
                const notice = interruptionMessage(true);
                setStatus("Interrupted");
                setError(notice);
                updateCurrentTurn((turn) => ({
                  ...turn,
                  status: "Interrupted",
                  errorMessage: notice,
                  events: recoveredEvents,
                  runId,
                }));
              }
            }
          } catch {
            recovered = false;
          } finally {
            setIsRecovering(false);
          }
        }
      }
      if (recovered) return;

      const rawMessage = reason instanceof Error ? reason.message : "";
      const networkInterruption =
        Boolean(runId) ||
        (/network|fetch|terminated|chunk|body/i.test(rawMessage) &&
          !/status \d+/i.test(rawMessage));
      const failure = networkInterruption
        ? interruptionMessage(false)
        : requestErrorMessage(reason, "The recommendation request failed.");
      setStatus(networkInterruption ? "Interrupted" : "Failed");
      setError(failure);
      updateCurrentTurn((turn) => ({
        ...turn,
        status: networkInterruption ? "Interrupted" : "Failed",
        errorMessage: failure,
      }));
    } finally {
      setIsRunning(false);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void submitTurn({ message });
  }

  function chooseClarification(option: ClarificationOption) {
    const structured = capabilities?.structured_clarification === true;
    void submitTurn({
      message: choiceMessage(option),
      clarificationChoiceId: structured ? option.choice_id : null,
      clearPrompt: false,
    });
  }

  function applyFilterEdit(edit: FilterEdit) {
    const typed = capabilities?.filter_edit === true;
    void submitTurn({
      message: filterEditMessage(edit),
      filterEdit: typed ? edit : null,
      clearPrompt: false,
    });
  }

  function relaxUnmetFilter(unmetFilter: string) {
    const filters = result ? activeFilters(result.intent) : [];
    const needle = unmetFilter.toLowerCase();
    const matched = filters.find(
      (filter) =>
        filter.remove &&
        (needle.includes(filter.group.toLowerCase()) ||
          needle.includes(String(filter.value).toLowerCase()) ||
          String(filter.value).toLowerCase().includes(needle)),
    );
    if (matched?.remove) {
      applyFilterEdit(matched.remove);
      return;
    }
    void submitTurn({
      message: `Please relax this filter: ${unmetFilter}.`,
      clearPrompt: false,
    });
  }

  function focusActiveFilters() {
    const node = document.getElementById("active-filters");
    node?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    node?.focus({ preventScroll: true });
  }

  function startNewConversation() {
    abortRef.current?.abort();
    if (conversation?.id) clearLocalTurns(conversation.id);
    setConversation(null);
    setTurns([]);
    turnsRef.current = [];
    setHistoryRestored(false);
    setResult(null);
    setAnswer(null);
    setEvents([]);
    setMessage("");
    setError(null);
    setAvailabilityError(null);
    setAvailabilityNotice(null);
    setStatus("Ready");
    saveConversation(null);
  }

  async function changeAvailabilityCountry(country: string): Promise<boolean> {
    if (!conversation || !result || isRunning || isRefreshingAvailability) return false;
    setIsRefreshingAvailability(true);
    setAvailabilityError(null);
    setAvailabilityNotice(null);
    try {
      const snapshot = await refreshConversationAvailability(
        conversation.id,
        country,
        conversation.version,
      );
      const nextConversation = { id: snapshot.conversation_id, version: snapshot.version };
      setConversation(nextConversation);
      saveConversation(nextConversation);
      if (snapshot.result) {
        const refreshed = normalizeResult(
          {
            ...snapshot.result,
            intent: snapshot.intent,
            clarification: snapshot.clarification ?? undefined,
            clarification_options: snapshot.clarification_options,
            issues: snapshot.issues ?? [],
            provider_lookups: snapshot.provider_lookups ?? [],
          },
          snapshot.intent,
        );
        if (refreshed) setResult(refreshed);
      }
      setAvailabilityNotice(`Streaming availability updated for ${countryName(country)}.`);
      return true;
    } catch (reason) {
      setAvailabilityError(
        requestErrorMessage(
          reason,
          "Could not update country availability. Your saved preferences were not changed.",
        ),
      );
      return false;
    } finally {
      setIsRefreshingAvailability(false);
    }
  }

  return (
    <main className="workspace">
      <section className="hero">
        <p className="eyebrow">Movie Agent · grounded picks</p>
        <div className="workspaceHeading">
          <div>
            <h1>What should we watch tonight?</h1>
            <p className="lede">
              Describe the movie you want. Refine the results by chatting, or ask for another set.
            </p>
          </div>
          {conversation ? (
            <button className="quietButton" onClick={startNewConversation} type="button">
              New search
            </button>
          ) : null}
        </div>

        <form onSubmit={handleSubmit}>
          <label htmlFor="prompt">Tell me what you&apos;re in the mood for</label>
          <textarea
            id="prompt"
            maxLength={1200}
            minLength={1}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="For example: three comedies with Jim Carrey"
            required
            rows={3}
            value={message}
          />
          <div className="formActions">
            <span>{message.length}/1200</span>
            <div className="actionButtons">
              {isRunning ? (
                <button
                  className="quietButton"
                  onClick={() => abortRef.current?.abort()}
                  type="button"
                >
                  Cancel
                </button>
              ) : null}
              <button disabled={isRunning || !message.trim()} type="submit">
                Get recommendations
              </button>
            </div>
          </div>
        </form>
      </section>

      <ConversationThread historyRestored={historyRestored} turns={turns} />

      <ProgressTrace
        error={error}
        events={events}
        isRunning={isRunning || isRecovering}
        status={isRecovering ? "Checking saved run" : status}
      />
      {answer ? (
        <p className="conversationAnswer" role="status">
          {answer}
        </p>
      ) : null}

      {result ? (
        <>
          <ActiveFilters
            disabled={isRunning}
            intent={result.intent}
            onApplyEdit={applyFilterEdit}
          />
          <RecommendationResults
            error={availabilityError}
            isRefreshing={isRefreshingAvailability}
            isRunning={isRunning}
            notice={availabilityNotice}
            onChooseClarification={chooseClarification}
            onCountryChange={changeAvailabilityCountry}
            onEditFilters={focusActiveFilters}
            onRelaxFilter={relaxUnmetFilter}
            result={result}
          />
        </>
      ) : null}
      <footer className="attribution" aria-label="Credits">
        <a href="https://www.themoviedb.org/" rel="noreferrer" target="_blank">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            alt="TMDB"
            height={28}
            loading="lazy"
            src="https://www.themoviedb.org/assets/2/v4/logos/v2/blue_square_2-d537fb228cf3ded904ef09b136fe3fec72548ebc1fea3fbbd1ad9e36364db38b.svg"
            width={28}
          />
        </a>
        <span>
          This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise
          approved by TMDB.
        </span>
        <span>Streaming availability data is provided through JustWatch.</span>
      </footer>
    </main>
  );
}
