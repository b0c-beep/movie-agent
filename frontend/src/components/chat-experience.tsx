"use client";

import { useEffect, useRef, useState, type ChangeEvent, type FormEvent, type KeyboardEvent } from "react";
import Link from "next/link";

import { ActiveFilters } from "@/components/active-filters";
import { ClarificationChoices } from "@/components/clarification-choices";
import { MovieCard } from "@/components/movie-card";
import { eventTypeLabel } from "@/components/progress-trace";
import type {
  ClarificationOption,
  FilterEdit,
  TraceEvent,
} from "@/lib/api";
import { COUNTRY_OPTIONS, countryName } from "@/lib/countries";
import type { ThreadTurn } from "@/lib/workspace";

export const EXAMPLE_PROMPTS = [
  "Show me three action movies.",
  "Recommend three movies featuring both Tom Hanks and Meg Ryan.",
  "I want three suspenseful movies for tonight.",
  "Recommend three Japanese animated movies under 120 minutes.",
  "Find three movies rated at least 7.0 on TMDB.",
  "Recommend three movies similar to The Matrix (1999).",
  "Show me three French-language dramas released from 2000 through 2020.",
  "Find five thrillers starring Denzel Washington released after 2000.",
  "Find three science fiction movies under 110 minutes and check streaming in Canada.",
  "Find comedies with Jim Carrey from 1990 through 2000, under 130 minutes.",
];

const TERMINAL_EVENTS = new Set([
  "recommendations",
  "completed",
  "needs_clarification",
  "failed",
  "cancelled",
]);

function publicEvents(events: TraceEvent[]) {
  return events.filter((event) => !TERMINAL_EVENTS.has(event.type));
}

function ActivityMessage({ event }: { event: TraceEvent }) {
  return (
    <div className="activityMessage" key={event.event_id}>
      <div className="activityMeta">
        <span>{event.stage}</span>
        <span>{event.origin === "agent" ? "Agent" : "Application"}</span>
        <span>{eventTypeLabel(event.type)}</span>
      </div>
      <p>{event.summary}</p>
    </div>
  );
}

