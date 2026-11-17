import { useEffect, useState } from "react";

export type RouteName = "chat" | "plans" | "library" | "graph" | "settings" | "notfound";

export interface Route {
  name: RouteName;
  conversationId: string | null;
  query: Record<string, string>;
  hash: string;
}

function parseQuery(raw: string): Record<string, string> {
  const query: Record<string, string> = {};
  const search = raw.startsWith("?") ? raw.slice(1) : raw;
  if (!search) return query;
  for (const part of search.split("&")) {
    if (!part) continue;
    const eq = part.indexOf("=");
    const key = decodeURIComponent(eq === -1 ? part : part.slice(0, eq));
    const value = eq === -1 ? "" : decodeURIComponent(part.slice(eq + 1));
    if (key) query[key] = value;
  }
  return query;
}

export function parseHash(hash: string): Route {
  const clean = hash.replace(/^#/, "");
  const [pathPart = "", queryPart = ""] = clean.split("?");
  const segments = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  const query = parseQuery(queryPart);
  const head = segments[0] ?? "";

  if (head === "" || head === "chat") {
    return { name: "chat", conversationId: segments[1] ?? null, query, hash };
  }
  if (head === "plans") return { name: "plans", conversationId: null, query, hash };
  if (head === "library") return { name: "library", conversationId: null, query, hash };
  if (head === "graph") return { name: "graph", conversationId: null, query, hash };
  if (head === "settings") return { name: "settings", conversationId: null, query, hash };
  return { name: "notfound", conversationId: null, query, hash };
}

export function currentHash(): string {
  return window.location.hash || "#/chat";
}

export function buildHash(path: string, query?: Record<string, string | null | undefined>): string {
  const entries = Object.entries(query ?? {}).filter(
    (entry): entry is [string, string] => typeof entry[1] === "string" && entry[1] !== "",
  );
  const suffix = entries.length
    ? `?${entries.map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`).join("&")}`
    : "";
  return `#${path.startsWith("/") ? path : `/${path}`}${suffix}`;
}

export function navigate(
  path: string,
  options?: { query?: Record<string, string | null | undefined>; replace?: boolean },
): void {
  const target = buildHash(path, options?.query);
  if (options?.replace) {
    window.history.replaceState(null, "", target);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else {
    window.location.hash = target;
  }
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(currentHash()));

  useEffect(() => {
    if (!window.location.hash) {
      window.history.replaceState(null, "", "#/chat");
    }
    const onChange = (): void => setRoute(parseHash(currentHash()));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return route;
}
