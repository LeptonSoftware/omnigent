// Detection grammar + rehype plugin for external references in chat text.
//
// Finds ticket keys (`VIN-1732`), PR/issue numbers (`#37`, `owner/repo#37`)
// and commit SHAs in plain text nodes and wraps each in a marked anchor that
// `ExternalRefChip` renders. The grammar is deliberately loose — the chip
// verifies against `/v1/refs` and anything unconfigured or unresolvable
// renders back as plain text — so a false candidate costs a cached lookup,
// never a wrong link.
//
// Runs after Streamdown's sanitize step (which would drop the marker
// attributes as unknown) and before harden, exactly like the workspace
// file-link marker in `streamdown-security.ts`.

export const EXTERNAL_REF_ATTR = "data-omnigent-ref";
export const EXTERNAL_REF_KIND_ATTR = "data-omnigent-ref-kind";
export const EXTERNAL_REF_REPO_ATTR = "data-omnigent-ref-repo";

// Named fragment, not a bare "#": harden only passes a fragment-only href
// through when it round-trips as a hash, and a bare "#" parses to an empty
// one. Same trick as `PARKED_FILE_HREF`.
export const PARKED_REF_HREF = "#omnigent-ref";

export type DetectedRefKind = "ticket" | "github" | "commit" | "branch";

export interface DetectedRef {
  start: number;
  end: number;
  kind: DetectedRefKind;
  ref: string;
  repo?: string;
}

