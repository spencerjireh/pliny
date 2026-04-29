import { useMemo } from "react";

import { makeClient } from "./api/client";
import { clearApiKey } from "./auth";
import { AuthGate } from "./components/AuthGate";
import { FilterSidebar } from "./components/FilterSidebar";
import { ResultsList } from "./components/ResultsList";
import { SearchBar } from "./components/SearchBar";
import { readFilters, useUrlState, writeFilters } from "./state";

export default function App() {
  return <AuthGate>{(apiKey) => <Workspace apiKey={apiKey} />}</AuthGate>;
}

function Workspace({ apiKey }: { apiKey: string }) {
  const [params, setParams] = useUrlState();
  const filters = useMemo(() => readFilters(params), [params]);
  const client = useMemo(() => makeClient(apiKey), [apiKey]);

  const apply = (next: typeof filters) => {
    setParams(writeFilters(next));
  };

  const handleLogout = () => {
    clearApiKey();
    window.location.reload();
  };

  return (
    <div className="min-h-screen bg-zinc-50 text-zinc-900">
      <header className="border-b border-zinc-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center gap-4 px-4 py-3">
          <h1 className="text-lg font-semibold">Pliny</h1>
          <div className="flex-1">
            <SearchBar value={filters.q} onChange={(q) => apply({ ...filters, q })} />
          </div>
          <button
            type="button"
            onClick={handleLogout}
            className="text-xs text-zinc-500 hover:text-zinc-800"
          >
            Sign out
          </button>
        </div>
      </header>
      <div className="mx-auto flex max-w-6xl gap-4 px-4 py-4">
        <FilterSidebar filters={filters} onChange={apply} />
        <main className="min-w-0 flex-1">
          <ResultsList client={client} filters={filters} />
        </main>
      </div>
    </div>
  );
}
