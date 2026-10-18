// Client for the external-reference resolver (`/v1/refs`).
//
// Chat text is full of ticket keys, PR numbers, commit SHAs and branch names.
// The server resolves them against GitHub/Linear/Jira with credentials the
// browser never sees; this module fetches the routing config and batches
// per-reference lookups into `/v1/refs/resolve` calls so a transcript full of
// references costs a couple of round trips, not one per token.

import { authenticatedFetch } from "./identity";

export type RefKind = "ticket" | "github" | "commit" | "branch";

export interface RefRequest {
  kind: RefKind;
  ref: string;
  repo?: string;
}

// ── Wire types (snake_case from server) ─────────────────────────

interface RefsConfigWire {
  github: { repos: string[] } | null;
  linear: { workspace: string; teams: string[] } | null;
  jira: { base_url: string; projects: string[] } | null;
}

interface RefCardWire {
  kind: string;
  url: string;
  title: string;
  identifier: string | null;
  repo: string | null;
  state: string | null;
  state_kind: string | null;
  author: string | null;
  assignee: string | null;
  date: string | null;
}

interface RefResultWire {
  kind: RefKind;
  ref: string;
  repo: string | null;
  found: boolean;
  cards: RefCardWire[];
}

// ── App types (camelCase) ───────────────────────────────────────

export interface RefsConfig {
  github: { repos: string[] } | null;
  linear: { workspace: string; teams: string[] } | null;
  jira: { baseUrl: string; projects: string[] } | null;
}

export interface RefCard {
  kind: string;
  url: string;
  title: string;
  identifier: string | null;
  repo: string | null;
  state: string | null;
  stateKind: string | null;
  author: string | null;
  assignee: string | null;
  date: string | null;
}

export interface RefResult {
  found: boolean;
  cards: RefCard[];
}

/** Stable identity for one reference; the react-query key and batch key. */
export function refRequestKey(req: RefRequest): string {
  return `${req.kind}:${req.repo ?? ""}:${req.ref}`;
}

export async function fetchRefsConfig(): Promise<RefsConfig> {
  const res = await authenticatedFetch("/v1/refs/config");
  if (!res.ok) throw new Error(`refs config failed: ${res.status}`);
  const wire = (await res.json()) as RefsConfigWire;
  return {
    github: wire.github,
    linear: wire.linear,
    jira: wire.jira ? { baseUrl: wire.jira.base_url, projects: wire.jira.projects } : null,
  };
}

function toRefResult(wire: RefResultWire): RefResult {
  return {
    found: wire.found,
    cards: wire.cards.map((c) => ({
      kind: c.kind,
      url: c.url,
      title: c.title,
      identifier: c.identifier,
      repo: c.repo,
      state: c.state,
      stateKind: c.state_kind,
      author: c.author,
      assignee: c.assignee,
      date: c.date,
    })),
  };
}

// ── Batching ────────────────────────────────────────────────────
//
// Every chip resolves independently (each is its own react-query entry), but a
// freshly opened transcript mounts dozens at once. Collect requests for a few
// milliseconds and send them as one capped batch; react-query dedupes repeat
// keys above this layer, so the queue only ever sees distinct references.

const BATCH_WINDOW_MS = 25;
const BATCH_MAX = 50;

interface PendingRef {
  req: RefRequest;
  resolve: (result: RefResult) => void;
  reject: (err: unknown) => void;
}

const pendingRefs: PendingRef[] = [];
let flushTimer: ReturnType<typeof setTimeout> | null = null;

async function flushPending(): Promise<void> {
  flushTimer = null;
  const batch = pendingRefs.splice(0, BATCH_MAX);
  if (pendingRefs.length > 0) scheduleFlush();
  if (batch.length === 0) return;
  try {
    const res = await authenticatedFetch("/v1/refs/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        refs: batch.map(({ req }) => ({ kind: req.kind, ref: req.ref, repo: req.repo })),
      }),
    });
    if (!res.ok) throw new Error(`refs resolve failed: ${res.status}`);
    const body = (await res.json()) as { data: RefResultWire[] };
    const byKey = new Map<string, RefResult>();
    for (const result of body.data) {
      byKey.set(
        refRequestKey({ kind: result.kind, ref: result.ref, repo: result.repo ?? undefined }),
        toRefResult(result),
      );
    }
    for (const entry of batch) {
      const result = byKey.get(refRequestKey(entry.req));
      if (result) entry.resolve(result);
      else entry.reject(new Error("reference missing from resolve response"));
    }
  } catch (err) {
    for (const entry of batch) entry.reject(err);
  }
}

function scheduleFlush(): void {
  if (flushTimer === null) flushTimer = setTimeout(() => void flushPending(), BATCH_WINDOW_MS);
}

/** Resolve one reference through the shared batch. */
export function resolveRefCards(req: RefRequest): Promise<RefResult> {
  return new Promise((resolve, reject) => {
    pendingRefs.push({ req, resolve, reject });
    if (pendingRefs.length >= BATCH_MAX) {
      if (flushTimer !== null) clearTimeout(flushTimer);
      void flushPending();
      return;
    }
    scheduleFlush();
  });
}