// `owner/repo#123`. Matched before the bare-number pattern so the qualified
// form wins the overlap.
const REPO_ISSUE_RE = /(^|[^\w.-])([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)#(\d{1,7})\b/g;
// `VIN-1732`, `NETW-1481` — any tracker-shaped key; the chip drops prefixes
// no provider claims (so `SHA-256` in prose stays text).
const TICKET_RE = /\b([A-Z][A-Z0-9]{1,9}-\d{1,7})\b/g;
// Bare `#37`. The left guard keeps out fragments of URLs (`…/pull/1#r2`),
// HTML entities (`&#123;`) and words (`Chapter#3`).
const BARE_ISSUE_RE = /(^|[\s([{])#(\d{1,7})\b/g;
// Hex run that could be a commit SHA. Post-filtered to require both a digit
// and a hex letter, which keeps out decimal numbers and all-letter words
// ("deadbeef" is a plausible SHA, but far more often prose).
const SHA_RE = /\b([0-9a-f]{8,40})\b/g;
const SHA_HAS_DIGIT_RE = /\d/;
const SHA_HAS_LETTER_RE = /[a-f]/;

/**
 * A backticked span that could be a branch name: `owner-ish/segments` with no
 * file extension on the last segment (paths dominate backticked slashed
 * tokens, and paths usually end in `.ext`; branches almost never do).
 */
const BRANCH_CANDIDATE_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9._/-]+$/;

export function isBranchCandidate(text: string): boolean {
  if (text.length > 200 || !BRANCH_CANDIDATE_RE.test(text)) return false;
  const segments = text.split("/");
  const last = segments[segments.length - 1];
  return last.length > 0 && !last.includes(".");
}

interface Candidate extends DetectedRef {
  priority: number;
}

function collect(
  out: Candidate[],
  text: string,
  re: RegExp,
  priority: number,
  build: (m: RegExpExecArray) => Omit<DetectedRef, "start" | "end"> & { offset?: number },
): void {
  re.lastIndex = 0;
  for (let m = re.exec(text); m !== null; m = re.exec(text)) {
    const { offset = 0, ...ref } = build(m);
    out.push({ ...ref, start: m.index + offset, end: m.index + m[0].length, priority });
  }
}

/**
 * Find every external-reference candidate in a plain-text run.
 * Overlapping matches resolve by priority (repo-qualified beats bare),
 * then by position.
 */
export function findExternalRefs(text: string): DetectedRef[] {
  // Cheap pre-filter: nothing to find without one of these anchors.
  if (!text.includes("#") && !/[A-Z]-\d|[0-9a-f]{8}/.test(text)) return [];

  const candidates: Candidate[] = [];
  collect(candidates, text, REPO_ISSUE_RE, 0, (m) => ({
    kind: "github",
    ref: m[3],
    repo: m[2],
    offset: m[1].length,
  }));
  collect(candidates, text, TICKET_RE, 1, (m) => ({ kind: "ticket", ref: m[1] }));
  collect(candidates, text, BARE_ISSUE_RE, 2, (m) => ({
    kind: "github",
    ref: m[2],
    offset: m[1].length,
  }));
  collect(candidates, text, SHA_RE, 3, (m) =>
    SHA_HAS_DIGIT_RE.test(m[1]) && SHA_HAS_LETTER_RE.test(m[1])
      ? { kind: "commit", ref: m[1] }
      : { kind: "commit", ref: "" },
  );

  const picked: DetectedRef[] = [];
  const taken: Array<[number, number]> = [];
  candidates.sort((a, b) => a.priority - b.priority || a.start - b.start);
  for (const c of candidates) {
    if (!c.ref) continue;
    if (taken.some(([s, e]) => c.start < e && c.end > s)) continue;
    taken.push([c.start, c.end]);
    picked.push({ start: c.start, end: c.end, kind: c.kind, ref: c.ref, repo: c.repo });
  }
  return picked.sort((a, b) => a.start - b.start);
}

/**
 * Parse an already-linkified URL (GitHub / Linear / Jira) back into a
 * reference, so full URLs in chat get the same hover card as bare tokens.
 */
export function parseRefFromUrl(
  href: string,
): { kind: DetectedRefKind; ref: string; repo?: string } | null {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (url.protocol !== "https:") return null;
  const parts = url.pathname.split("/").filter(Boolean);
  if (url.hostname === "github.com" && parts.length >= 4) {
    const repo = `${parts[0]}/${parts[1]}`;
    if ((parts[2] === "pull" || parts[2] === "issues") && /^\d{1,7}$/.test(parts[3])) {
      return { kind: "github", ref: parts[3], repo };
    }
    if (parts[2] === "commit" && /^[0-9a-f]{7,40}$/.test(parts[3])) {
      return { kind: "commit", ref: parts[3], repo };
    }
    if (parts[2] === "tree" && parts.length >= 4) {
      return { kind: "branch", ref: parts.slice(3).join("/"), repo };
    }
  }
  if (url.hostname === "linear.app" && parts[1] === "issue" && parts.length >= 3) {
    const key = parts[2];
    if (/^[A-Z][A-Z0-9]{1,9}-\d{1,7}$/.test(key)) return { kind: "ticket", ref: key };
  }
  if (
    parts[0] === "browse" &&
    parts.length === 2 &&
    /^[A-Z][A-Z0-9]{1,9}-\d{1,7}$/.test(parts[1])
  ) {
    return { kind: "ticket", ref: parts[1] };
  }
  return null;
}

// Minimal hast shapes; matches what `streamdown-security.ts` walks.
interface HastNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
  value?: string;
}

// Subtrees whose text must stay literal: existing links, code, and the
// KaTeX/mermaid containers, which own their own text layout.
const SKIP_TAGS = new Set(["a", "code", "pre", "script", "style"]);

function refToAnchor(ref: DetectedRef, text: string): HastNode {
  const properties: Record<string, unknown> = {
    href: PARKED_REF_HREF,
    [EXTERNAL_REF_ATTR]: ref.ref,
    [EXTERNAL_REF_KIND_ATTR]: ref.kind,
  };
  if (ref.repo) properties[EXTERNAL_REF_REPO_ATTR] = ref.repo;
  return {
    type: "element",
    tagName: "a",
    properties,
    children: [{ type: "text", value: text.slice(ref.start, ref.end) }],
  };
}

function splitTextNode(node: HastNode): HastNode[] | null {
  const text = node.value ?? "";
  const refs = findExternalRefs(text);
  if (refs.length === 0) return null;
  const out: HastNode[] = [];
  let cursor = 0;
  for (const ref of refs) {
    if (ref.start > cursor) out.push({ type: "text", value: text.slice(cursor, ref.start) });
    out.push(refToAnchor(ref, text));
    cursor = ref.end;
  }
  if (cursor < text.length) out.push({ type: "text", value: text.slice(cursor) });
  return out;
}

function walk(node: HastNode): void {
  const children = node.children;
  if (!children) return;
  for (let i = children.length - 1; i >= 0; i -= 1) {
    const child = children[i];
    if (child.type === "element") {
      if (!SKIP_TAGS.has(child.tagName ?? "")) walk(child);
      continue;
    }
    if (child.type !== "text") continue;
    const replacement = splitTextNode(child);
    if (replacement) children.splice(i, 1, ...replacement);
  }
}

/**
 * Rehype plugin: wrap external-reference candidates in marked anchors for
 * `ExternalRefChip`. Must run after sanitize (the marker attributes survive)
 * and before harden (the parked fragment href passes through).
 */
export function linkifyExternalRefs() {
  return (tree: HastNode) => {
    walk(tree);
  };
}
