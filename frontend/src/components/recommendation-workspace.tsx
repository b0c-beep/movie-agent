"use client";

import { useEffect, useRef, useState } from "react";

import { ChatExperience } from "@/components/chat-experience";
import {
  getRecommendationCapabilities,
  getRunStatus,
  refreshConversationAvailability,
  streamRecommendations,
  type ClarificationOption,
  type FilterEdit,
  type RecommendationCapabilities,
  type RecommendationResult,
  type TraceEvent,
} from "@/lib/api";
import { countryName } from "@/lib/countries";
import {
  activeFilters,
  choiceMessage,
  failureMessage,
  filterEditMessage,
  interruptionMessage,
  normalizeResult,
  requestErrorMessage,
  statusLabel,
  type ThreadTurn,
} from "@/lib/workspace";

type SavedConversation = { id: string; version: number };

export function RecommendationWorkspace() {
  const [message, setMessage] = useState("");
  const [selectedCountry, setSelectedCountry] = useState("RO");
  const [conversation, setConversation] = useState<SavedConversation | null>(null);
  const [turns, setTurns] = useState<ThreadTurn[]>([]);
  const [result, setResult] = useState<RecommendationResult | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [isRefreshingAvailability, setIsRefreshingAvailability] = useState(false);
  const [isRecovering, setIsRecovering] = useState(false);
  const [capabilities, setCapabilities] = useState<RecommendationCapabilities | null>(null);
  const [availabilityError, setAvailabilityError] = useState<string | null>(null);
  const [availabilityNotice, setAvailabilityNotice] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const turnsRef = useRef<ThreadTurn[]>([]);

  function commitTurns(next: ThreadTurn[]) {
    turnsRef.current = next;
    setTurns(next);
  }

  useEffect(() => {
    let active = true;
    getRecommendationCapabilities().then((next) => {
      if (active) setCapabilities(next);
    });
    return () => {
      active = false;
    };
  }, []);

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
    };
    commitTurns([...turnsRef.current, pendingTurn]);
    setAvailabilityError(null);
    setAvailabilityNotice(null);
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
          if (nextEvent.type === "failed") {
            terminal.event = nextEvent;
            const failure = failureMessage(nextEvent);
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
            updateCurrentTurn((turn) => ({
              ...turn,
              status: "Complete",
              answer: nextAnswer ?? turn.answer,
              events: [...runEvents],
            }));
            return;
          }
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
      const finalStatus =
        terminal.event?.type === "needs_clarification" ? "Needs clarification" : "Complete";
      updateCurrentTurn((turn) => ({ ...turn, status: finalStatus }));
      if (options.clearPrompt !== false) setMessage("");
    } catch (reason) {
      if (terminal.event) return;
      if (controller.signal.aborted) {
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
          try {
            const recoveredRun = await getRunStatus(runId);
            if (recoveredRun) {
              recovered = true;
              const recoveredEvents = Array.isArray(recoveredRun.events)
                ? recoveredRun.events
                : [];
              if (recoveredEvents.length) {
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
                updateCurrentTurn((turn) => ({
                  ...turn,
                  status: "Failed",
                  errorMessage: failure,
                  events: recoveredEvents,
                  runId,
                }));
              } else if (recoveredRun.status === "cancelled") {
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
              } else {
                const notice = interruptionMessage(true);
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
      updateCurrentTurn((turn) => ({
        ...turn,
        status: networkInterruption ? "Interrupted" : "Failed",
        errorMessage: failure,
      }));
    } finally {
      setIsRunning(false);
    }
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

  function startNewConversation() {
    abortRef.current?.abort();
    setConversation(null);
    setTurns([]);
    turnsRef.current = [];
    setResult(null);
    setMessage("");
    setAvailabilityError(null);
    setAvailabilityNotice(null);
    setSelectedCountry("RO");
  }

  async function changeAvailabilityCountry(country: string): Promise<boolean> {
    if (isRunning || isRefreshingAvailability) return false;
    if (!conversation || !result) {
      setSelectedCountry(country);
      return true;
    }
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
      setSelectedCountry(country);
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
        if (refreshed) {
          const latestTurnId = turnsRef.current.at(-1)?.id;
          if (latestTurnId) {
            commitTurns(
              turnsRef.current.map((turn) =>
                turn.id === latestTurnId ? { ...turn, result: refreshed } : turn,
              ),
            );
          }
        }
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
    <ChatExperience
      availabilityError={availabilityError}
      availabilityNotice={availabilityNotice}
      currentIntent={result?.intent ?? {}}
      isRecovering={isRecovering}
      isRefreshingAvailability={isRefreshingAvailability}
      isRunning={isRunning}
      message={message}
      onApplyFilterEdit={applyFilterEdit}
      onCancel={() => abortRef.current?.abort()}
      onChooseClarification={chooseClarification}
      onCountryChange={changeAvailabilityCountry}
      onMessageChange={setMessage}
      onNewChat={startNewConversation}
      onRelaxFilter={relaxUnmetFilter}
      onSend={(nextMessage) => void submitTurn({ message: nextMessage })}
      selectedCountry={selectedCountry}
      turns={turns}
    />
  );
}
