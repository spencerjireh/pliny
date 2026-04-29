import type { SearchFilters } from "../state";

const ITEM_TYPES = ["text", "url", "image", "pdf", "audio", "video", "file"];

interface FilterSidebarProps {
  filters: SearchFilters;
  onChange: (next: SearchFilters) => void;
}

export function FilterSidebar({ filters, onChange }: FilterSidebarProps) {
  const toggleType = (t: string) => {
    const next = filters.type.includes(t)
      ? filters.type.filter((x) => x !== t)
      : [...filters.type, t];
    onChange({ ...filters, type: next });
  };

  return (
    <aside className="w-64 shrink-0 space-y-6 border-r border-zinc-200 bg-white p-4 text-sm">
      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Type</h2>
        <ul className="space-y-1">
          {ITEM_TYPES.map((t) => (
            <li key={t}>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={filters.type.includes(t)}
                  onChange={() => toggleType(t)}
                />
                <span>{t}</span>
              </label>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Date</h2>
        <label className="block">
          <span className="text-xs text-zinc-600">From</span>
          <input
            type="date"
            className="mt-1 w-full rounded border border-zinc-300 px-2 py-1"
            value={filters.from}
            onChange={(e) => onChange({ ...filters, from: e.target.value })}
          />
        </label>
        <label className="mt-2 block">
          <span className="text-xs text-zinc-600">To</span>
          <input
            type="date"
            className="mt-1 w-full rounded border border-zinc-300 px-2 py-1"
            value={filters.to}
            onChange={(e) => onChange({ ...filters, to: e.target.value })}
          />
        </label>
      </section>

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Tags</h2>
        <CsvList
          values={filters.tag}
          placeholder="comma-separated"
          onChange={(values) => onChange({ ...filters, tag: values })}
        />
      </section>

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Entities
        </h2>
        <CsvList
          values={filters.entity}
          placeholder="UUIDs, comma-separated"
          onChange={(values) => onChange({ ...filters, entity: values })}
        />
      </section>

      <section>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={filters.possibleDuplicate}
            onChange={(e) => onChange({ ...filters, possibleDuplicate: e.target.checked })}
          />
          <span>Possible duplicates only</span>
        </label>
      </section>
    </aside>
  );
}

function CsvList({
  values,
  placeholder,
  onChange,
}: {
  values: string[];
  placeholder: string;
  onChange: (next: string[]) => void;
}) {
  return (
    <input
      type="text"
      placeholder={placeholder}
      className="w-full rounded border border-zinc-300 px-2 py-1"
      defaultValue={values.join(",")}
      onBlur={(e) => {
        const parts = e.target.value
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
        onChange(parts);
      }}
    />
  );
}
