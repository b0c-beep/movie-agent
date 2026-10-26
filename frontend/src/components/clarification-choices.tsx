"use client";

import { useState } from "react";

import type { ClarificationOption } from "@/lib/api";

export function ClarificationChoices({
  options,
  disabled,
  onChoose,
}: {
  options: ClarificationOption[];
  disabled: boolean;
  onChoose: (option: ClarificationOption) => void;
}) {
  const [selectedId, setSelectedId] = useState(options[0]?.choice_id ?? "");
  const selected = options.find((option) => option.choice_id === selectedId) ?? options[0];

  if (options.length === 0) return null;

  return (
    <fieldset className="clarificationChoices" disabled={disabled} tabIndex={-1}>
      <legend>Choose the closest match</legend>
      {options.map((option) => (
        <label className="clarificationOption" key={option.choice_id}>
          <input
            checked={selectedId === option.choice_id}
            name="clarification-choice"
            onChange={() => setSelectedId(option.choice_id)}
            type="radio"
            value={option.choice_id}
          />
          <span>
            <strong>{option.label}</strong>
            {option.detail ? <span className="choiceDetail">{option.detail}</span> : null}
            <span className="choiceKind">{option.kind === "person" ? "Person" : "Title"}</span>
          </span>
        </label>
      ))}
      <div className="clarificationActions">
        <button
          disabled={disabled || !selected}
          onClick={() => {
            if (selected) onChoose(selected);
          }}
          type="button"
        >
          Use this choice
        </button>
        <span className="muted">Or reply in your own words below.</span>
      </div>
    </fieldset>
  );
}
