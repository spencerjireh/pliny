import { useCallback, useEffect, useState } from "react";

export function useUrlState(): [URLSearchParams, (next: URLSearchParams) => void] {
  const [params, setParams] = useState<URLSearchParams>(
    () => new URLSearchParams(window.location.search),
  );

  useEffect(() => {
    const onPop = () => setParams(new URLSearchParams(window.location.search));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const update = useCallback((next: URLSearchParams) => {
    const url = `${window.location.pathname}?${next.toString()}`;
    window.history.pushState(null, "", url);
    setParams(new URLSearchParams(next));
  }, []);

  return [params, update];
}

export interface SearchFilters {
  q: string;
  type: string[];
  from: string;
  to: string;
  tag: string[];
  entity: string[];
  possibleDuplicate: boolean;
}

export function readFilters(params: URLSearchParams): SearchFilters {
  return {
    q: params.get("q") ?? "",
    type: params.getAll("type"),
    from: params.get("from") ?? "",
    to: params.get("to") ?? "",
    tag: params.getAll("tag"),
    entity: params.getAll("entity"),
    possibleDuplicate: params.get("possible_duplicate") === "true",
  };
}

export function writeFilters(filters: SearchFilters): URLSearchParams {
  const out = new URLSearchParams();
  if (filters.q) out.set("q", filters.q);
  for (const t of filters.type) out.append("type", t);
  if (filters.from) out.set("from", filters.from);
  if (filters.to) out.set("to", filters.to);
  for (const t of filters.tag) out.append("tag", t);
  for (const e of filters.entity) out.append("entity", e);
  if (filters.possibleDuplicate) out.set("possible_duplicate", "true");
  return out;
}
