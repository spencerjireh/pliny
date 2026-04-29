import { useEffect, useState } from "react";

interface SearchBarProps {
  value: string;
  onChange: (next: string) => void;
}

export function SearchBar({ value, onChange }: SearchBarProps) {
  const [draft, setDraft] = useState(value);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  return (
    <form
      className="flex gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        onChange(draft.trim());
      }}
    >
      <input
        type="search"
        placeholder="Search items"
        className="flex-1 rounded border border-zinc-300 px-3 py-2 text-sm focus:border-zinc-500 focus:outline-none"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      <button
        type="submit"
        className="rounded bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700"
      >
        Search
      </button>
      {value !== "" && (
        <button
          type="button"
          className="rounded border border-zinc-300 px-3 py-2 text-sm hover:bg-zinc-100"
          onClick={() => {
            setDraft("");
            onChange("");
          }}
        >
          Clear
        </button>
      )}
    </form>
  );
}
