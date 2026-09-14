// Inline chip for an external reference in chat text (ticket / PR / commit /
// branch): a link with a hover card showing the referent's live state.
//
// Linkification policy is precision-tiered:
//   - High-precision refs (configured ticket prefixes, `owner/repo#n`, full
//     URLs) link immediately — their href is deterministic — and fetch the
//     card only when hovered.
//   - Low-precision refs (bare `#n`, SHAs, branch-shaped inline code) resolve
//     eagerly and render as plain text unless the server confirms they exist,
//     so a stray number or path never becomes a wrong link.

import { useState, type ReactNode } from "react";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { useRefCards, useRefsConfig } from "@/hooks/useExternalRefs";
import type { RefCard, RefKind, RefRequest, RefsConfig } from "@/lib/refsApi";
import { cn } from "@/lib/utils";

const STATE_BADGE_CLASS: Record<string, string> = {
  todo: "bg-muted text-muted-foreground",
  draft: "bg-muted text-muted-foreground",
  active: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  open: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  done: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  merged: "bg-violet-500/15 text-violet-600 dark:text-violet-400",
  closed: "bg-red-500/15 text-red-600 dark:text-red-400",
};

/** How this chip's reference links and loads, given the server config. */
function resolvePolicy(
  config: RefsConfig | null,
  kind: RefKind,
  refText: string,
  repo: string | undefined,
): { configured: boolean; immediateHref: string | null; eager: boolean } {
  if (!config) return { configured: false, immediateHref: null, eager: false };
  if (kind === "ticket") {
    const prefix = refText.split("-", 1)[0];
    if (config.linear?.teams.includes(prefix)) {
      return {
        configured: true,
        immediateHref: `https://linear.app/${config.linear.workspace}/issue/${refText}`,
        eager: false,
      };
    }
    if (config.jira?.projects.includes(prefix)) {
      return {
        configured: true,
        immediateHref: `${config.jira.baseUrl}/browse/${refText}`,
        eager: false,
      };
    }
    return { configured: false, immediateHref: null, eager: false };
  }
  if (!config.github) return { configured: false, immediateHref: null, eager: false };
  if (kind === "github" && repo) {
    // `/issues/n` redirects to `/pull/n` when the number is a PR.
    return {
      configured: true,
      immediateHref: `https://github.com/${repo}/issues/${refText}`,
      eager: false,
    };
  }
  // Bare `#n`, commits and branches need candidate repos to resolve against.
  const configured = repo !== undefined || config.github.repos.length > 0;
  return { configured, immediateHref: null, eager: configured };
}

function shortDate(iso: string | null): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

function RefCardBody({ card }: { card: RefCard }) {
  const who = card.assignee ?? card.author;
  const date = shortDate(card.date);
  return (
    <a
      href={card.url}
      target="_blank"
      rel="noreferrer"
      className="block rounded-md p-1.5 -m-1.5 hover:bg-muted/60"
    >
      <div className="flex items-center gap-1.5 text-xs">
        {card.state && (
          <span
            className={cn(
              "rounded px-1.5 py-0.5 font-medium",
              STATE_BADGE_CLASS[card.stateKind ?? ""] ?? "bg-muted text-muted-foreground",
            )}
          >
            {card.state}
          </span>
        )}
        {card.identifier && (
          <span className="font-mono text-muted-foreground">{card.identifier}</span>
        )}
        {card.repo && <span className="truncate text-muted-foreground">{card.repo}</span>}
      </div>
      {card.title && <div className="mt-1 line-clamp-2 font-medium">{card.title}</div>}
      {(who || date) && (
        <div className="mt-1 text-xs text-muted-foreground">
          {who}
          {who && date ? " · " : ""}
          {date}
        </div>
      )}
    </a>
  );
}

export interface ExternalRefChipProps {
  kind: RefKind;
  refText: string;
  repo?: string;
  /** Pre-existing href (full-URL enrichment); kept even when unconfigured. */
  href?: string;
  className?: string;
  /** Rendered when the ref can't or shouldn't linkify; defaults to children. */
  fallback?: ReactNode;
  children: ReactNode;
}

/**
 * Renders an external reference as a link with a hover card, degrading to
 * `fallback` (plain text) when the feature is unconfigured or the reference
 * doesn't verify. Safe inside memoized markdown: all state is internal.
 */
export function ExternalRefChip({
  kind,
  refText,
  repo,
  href,
  className,
  fallback,
  children,
}: ExternalRefChipProps) {
  const config = useRefsConfig();
  const [hovered, setHovered] = useState(false);
  const { configured, immediateHref, eager } = resolvePolicy(config, kind, refText, repo);
  const request: RefRequest | null = configured ? { kind, ref: refText, repo } : null;
  const { result, isLoading, isError } = useRefCards(request, eager || hovered);

  const plain = fallback ?? children;
  if (!configured) {
    // A pre-linkified URL stays a link even when the server can't enrich it.
    return href ? (
      <a href={href} target="_blank" rel="noreferrer" className={className} data-streamdown="link">
        {children}
      </a>
    ) : (
      plain
    );
  }

  // Verify-first kinds stay plain text until the reference is confirmed real.
  const verifyFirst = href === undefined && immediateHref === null;
  if (verifyFirst && !result?.found) return plain;

  const cards = result?.cards ?? [];
  const linkHref = href ?? cards[0]?.url ?? immediateHref ?? "";
  if (!linkHref) return plain;

  return (
    <HoverCard openDelay={150} closeDelay={100} onOpenChange={(open) => open && setHovered(true)}>
      <HoverCardTrigger asChild>
        <a
          href={linkHref}
          target="_blank"
          rel="noreferrer"
          className={className}
          data-streamdown="link"
        >
          {children}
        </a>
      </HoverCardTrigger>
      <HoverCardContent align="start" className="w-80 space-y-2">
        {cards.length > 0 ? (
          cards.map((card) => <RefCardBody key={card.url} card={card} />)
        ) : (
          <div className="text-xs text-muted-foreground">
            {isLoading ? "Loading…" : isError ? "Couldn't load details" : "No details available"}
          </div>
        )}
      </HoverCardContent>
    </HoverCard>
  );
}
