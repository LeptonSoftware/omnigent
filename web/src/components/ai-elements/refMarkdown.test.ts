// The detection grammar is deliberately loose (the chip verifies against the
// server before anything links), so these tests pin two things: what the
// grammar offers as candidates, and that the rehype plugin never touches text
// inside links or code.

import { describe, expect, it } from "vitest";
import {
  EXTERNAL_REF_ATTR,
  EXTERNAL_REF_KIND_ATTR,
  EXTERNAL_REF_REPO_ATTR,
  findExternalRefs,
  isBranchCandidate,
  linkifyExternalRefs,
  parseRefFromUrl,
} from "./refMarkdown";

function refsIn(text: string) {
  return findExternalRefs(text).map((r) => ({ kind: r.kind, ref: r.ref, repo: r.repo }));
}

describe("findExternalRefs — the candidate grammar", () => {
  it("finds tickets and bare issue numbers side by side", () => {
    expect(refsIn("Merged PR #37 for VIN-1732 yesterday")).toEqual([
      { kind: "github", ref: "37", repo: undefined },
      { kind: "ticket", ref: "VIN-1732", repo: undefined },
    ]);
  });

  it("prefers the repo-qualified form over a bare number", () => {
    expect(refsIn("see LeptonSoftware/omnigent#36 for details")).toEqual([
      { kind: "github", ref: "36", repo: "LeptonSoftware/omnigent" },
    ]);
  });

  it("matches a bare number at the start of the text", () => {
    expect(refsIn("#37 is green")).toEqual([{ kind: "github", ref: "37", repo: undefined }]);
  });

  it("offers tracker-shaped words as ticket candidates (server prunes them)", () => {
    // SHA-256 is a candidate by design: the chip renders it back to plain
    // text because no provider claims the SHA prefix.
    expect(refsIn("hash it with SHA-256")).toEqual([
      { kind: "ticket", ref: "SHA-256", repo: undefined },
    ]);
  });

  it("finds commit SHAs but never all-digit or all-letter runs", () => {
    expect(refsIn("deployed 6981f917f to prod")).toEqual([
      { kind: "commit", ref: "6981f917f", repo: undefined },
    ]);
    expect(refsIn("image 383070025 and word deadbeef")).toEqual([]);
  });

  it("ignores numbers glued to words, entities and URL fragments", () => {
    expect(refsIn("Chapter#3 and &#123; and /pull/1#r2")).toEqual([]);
  });

  it("returns nothing for plain prose", () => {
    expect(refsIn("just a normal sentence with no references")).toEqual([]);
  });
});

describe("parseRefFromUrl — enrichment of already-linkified URLs", () => {
  it("parses GitHub PR, issue, commit and branch URLs", () => {
    expect(parseRefFromUrl("https://github.com/acme/alpha/pull/37")).toEqual({
      kind: "github",
      ref: "37",
      repo: "acme/alpha",
    });
    expect(parseRefFromUrl("https://github.com/acme/alpha/issues/9")).toEqual({
      kind: "github",
      ref: "9",
      repo: "acme/alpha",
    });
    expect(parseRefFromUrl("https://github.com/acme/alpha/commit/6981f917f")).toEqual({
      kind: "commit",
      ref: "6981f917f",
      repo: "acme/alpha",
    });
    expect(parseRefFromUrl("https://github.com/acme/alpha/tree/user/topic")).toEqual({
      kind: "branch",
      ref: "user/topic",
      repo: "acme/alpha",
    });
  });

  it("parses Linear issue URLs and Jira browse URLs", () => {
    expect(parseRefFromUrl("https://linear.app/acme/issue/VIN-1732/fix-it")).toEqual({
      kind: "ticket",
      ref: "VIN-1732",
    });
    expect(parseRefFromUrl("https://acme.atlassian.net/browse/NETW-1481")).toEqual({
      kind: "ticket",
      ref: "NETW-1481",
    });
  });

  it("rejects non-https and unrelated URLs", () => {
    expect(parseRefFromUrl("http://github.com/acme/alpha/pull/37")).toBeNull();
    expect(parseRefFromUrl("https://github.com/acme")).toBeNull();
    expect(parseRefFromUrl("https://example.com/some/page")).toBeNull();
    expect(parseRefFromUrl("not a url")).toBeNull();
  });
});

describe("isBranchCandidate — backticked branch shapes", () => {
  it("accepts branch-shaped tokens and rejects file-shaped ones", () => {
    expect(isBranchCandidate("nsaraf98/web-rich-refs")).toBe(true);
    expect(isBranchCandidate("sync/upstream-2026-08-24")).toBe(true);
    // Directory-shaped tokens stay candidates; the server verdict is what
    // keeps them plain when no such branch exists.
    expect(isBranchCandidate("services/kernel")).toBe(true);
    expect(isBranchCandidate("web/src/App.tsx")).toBe(false);
    expect(isBranchCandidate("no-slash-here")).toBe(false);
    expect(isBranchCandidate("has space/inside")).toBe(false);
  });
});

// ── rehype plugin ───────────────────────────────────────────────

interface TestNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: TestNode[];
  value?: string;
}

function paragraph(children: TestNode[]): TestNode {
  return {
    type: "root",
    children: [{ type: "element", tagName: "p", properties: {}, children }],
  };
}

function run(tree: TestNode): TestNode {
  linkifyExternalRefs()(tree as never);
  return tree;
}

describe("linkifyExternalRefs — the rehype plugin", () => {
  it("wraps candidates in marked anchors and keeps surrounding text", () => {
    const tree = run(paragraph([{ type: "text", value: "Fixed VIN-9 in acme/alpha#5." }]));
    const children = tree.children![0].children!;
    expect(children.map((c) => c.type)).toEqual(["text", "element", "text", "element", "text"]);
    const [, ticket, , issue] = children;
    expect(ticket.properties).toMatchObject({
      [EXTERNAL_REF_ATTR]: "VIN-9",
      [EXTERNAL_REF_KIND_ATTR]: "ticket",
    });
    expect(ticket.children).toEqual([{ type: "text", value: "VIN-9" }]);
    expect(issue.properties).toMatchObject({
      [EXTERNAL_REF_ATTR]: "5",
      [EXTERNAL_REF_KIND_ATTR]: "github",
      [EXTERNAL_REF_REPO_ATTR]: "acme/alpha",
    });
    expect(children[4]).toEqual({ type: "text", value: "." });
  });

  it("leaves text inside links, inline code and fences alone", () => {
    const tree = run(
      paragraph([
        {
          type: "element",
          tagName: "a",
          properties: { href: "https://example.com" },
          children: [{ type: "text", value: "VIN-9" }],
        },
        {
          type: "element",
          tagName: "code",
          properties: {},
          children: [{ type: "text", value: "#37" }],
        },
        {
          type: "element",
          tagName: "pre",
          properties: {},
          children: [{ type: "text", value: "VIN-1 #2 acme/a#3" }],
        },
      ]),
    );
    const children = tree.children![0].children!;
    expect(children[0].children).toEqual([{ type: "text", value: "VIN-9" }]);
    expect(children[1].children).toEqual([{ type: "text", value: "#37" }]);
    expect(children[2].children).toEqual([{ type: "text", value: "VIN-1 #2 acme/a#3" }]);
  });

  it("does nothing to reference-free trees", () => {
    const tree = paragraph([{ type: "text", value: "nothing to see" }]);
    const before = JSON.stringify(tree);
    run(tree);
    expect(JSON.stringify(tree)).toBe(before);
  });
});
