// Data hooks for external-reference chips (tickets / PRs / commits / branches
// in chat text). Config loads once per app; cards resolve through the batching
// layer in `refsApi`, one react-query entry per distinct reference, so every
// mention of the same ticket across the transcript shares one lookup.

import { useQuery } from "@tanstack/react-query";
import {
  fetchRefsConfig,
  refRequestKey,
  resolveRefCards,
  type RefRequest,
  type RefResult,
  type RefsConfig,
} from "@/lib/refsApi";

/**
 * The server's reference-routing config, or `null` while loading / when the
 * server has no providers configured (feature off → chips render plain text).
 */
export function useRefsConfig(): RefsConfig | null {
  const { data } = useQuery({
    queryKey: ["refs-config"],
    queryFn: fetchRefsConfig,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    retry: 1,
  });
  return data ?? null;
}

/**
 * Resolve one reference to its display cards. Pass `enabled: false` to defer
 * (hover-gated kinds); the query stays dormant until enabled flips.
 */
export function useRefCards(
  req: RefRequest | null,
  enabled: boolean,
): { result: RefResult | undefined; isLoading: boolean; isError: boolean } {
  const query = useQuery({
    queryKey: ["ref-card", req ? refRequestKey(req) : "none"],
    // Non-null by `enabled`; react-query only calls queryFn when enabled.
    queryFn: () => resolveRefCards(req as RefRequest),
    enabled: req !== null && enabled,
    staleTime: 120_000,
    retry: 1,
  });
  return { result: query.data, isLoading: query.isLoading, isError: query.isError };
}
