"use client";

import { FormEvent, useState } from "react";

import type { FilterEdit } from "@/lib/api";
import { activeFilters, editValueForFilter, type ActiveFilter } from "@/lib/workspace";

function inputId(filter: ActiveFilter): string {
  return `filter-edit-${filter.id.replace(/[^a-z0-9-]+/gi, "-")}`;
}

export function ActiveFilters({
  intent,
  disabled,
  onApplyEdit,
}: {
  intent: Record<string, unknown>;
  disabled: boolean;
  onApplyEdit: (edit: FilterEdit) => void;
}) {
  const filters = activeFilters(intent);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  function startEditing(filter: ActiveFilter) {
    setEditingId(filter.id);
    setDraft(String(filter.value));
  }

  function submitEdit(event: FormEvent<HTMLFormElement>, filter: ActiveFilter) {
    event.preventDefault();
    const edit = editValueForFilter(filter, draft);
    if (!edit) return;
    setEditingId(null);
    onApplyEdit(edit);
  }

  return (
    <section
      aria-labelledby="active-filters-heading"
      className="activeFilters"
      id="active-filters"
      tabIndex={-1}
    >
      <div className="activeFiltersHeading">
        <h2 id="active-filters-heading">Active filters</h2>
        <p className="muted">Remove or revise a saved preference, or describe a change in the prompt.</p>
      </div>
      {filters.length === 0 ? (
        <p className="empty">No saved filters yet. Describe a genre, actor, year, or mood to begin.</p>
      ) : (
        <ul className="filterList">
          {filters.map((filter) => (
            <li className="filterChip" key={filter.id}>
              <span className="filterText">
                <span className="filterGroup">{filter.group}:</span> {filter.display}
              </span>
              {filter.remove ? (
                <button
                  aria-label={`Remove ${filter.group} ${filter.display} filter`}
                  className="filterButton"
                  disabled={disabled}
                  onClick={() => onApplyEdit(filter.remove as FilterEdit)}
                  type="button"
                >
                  Remove
                </button>
              ) : null}
              {filter.editable ? (
                <button
                  aria-expanded={editingId === filter.id}
                  aria-label={`Edit ${filter.group} filter`}
                  className="filterButton"
                  disabled={disabled}
                  onClick={() => startEditing(filter)}
                  type="button"
                >
                  Edit
                </button>
              ) : null}
              {filter.field === "country" ? (
                <span className="filterNote">Managed above</span>
              ) : null}
              {editingId === filter.id ? (
                <form className="filterEditForm" onSubmit={(event) => submitEdit(event, filter)}>
                  <label htmlFor={inputId(filter)}>New {filter.group.toLowerCase()}</label>
                  <input
                    autoComplete="off"
                    id={inputId(filter)}
                    inputMode={filter.inputType === "number" ? "decimal" : "text"}
                    maxLength={filter.inputType === "number" ? 5 : 120}
                    onChange={(event) => setDraft(event.target.value)}
                    step={filter.inputStep}
                    type={filter.inputType ?? "text"}
                    value={draft}
                  />
                  <div className="filterEditActions">
                    <button disabled={disabled} type="submit">
                      Save filter
                    </button>
                    <button
                      className="quietButton"
                      onClick={() => setEditingId(null)}
                      type="button"
                    >
                      Cancel edit
                    </button>
                  </div>
                </form>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
