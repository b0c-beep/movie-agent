"use client";

import type { RecommendationResult } from "@/lib/api";
import type { ThreadTurn } from "@/lib/workspace";

function ResultSummary({ result }: { result: RecommendationResult }) {
  if (result.clarification) {
    return <p className="threadSummary">Needs clarification: {result.clarification}</p>;
  }
  const alternatives = result.alternatives ?? [];
  if (result.recommendations.length) {
    const titles = result.recommendations.map((item) => item.movie.title).join(", ");
    return (
      <p className="threadSummary">
        {result.recommendations.length} of {result.requested_count} exact picks: {titles}
        {alternatives.length ? `; plus ${alternatives.length} close alternative${alternatives.length === 1 ? "" : "s"}.` : "."}
      </p>
    );
  }
  if (alternatives.length) {
    return (
      <p className="threadSummary">
        No exact matches; {alternatives.length} close alternative{alternatives.length === 1 ? "" : "s"} shown.
      </p>
    );
  }
  return <p className="threadSummary">No verified matches for this turn.</p>;
}

export function ConversationThread({
  turns,
  historyRestored,
}: {
  turns: ThreadTurn[];
  historyRestored: boolean;
}) {
  const hasLocalOnly = !historyRestored && turns.length > 0;
  return (
    <section className="thread" aria-labelledby="thread-heading">
      <div className="threadHeader">
        <h2 id="thread-heading">Conversation</h2>
        <span className="threadSource">
          {historyRestored
            ? "Restored from this conversation."
            : hasLocalOnly
              ? "Kept in this tab; older history is not available from this backend."
              : "Only this conversation appears here."}
        </span>
      </div>
      {turns.length === 0 ? (
        <p className="empty">Your messages and the agent replies will appear here.</p>
      ) : (
        <ol className="threadList">
          {turns.map((turn) => (
            <li className="threadTurn" key={turn.id}>
              <div className="threadMessage threadUser">
                <span className="threadRole">You</span>
                <p>{turn.userMessage}</p>
              </div>
              <div className="threadMessage threadAgent">
                <span className="threadRole">
                  Movie Agent <span className="turnStatus">{turn.status}</span>
                </span>
                {turn.errorMessage ? (
                  <p className="threadError">{turn.errorMessage}</p>
                ) : null}
                {turn.answer ? <p className="threadAnswer">{turn.answer}</p> : null}
                {turn.result ? <ResultSummary result={turn.result} /> : null}
                {!turn.result && !turn.answer && !turn.errorMessage ? (
                  <p className="threadSummary">{turn.status}…</p>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