function AgentResponse({
  turn,
  disabled,
  onChooseClarification,
  onRelaxFilter,
  onUsePrompt,
}: {
  turn: ThreadTurn;
  disabled: boolean;
  onChooseClarification: (option: ClarificationOption) => void;
  onRelaxFilter: (filter: string) => void;
  onUsePrompt: (prompt: string) => void;
}) {
  const result = turn.result;
  const alternatives = result?.alternatives ?? [];
  const recommendations = result?.recommendations ?? [];
  return (
    <div className="agentReply">
      <div className="agentReplyHeader">
        <span className="agentName">Movie Agent</span>
        <span className="turnStatus">{turn.status}</span>
      </div>
      {publicEvents(turn.events).map((event) => (
        <ActivityMessage event={event} key={event.event_id} />
      ))}
      {!turn.events.length && !turn.result && !turn.errorMessage ? (
        <div className="activityMessage"><p>Starting the search…</p></div>
      ) : null}
      {turn.errorMessage ? (
        <p className="agentError" role="alert" tabIndex={-1}>
          {turn.errorMessage}
        </p>
      ) : null}
      {turn.answer ? (
        <p className="agentAnswer" role="status">
          {turn.answer}
        </p>
      ) : null}
      {result?.clarification ? (
        <div className="agentText">
          <p>{result.clarification}</p>
          {result.clarification_options?.length ? (
            <ClarificationChoices
              disabled={disabled}
              key={result.clarification_options.map((option) => option.choice_id).join("|")}
              onChoose={onChooseClarification}
              options={result.clarification_options}
            />
          ) : (
            <p className="muted">Reply with the person, title, or detail you meant.</p>
          )}
        </div>
      ) : null}
      {result?.shortfall_reason ? (
        <p className="agentShortfall" role="status">
          {result.shortfall_reason}
        </p>
      ) : null}
      {recommendations.length ? (
        <section aria-label="Exact movie matches" className="chatFilmSection">
          <h3>
            {recommendations.length} of {result?.requested_count} exact picks
          </h3>
          <div className="chatMovieGrid">
            {recommendations.map((recommendation) => (
              <MovieCard
                key={recommendation.movie.tmdb_id}
                providerLookup={result?.provider_lookups.find(
                  (lookup) => lookup.tmdb_id === recommendation.movie.tmdb_id,
                )}
                recommendation={recommendation}
              />
            ))}
          </div>
        </section>
      ) : null}
      {alternatives.length ? (
        <section aria-label="Closest movie alternatives" className="chatFilmSection">
          <h3>Closest alternatives</h3>
          <p className="muted">These grounded results miss at least one requested filter.</p>
          <div className="chatMovieGrid">
            {alternatives.slice(0, 3).map((alternative) => (
              <div
                className="chatAlternative"
                key={`${alternative.recommendation.movie.tmdb_id}-${alternative.unmet_filters.join("|")}`}
              >
                <MovieCard
                  isAlternative
                  providerLookup={result?.provider_lookups.find(
                    (lookup) => lookup.tmdb_id === alternative.recommendation.movie.tmdb_id,
                  )}
                  recommendation={alternative.recommendation}
                />
                <div className="unmetFilters">
                  <strong>Unmet filters</strong>
                  <ul>
                    {alternative.unmet_filters.map((filter) => (
                      <li key={filter}>
                        <span>{filter}</span>
                        <button
                          className="filterButton"
                          disabled={disabled}
                          onClick={() => onRelaxFilter(filter)}
                          type="button"
                        >
                          Relax
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}
      {result && !result.clarification && recommendations.length === 0 && alternatives.length === 0 ? (
        <p className="agentShortfall" role="status">
          {result.shortfall_reason || "No verified matches for this turn."}
        </p>
      ) : null}
      {result && (recommendations.length > 0 || alternatives.length > 0) ? (
        <div aria-label="Follow-up suggestions" className="followUpPrompts">
          <button onClick={() => onUsePrompt("Show me another set.")} type="button">
            Another set
          </button>
          <button onClick={() => onUsePrompt("Make those movies newer.")} type="button">
            Make them newer
          </button>
          {recommendations[1] ? (
            <button
              onClick={() =>
                onUsePrompt(
                  `Tell me about ${recommendations[1].movie.title}${recommendations[1].movie.release_year ? ` (${recommendations[1].movie.release_year})` : ""}.`,
                )
              }
              type="button"
            >
              Ask about the second movie
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function ChatExperience({
  turns,
  message,
  selectedCountry,
  currentIntent,
  isRunning,
  isRecovering,
  isRefreshingAvailability,
  availabilityError,
  availabilityNotice,
  onMessageChange,
  onSend,
  onCancel,
  onNewChat,
  onCountryChange,
  onApplyFilterEdit,
  onChooseClarification,
  onRelaxFilter,
}: {
  turns: ThreadTurn[];
  message: string;
  selectedCountry: string;
  currentIntent: Record<string, unknown>;
  isRunning: boolean;
  isRecovering: boolean;
  isRefreshingAvailability: boolean;
  availabilityError: string | null;
  availabilityNotice: string | null;
  onMessageChange: (message: string) => void;
  onSend: (message: string) => void;
  onCancel: () => void;
  onNewChat: () => void;
  onCountryChange: (country: string) => Promise<boolean>;
  onApplyFilterEdit: (edit: FilterEdit) => void;
  onChooseClarification: (option: ClarificationOption) => void;
  onRelaxFilter: (filter: string) => void;
}) {
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const stickToLatestRef = useRef(true);
  const [isAtLatest, setIsAtLatest] = useState(true);
  const [activeSuggestion, setActiveSuggestion] = useState(-1);
  const [suggestionsDismissed, setSuggestionsDismissed] = useState(false);
  const isEmpty = turns.length === 0;
  const matchingPrompts = EXAMPLE_PROMPTS.filter((prompt) =>
    message.trim() ? prompt.toLowerCase().includes(message.trim().toLowerCase()) : true,
  );

  useEffect(() => {
    const node = transcriptRef.current;
    if (node && stickToLatestRef.current) node.scrollTop = node.scrollHeight;
  }, [turns, availabilityError, availabilityNotice]);

  useEffect(() => {
    if (isRunning) return;
    const latest = turns.at(-1);
    if (latest?.errorMessage) {
      Array.from(transcriptRef.current?.querySelectorAll<HTMLElement>(".agentError") ?? []).pop()?.focus();
    }
    else if (latest?.result?.clarification) {
      Array.from(
        transcriptRef.current?.querySelectorAll<HTMLElement>(".clarificationChoices") ?? [],
      ).pop()?.focus();
    } else if (availabilityError) {
      Array.from(transcriptRef.current?.querySelectorAll<HTMLElement>("[role=alert]") ?? []).pop()?.focus();
    }
  }, [turns, isRunning, availabilityError]);

  function handleScroll() {
    const node = transcriptRef.current;
    if (!node) return;
    const atLatest = node.scrollHeight - node.scrollTop - node.clientHeight < 40;
    stickToLatestRef.current = atLatest;
    setIsAtLatest(atLatest);
  }

  function jumpToLatest() {
    const node = transcriptRef.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
    stickToLatestRef.current = true;
    setIsAtLatest(true);
  }

  function fillPrompt(prompt: string) {
    onMessageChange(prompt);
    setActiveSuggestion(-1);
    setSuggestionsDismissed(false);
    requestAnimationFrame(() => {
      const composer = composerRef.current;
      composer?.focus();
      composer?.setSelectionRange(prompt.length, prompt.length);
    });
  }

  function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isRunning || !message.trim()) return;
    onSend(message);
    requestAnimationFrame(() => composerRef.current?.focus());
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (isEmpty && !suggestionsDismissed && matchingPrompts.length && event.key === "ArrowDown") {
      event.preventDefault();
      setActiveSuggestion((index) => Math.min(index + 1, matchingPrompts.length - 1));
      return;
    }
    if (isEmpty && !suggestionsDismissed && matchingPrompts.length && event.key === "ArrowUp") {
      event.preventDefault();
      setActiveSuggestion((index) => Math.max(index - 1, 0));
      return;
    }
    if (event.key === "Escape") {
      setActiveSuggestion(-1);
      setSuggestionsDismissed(true);
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      if (activeSuggestion >= 0 && matchingPrompts[activeSuggestion]) {
        event.preventDefault();
        fillPrompt(matchingPrompts[activeSuggestion]);
      } else {
        event.preventDefault();
        composerRef.current?.form?.requestSubmit();
      }
    }
  }

  async function handleCountryChange(event: ChangeEvent<HTMLSelectElement>) {
    const select = event.currentTarget;
    if (await onCountryChange(select.value)) requestAnimationFrame(() => select.focus());
  }

  return (
    <main className="chatShell">
      <div aria-hidden="true" className="paperGrain" />
      <header className="chatHeader">
          <Link aria-label="Movie Agent home" className="brandMark" href="/">
          <span aria-hidden="true" className="brandSeal">M</span>
          <span>Movie Agent</span>
          </Link>
        {turns.length ? (
          <button className="newChatButton" onClick={onNewChat} type="button">
            New chat
          </button>
        ) : null}
      </header>

      <div
        aria-label="Conversation"
        className="chatTranscript"
        onScroll={handleScroll}
        ref={transcriptRef}
        role="log"
        aria-live="polite"
        aria-relevant="additions text"
        tabIndex={0}
      >
        {isEmpty ? (
          <div className="emptyConversation">
            <div className="welcomeReply">
              <span className="agentName">Movie Agent</span>
              <h1>What should we watch tonight?</h1>
              <p>Tell me what you’re in the mood for, or start with one of these examples.</p>
            </div>
          </div>
        ) : (
          <ol className="chatTurns">
            {turns.map((turn) => (
              <li className="chatTurn" key={turn.id}>
                <div className="userMessage">
                  <span className="messageName">You</span>
                  <p>{turn.userMessage}</p>
                </div>
                <AgentResponse
                  disabled={isRunning}
                  onChooseClarification={onChooseClarification}
                  onRelaxFilter={onRelaxFilter}
                  onUsePrompt={fillPrompt}
                  turn={turn}
                />
              </li>
            ))}
            {availabilityNotice ? (
              <li className="activityMessage" role="status">{availabilityNotice}</li>
            ) : null}
            {availabilityError ? (
              <li className="activityMessage agentError" role="alert">{availabilityError}</li>
            ) : null}
          </ol>
        )}
        <div aria-hidden="true" className="chatEnd" />
      </div>
      <aside aria-label="Chat tools" className="chatSideRail">
        <ActiveFilters
          disabled={isRunning}
          intent={currentIntent}
          onApplyEdit={onApplyFilterEdit}
        />
        <section aria-label="Example prompts" className="examplePrompts">
          <h2>{message.trim() ? "Matching examples" : "Try a prompt"}</h2>
          {!suggestionsDismissed && matchingPrompts.length ? (
            <ul id="example-prompts" role="listbox">
              {matchingPrompts.map((prompt, index) => (
                <li key={prompt}>
                  <button
                    aria-selected={activeSuggestion === index}
                    className="examplePrompt"
                    id={`example-prompt-${index}`}
                    onClick={() => fillPrompt(prompt)}
                    role="option"
                    type="button"
                  >
                    {prompt}
                  </button>
                </li>
              ))}
            </ul>
          ) : !suggestionsDismissed ? (
            <p className="muted">No matching examples. Your message can still be sent.</p>
          ) : null}
        </section>
      </aside>
      {!isAtLatest && turns.length ? (
        <button className="jumpToLatest" onClick={jumpToLatest} type="button">
          Jump to latest
        </button>
      ) : null}

      <div className="chatComposerArea">
        <section aria-label="Availability settings" className="chatControls">
          <div className="countryPicker chatCountryPicker">
            <label htmlFor="availability-country">Streaming country</label>
            <select
              aria-disabled={isRefreshingAvailability || isRunning}
              disabled={isRunning || isRefreshingAvailability}
              id="availability-country"
              onChange={(event) => void handleCountryChange(event)}
              value={selectedCountry}
            >
              {COUNTRY_OPTIONS.map(([code, label]) => (
                <option key={code} value={code}>
                  {label} ({code})
                </option>
              ))}
            </select>
            <span aria-live="polite" className="countryStatus">
              {isRecovering
                ? "Checking the saved run…"
                : isRefreshingAvailability
                  ? "Refreshing providers…"
                  : `Provider availability in ${countryName(selectedCountry)}.`}
            </span>
          </div>
        </section>

        <form className="chatComposer" onSubmit={sendMessage}>
          <label className="srOnly" htmlFor="chat-message">Message</label>
          <textarea
            aria-activedescendant={activeSuggestion >= 0 ? `example-prompt-${activeSuggestion}` : undefined}
            aria-autocomplete={!suggestionsDismissed ? "list" : "none"}
            aria-controls={!suggestionsDismissed ? "example-prompts" : undefined}
            aria-expanded={!suggestionsDismissed && matchingPrompts.length > 0}
            aria-haspopup="listbox"
            autoComplete="off"
            id="chat-message"
            maxLength={1200}
            onChange={(event) => {
              onMessageChange(event.target.value);
              setActiveSuggestion(-1);
              setSuggestionsDismissed(false);
            }}
            onKeyDown={handleComposerKeyDown}
              placeholder="Write a message…"
            role="combobox"
            ref={composerRef}
            rows={1}
            value={message}
          />
          <div className="composerActions">
            <span className="composerHint">Enter to send · Shift + Enter for a new line</span>
            {isRunning ? (
              <button className="cancelButton" onClick={onCancel} type="button">
                Cancel
              </button>
            ) : null}
            <button className="sendButton" disabled={isRunning || !message.trim()} type="submit">
              Send
            </button>
          </div>
        </form>
        <footer aria-label="Credits" className="attribution chatAttribution">
          <span>This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by TMDB.</span>
          <span>Streaming availability data is provided through JustWatch.</span>
        </footer>
      </div>
    </main>
  );
}
