#!/usr/bin/env node
/**
 * markdown-helper MCP server (v2).
 *
 * Editor-style tool surface for working with markdown files
 * section-by-section. Open existing files, create new ones, edit
 * sections, save back to disk. Insert/delete/move sections in the
 * tree without renumbering.
 *
 * v2 highlights vs v1:
 *   - Custom state schemas (default [pending, done]; agents declare).
 *   - Non-destructive open (reconcile, divergence reporting, reset:true).
 *   - Auto-split: set/insert with mode:'subtree' parses headings.
 *   - markdown_patch: search-replace edits.
 *   - markdown_discover: find docs without opening.
 *   - Explicit source: workspace|disk on every read tool.
 *   - touched.json registry: source:'disk' marks files as seen.
 *   - Standard changeReport + userSummary on every mutation.
 *   - Lever A: every result wraps with {title, metadata, output} so
 *     opencode (and any client honoring AI SDK tool-result shape)
 *     renders nicely without a fork.
 */

import { spawn } from "child_process";
import { existsSync } from "fs";
import { join, resolve } from "path";
import { homedir } from "os";

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  Tool,
} from "@modelcontextprotocol/sdk/types.js";

// ---------------------------------------------------------------------------
// CLI invocation
// ---------------------------------------------------------------------------

function locateCore(): string {
  const installed = join(homedir(), ".markdown-helper", "core", "md_helper_core.py");
  if (existsSync(installed)) return installed;
  const source = resolve(new URL("../../core/md_helper_core.py", import.meta.url).pathname);
  if (existsSync(source)) return source;
  throw new Error("Could not locate md_helper_core.py");
}

interface ExecResult { code: number; stdout: string; stderr: string; }

function execCore(args: string[], stdin?: string): Promise<ExecResult> {
  return new Promise((resolveResult) => {
    let proc;
    try {
      proc = spawn(
        "python3",
        [locateCore(), ...args],
        {
          stdio: [stdin !== undefined ? "pipe" : "ignore", "pipe", "pipe"],
        },
      );
    } catch (e: any) {
      resolveResult({
        code: -1,
        stdout: "",
        stderr: JSON.stringify({
          error: `failed to spawn python3: ${e.code || e.name}: ${e.message}`,
          hint: e.code === "E2BIG"
            ? "Tool argument list exceeded the OS limit. The MCP server should have piped this via stdin -- this is a bug. File an issue."
            : undefined,
        }),
      });
      return;
    }
    let stdout = "";
    let stderr = "";
    proc.stdout?.on("data", (chunk) => { stdout += chunk.toString(); });
    proc.stderr?.on("data", (chunk) => { stderr += chunk.toString(); });
    proc.on("error", (e: any) => {
      resolveResult({
        code: -1,
        stdout: "",
        stderr: JSON.stringify({ error: `python3 process error: ${e.code || e.name}: ${e.message}` }),
      });
    });
    proc.on("close", (code) => {
      resolveResult({ code: code ?? -1, stdout, stderr });
    });
    if (stdin !== undefined && proc.stdin) {
      proc.stdin.end(stdin);
    }
  });
}

// ---------------------------------------------------------------------------
// Lever A: wrap result for opencode-style rendering
// ---------------------------------------------------------------------------
//
// opencode reads `output.title`, `output.metadata`, `output.output` from
// every tool-result regardless of whether it came from a native tool or
// MCP. Adding these top-level fields to our return shape lets opencode
// render our results in a panel automatically -- no opencode fork needed.
//
// `content` stays the agent-facing JSON (the model reads this).
// `title` is a one-line condensation for the panel header.
// `output` is the userSummary the Python core produced (multi-line,
//   human-readable). Falls back to the title when no userSummary exists.
// `metadata` carries structured fields (changeReport, outline, source,
//   readOnly, etc) for any client that wants to render its own panel.

const TOOLS_WITH_REMINDER = new Set<string>([
  "markdown_create",
  "markdown_open",
  "markdown_outline",
  "markdown_save",
  "markdown_review",
  "markdown_dispatch",
  "markdown_list",
  "markdown_insert",
  "markdown_delete",
  "markdown_move",
  "markdown_read",
  "markdown_set",
  "markdown_patch",
  "markdown_discover",
]);

const REMINDER_TEXT =
  "---\n" +
  "> markdown-helper:\n" +
  ">   - Paste userSummary verbatim. needsSave -> save at pause. divergence -> resolve first.\n" +
  ">   - New decision/fact/anti-pattern/requirement -> capture before next tool call.\n" +
  ">   - Deliverable docs: see \"Complex refactor requirement card\" in markdown_guide.";

interface WrappedResult {
  content: { type: "text"; text: string }[];
  isError?: boolean;
  // Lever A fields:
  title?: string;
  metadata?: any;
  output?: string;
}

function toolText(text: string, isError = false): WrappedResult {
  return {
    content: [{ type: "text" as const, text }],
    isError,
  };
}

function withReminder(toolName: string, payload: WrappedResult): WrappedResult {
  if (!TOOLS_WITH_REMINDER.has(toolName) || payload.isError) return payload;
  return {
    ...payload,
    content: [
      ...payload.content,
      { type: "text" as const, text: REMINDER_TEXT },
    ],
  };
}

/**
 * Try to extract Lever A fields from the Python core's JSON response.
 * Best-effort: any failures fall back to a basic content-only result.
 */
function liftLeverAFields(toolName: string, jsonText: string): {
  title?: string; metadata?: any; output?: string;
} {
  let parsed: any;
  try {
    parsed = JSON.parse(jsonText);
  } catch {
    return {};
  }
  if (parsed === null || typeof parsed !== "object") return {};

  // Build a metadata object with the fields agent UIs care about.
  const metadata: Record<string, any> = {};
  for (const key of [
    "changeReport", "outline", "source", "readOnly", "filename",
    "sectionCount", "stateCounts", "schema", "needsSave", "divergence",
    "nextFocus", "sections", "writeCount", "scope",
  ]) {
    if (parsed[key] !== undefined) metadata[key] = parsed[key];
  }

  // userSummary -> output for panel body.
  let output: string | undefined;
  if (typeof parsed.userSummary === "string") {
    output = parsed.userSummary;
  }

  // title: try to build a compact one-liner from changeReport, falling
  // back to filename + tool name.
  let title: string | undefined;
  const filename = typeof parsed.filename === "string"
    ? parsed.filename
    : undefined;
  if (parsed.changeReport && typeof parsed.changeReport === "object") {
    const cr = parsed.changeReport;
    const parts: string[] = [];
    if (Array.isArray(cr.inserted) && cr.inserted.length > 0) parts.push(`+${cr.inserted.length}`);
    if (Array.isArray(cr.deleted) && cr.deleted.length > 0) parts.push(`-${cr.deleted.length}`);
    if (Array.isArray(cr.shifted) && cr.shifted.length > 0) parts.push(`~${cr.shifted.length}`);
    else if (typeof cr.shiftedCount === "number" && cr.shiftedCount > 0) parts.push(`~${cr.shiftedCount}`);
    if (Array.isArray(cr.modified) && cr.modified.length > 0) parts.push(`*${cr.modified.length}`);
    const summary = parts.join(" ");
    if (filename && summary) title = `${filename}  ${summary}`;
    else if (filename) title = filename;
  } else if (filename) {
    // No changeReport (read tool): use filename + count if available.
    if (typeof parsed.sectionCount === "number") {
      title = `${filename}  ${parsed.sectionCount} section${parsed.sectionCount === 1 ? "" : "s"}`;
    } else {
      title = filename;
    }
  }

  // For tools without filename (discover/list-no-filename), derive
  // a useful title from the response shape.
  if (!title) {
    if (toolName === "markdown_discover" && typeof parsed.matchedCount === "number") {
      title = `discover: ${parsed.matchedCount} matched`;
    } else if (toolName === "markdown_list" && Array.isArray(parsed.files)) {
      const files = parsed.files.length;
      const touched = Array.isArray(parsed.touched) ? parsed.touched.length : 0;
      title = `list: ${files} open${touched ? `, ${touched} touched` : ""}`;
    } else if (toolName === "markdown_guide") {
      title = "markdown-helper guide";
    } else {
      title = toolName;
    }
  }

  // If no userSummary, fall back to the title.
  if (output === undefined) output = title;

  return { title, metadata, output };
}

function wrapResult(toolName: string, result: ExecResult): WrappedResult {
  if (result.code !== 0) {
    const text = result.stderr.trim() || result.stdout.trim() || `md_helper exited ${result.code}`;
    // Even errors get a title for the panel header.
    let title: string | undefined;
    let metadata: any = undefined;
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object") {
        if (typeof parsed.error === "string") title = `error: ${parsed.error.slice(0, 80)}`;
        metadata = parsed;
      }
    } catch { /* ignore */ }
    return {
      content: [{ type: "text" as const, text }],
      isError: true,
      title: title ?? `${toolName}: error`,
      metadata,
      output: title ?? text.slice(0, 200),
    };
  }

  const lifted = liftLeverAFields(toolName, result.stdout);
  return withReminder(toolName, {
    content: [{ type: "text" as const, text: result.stdout }],
    title: lifted.title,
    metadata: lifted.metadata,
    output: lifted.output,
  });
}

// ---------------------------------------------------------------------------
// Dispatcher
// ---------------------------------------------------------------------------

async function dispatch(toolName: string, args: Record<string, any>): Promise<WrappedResult> {
  const cliArgs: string[] = [];
  let stdinPayload: string | undefined;

  switch (toolName) {
    case "markdown_guide":
      cliArgs.push("guide");
      break;

    case "markdown_create": {
      cliArgs.push("create");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      cliArgs.push("--filename", String(args.filename));
      if (Array.isArray(args.sections)) {
        if (args.sections.length > 0 && typeof args.sections[0] === "object") {
          stdinPayload = JSON.stringify(args.sections);
          cliArgs.push("--outline-json-stdin");
        } else {
          for (const s of args.sections) cliArgs.push("--section", String(s));
          if (Array.isArray(args.seeds)) {
            for (const s of args.seeds) cliArgs.push("--seed", String(s ?? ""));
          }
        }
      }
      if (args.states !== undefined) {
        cliArgs.push("--states-json", JSON.stringify(args.states));
      }
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_open": {
      cliArgs.push("open");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      cliArgs.push("--filename", String(args.filename));
      if (args.reset) cliArgs.push("--reset");
      if (args.states !== undefined) {
        cliArgs.push("--states-json", JSON.stringify(args.states));
      }
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_close": {
      cliArgs.push("close");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      cliArgs.push("--filename", String(args.filename));
      if (args.force) cliArgs.push("--force");
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_outline": {
      cliArgs.push("outline");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      if (!args?.source) return toolText(JSON.stringify({ error: "source is required ('workspace' or 'disk')" }), true);
      cliArgs.push("--filename", String(args.filename));
      cliArgs.push("--source", String(args.source));
      if (args.filterStates) cliArgs.push("--filter-states", String(args.filterStates));
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_get": {
      cliArgs.push("get");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      if (!args?.sectionNumber) return toolText(JSON.stringify({ error: "sectionNumber is required" }), true);
      if (!args?.source) return toolText(JSON.stringify({ error: "source is required ('workspace' or 'disk')" }), true);
      cliArgs.push("--filename", String(args.filename));
      cliArgs.push("--section-number", String(args.sectionNumber));
      cliArgs.push("--source", String(args.source));
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_set": {
      cliArgs.push("set");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      cliArgs.push("--filename", String(args.filename));

      if (Array.isArray(args.writes)) {
        // Batch form: pipe via stdin? CLI accepts --writes-json. argv is
        // fine for normal sizes but use stdin when large to avoid E2BIG.
        const blob = JSON.stringify(args.writes);
        cliArgs.push("--writes-json", blob);
      } else {
        if (!args.sectionNumber) return toolText(JSON.stringify({ error: "sectionNumber or writes:[] is required" }), true);
        if (args.content === undefined && !args.contentFile) {
          return toolText(JSON.stringify({ error: "content or contentFile is required" }), true);
        }
        cliArgs.push("--section-number", String(args.sectionNumber));
        if (args.content !== undefined) cliArgs.push("--content", String(args.content));
        if (args.contentFile) cliArgs.push("--content-file", String(args.contentFile));
        if (args.title !== undefined) cliArgs.push("--title", String(args.title));
        if (args.seed !== undefined) cliArgs.push("--seed", String(args.seed));
        if (args.state) cliArgs.push("--state", String(args.state));
        if (args.mode) cliArgs.push("--mode", String(args.mode));
        if (args.by) cliArgs.push("--by", String(args.by));
        if (args.sessionId) cliArgs.push("--session-id", String(args.sessionId));
        if (args.force) cliArgs.push("--force");
      }
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_review": {
      cliArgs.push("review");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      if (!args?.sectionNumber) return toolText(JSON.stringify({ error: "sectionNumber is required" }), true);
      if (!args?.action && !args?.toState) {
        return toolText(JSON.stringify({ error: "action or toState is required" }), true);
      }
      cliArgs.push("--filename", String(args.filename));
      cliArgs.push("--section-number", String(args.sectionNumber));
      if (args.toState) cliArgs.push("--to-state", String(args.toState));
      else if (args.action) cliArgs.push("--action", String(args.action));
      if (args.notes) cliArgs.push("--notes", String(args.notes));
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_save": {
      cliArgs.push("save");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      cliArgs.push("--filename", String(args.filename));
      if (args.asFilename) cliArgs.push("--as-filename", String(args.asFilename));
      if (args.force) cliArgs.push("--force");
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_insert": {
      cliArgs.push("insert");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      cliArgs.push("--filename", String(args.filename));
      const positions = [args.before, args.after, args.under, args.topLevel ? "1" : null]
        .filter(x => x !== undefined && x !== null && x !== false).length;
      if (positions !== 1) {
        return toolText(JSON.stringify({
          error: "exactly one of before / after / under / topLevel is required",
        }), true);
      }
      if (args.before) cliArgs.push("--before", String(args.before));
      else if (args.after) cliArgs.push("--after", String(args.after));
      else if (args.under) cliArgs.push("--under", String(args.under));
      else if (args.topLevel) cliArgs.push("--top-level");

      if (args.title !== undefined) cliArgs.push("--title", String(args.title));
      if (args.seed) cliArgs.push("--seed", String(args.seed));
      if (args.content !== undefined) cliArgs.push("--content", String(args.content));
      if (args.contentFile) cliArgs.push("--content-file", String(args.contentFile));
      if (args.mode) cliArgs.push("--mode", String(args.mode));
      if (args.state) cliArgs.push("--state", String(args.state));
      if (args.by) cliArgs.push("--by", String(args.by));
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_delete": {
      cliArgs.push("delete");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      if (!args?.sectionNumber) return toolText(JSON.stringify({ error: "sectionNumber is required" }), true);
      cliArgs.push("--filename", String(args.filename));
      cliArgs.push("--section-number", String(args.sectionNumber));
      if (args.recursive === false) cliArgs.push("--no-recursive");
      if (args.force) cliArgs.push("--force");
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_move": {
      cliArgs.push("move");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      if (!args?.sectionNumber) return toolText(JSON.stringify({ error: "sectionNumber is required" }), true);
      cliArgs.push("--filename", String(args.filename));
      cliArgs.push("--section-number", String(args.sectionNumber));
      const positions = [args.before, args.after, args.under, args.topLevel ? "1" : null]
        .filter(x => x !== undefined && x !== null && x !== false).length;
      if (positions !== 1) {
        return toolText(JSON.stringify({
          error: "exactly one of before / after / under / topLevel is required",
        }), true);
      }
      if (args.before) cliArgs.push("--before", String(args.before));
      else if (args.after) cliArgs.push("--after", String(args.after));
      else if (args.under) cliArgs.push("--under", String(args.under));
      else if (args.topLevel) cliArgs.push("--top-level");
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_list": {
      cliArgs.push("list");
      if (args?.filename) cliArgs.push("--filename", String(args.filename));
      if (args?.filterStates) cliArgs.push("--filter-states", String(args.filterStates));
      if (args?.limit !== undefined) cliArgs.push("--limit", String(args.limit));
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_dispatch": {
      cliArgs.push("dispatch");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      if (!args?.sectionNumber) return toolText(JSON.stringify({ error: "sectionNumber is required" }), true);
      cliArgs.push("--filename", String(args.filename));
      cliArgs.push("--section-number", String(args.sectionNumber));
      if (args.kill) cliArgs.push("--kill");
      if (args.agent) cliArgs.push("--agent", String(args.agent));
      if (args.model) cliArgs.push("--model", String(args.model));
      if (args.sessionId) cliArgs.push("--session-id", String(args.sessionId));
      if (args.reuseSession === false) cliArgs.push("--no-reuse-session");
      if (args.extraInstructions) cliArgs.push("--extra-instructions", String(args.extraInstructions));
      if (args.resultState) cliArgs.push("--result-state", String(args.resultState));
      if (args.force) cliArgs.push("--force");
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_read": {
      cliArgs.push("read");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      if (!args?.assist && !args?.source) {
        return toolText(JSON.stringify({ error: "source is required ('workspace' or 'disk') for plain reads" }), true);
      }
      cliArgs.push("--filename", String(args.filename));
      // assist mode doesn't take source; pass workspace as a default
      // when assist is set (the cmd will route on assist).
      cliArgs.push("--source", String(args.source || "workspace"));
      if (args.sections) cliArgs.push("--sections", String(args.sections));
      if (args.format) cliArgs.push("--format", String(args.format));
      if (args.scope) cliArgs.push("--scope", String(args.scope));
      if (args.assist) cliArgs.push("--assist", String(args.assist));
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_patch": {
      cliArgs.push("patch");
      if (!args?.filename) return toolText(JSON.stringify({ error: "filename is required" }), true);
      if (!args?.sectionNumber) return toolText(JSON.stringify({ error: "sectionNumber is required" }), true);
      if (!args?.patch) return toolText(JSON.stringify({ error: "patch is required" }), true);
      cliArgs.push("--filename", String(args.filename));
      cliArgs.push("--section-number", String(args.sectionNumber));
      cliArgs.push("--patch", String(args.patch));
      if (args.scope) cliArgs.push("--scope", String(args.scope));
      if (args.state) cliArgs.push("--state", String(args.state));
      if (args.by) cliArgs.push("--by", String(args.by));
      if (args.force) cliArgs.push("--force");
      cliArgs.push("--pretty");
      break;
    }

    case "markdown_discover": {
      cliArgs.push("discover");
      if (args?.dir) cliArgs.push("--dir", String(args.dir));
      if (args?.filename) cliArgs.push("--filename", String(args.filename));
      if (args?.recursive) cliArgs.push("--recursive");
      if (args?.limit !== undefined) cliArgs.push("--limit", String(args.limit));
      if (args?.ignoreGitignore) cliArgs.push("--ignore-gitignore");
      cliArgs.push("--pretty");
      break;
    }

    default:
      return toolText(`Unknown tool: ${toolName}`, true);
  }

  const result = await execCore(cliArgs, stdinPayload);
  return wrapResult(toolName, result);
}

// ---------------------------------------------------------------------------
// State schema parameter (shared between create + open)
// ---------------------------------------------------------------------------

const STATES_SCHEMA = {
  description:
    "Optional state schema declaration. Accepts a list of bare strings " +
    "(positional inference: first=initial, last=terminal) OR a list of " +
    "objects with explicit role flags (initial/working/terminal/needsAttention). " +
    "Default: ['pending','done']. Examples: ['todo','doing','done'] for a task " +
    "tracker; [{name:'draft'},{name:'review',working:true},{name:'live',terminal:true}] " +
    "for a publication pipeline.",
  oneOf: [
    { type: "array", items: { type: "string" } },
    {
      type: "array",
      items: {
        type: "object",
        required: ["name"],
        properties: {
          name: { type: "string" },
          initial: { type: "boolean" },
          working: { type: "boolean" },
          terminal: { type: "boolean" },
          needsAttention: { type: "boolean" },
        },
      },
    },
  ],
};

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

const TOOLS: Tool[] = [
  {
    name: "markdown_guide",
    description:
      "START HERE if this is a long task, a refactor, a requirements doc, or " +
      "anything where you'll think across multiple turns. Returns the agent's " +
      "operating manual for the markdown-helper, which doubles as your " +
      "externalized brain in addition to editing markdown files. Covers: the " +
      "three operating modes (doc editing, agent brain, requirements refinement), " +
      "the canonical brain shape, decision-matrix-walk recipes, anti-pattern " +
      "cards, and the full state-schema + response-shape spec.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "markdown_create",
    description:
      "Use this when:\n" +
      "  - Starting a new document (plan, requirements, design doc, README).\n" +
      "  - Starting a long task and you want a BRAIN: create a doc with " +
      "sections HOW-TO-USE, FINAL-DECISIONS, Active-execution-tracker, " +
      "Follow-up-tasks. The markdown-helper tracks your progress, decisions, " +
      "and rationale across the entire effort. Future-you (post-compaction or " +
      "next session) inherits a navigable, filterable index instead of " +
      "scrollback-archaeology. See markdown_guide for the brain pattern.\n\n" +
      "Scaffold a new markdown file. File must NOT exist on disk. Provide " +
      "`sections` (list of {title,seed?,content?,children?}) and optional " +
      "`states` (custom state schema; defaults to ['pending','done']; for a " +
      "brain use ['pending','in-progress','blocked','under-review','done','deferred']). " +
      "Section content is NOT written to disk until markdown_save is called.",
    inputSchema: {
      type: "object",
      required: ["filename"],
      properties: {
        filename: { type: "string" },
        sections: {
          oneOf: [
            { type: "array", items: { type: "string" } },
            {
              type: "array",
              items: {
                type: "object",
                properties: {
                  title: { type: "string" },
                  seed: { type: "string" },
                  content: { type: "string" },
                  children: { type: "array", items: { type: "object" } },
                },
              },
            },
          ],
        },
        seeds: { type: "array", items: { type: "string" } },
        states: STATES_SCHEMA,
      },
    },
  },
  {
    name: "markdown_open",
    description:
      "Use this when you want to read, edit, or resume work on a markdown file " +
      "that ALREADY EXISTS on disk. (For scaffolding new files, use markdown_create.)\n\n" +
      "Typical follow-up: markdown_outline with filterStates='in-progress,blocked' " +
      "to find where you left off.\n\n" +
      "NON-DESTRUCTIVE BY DEFAULT. Three outcome branches:\n" +
      "  1. No workspace exists yet -> sections enter at pseudo-state 'loaded'. " +
      "First write transitions them to your schema's initial state. No force needed.\n" +
      "  2. Workspace exists, content matches disk -> all per-section state preserved.\n" +
      "  3. Workspace exists, content diverges -> response includes " +
      "`divergence: [{sectionNumber, diskHash, workspaceHash, recommendedAction}]`. " +
      "Workspace is NOT mutated. Resolve via save / open reset:true / get+set.\n\n" +
      "Pass reset:true to discard the workspace and reload from disk explicitly.\n\n" +
      "FOREIGN files (absolute path outside this project) open READ-ONLY: every " +
      "section comes in at state 'readonly' and all writes refuse. To edit a foreign " +
      "file, use markdown_save with asFilename to copy it into the current project.",
    inputSchema: {
      type: "object",
      required: ["filename"],
      properties: {
        filename: { type: "string" },
        reset: { type: "boolean", description: "Discard workspace and reload from disk." },
        states: STATES_SCHEMA,
      },
    },
  },
  {
    name: "markdown_close",
    description:
      "Use this when you're done with a file and want to free the workspace cache. " +
      "Rarely needed -- workspaces are cheap, leaving them open across a session is fine.\n\n" +
      "Drops a file's workspace cache. The file on disk is NOT touched. " +
      "Refuses with running dispatches or unsaved changes unless force=true.\n\n" +
      "CAUTION: force=true on a file someone else (parallel session, sub-agent) is " +
      "actively writing destroys their in-progress state metadata. Only force-close " +
      "workspaces you exclusively own.",
    inputSchema: {
      type: "object",
      required: ["filename"],
      properties: {
        filename: { type: "string" },
        force: { type: "boolean" },
      },
    },
  },
  {
    name: "markdown_outline",
    description:
      "Use this when you want to see what's in a doc at a glance -- THE SCOREBOARD. " +
      "Run early in a session to find live work; run between batches of mutations to " +
      "re-orient after section numbers shift. The filterStates parameter is your " +
      "friend: filterStates='in-progress,blocked,under-review' surfaces only what " +
      "needs attention, ignoring 100s of settled or historical sections.\n\n" +
      "Requires explicit `source`:\n" +
      "  workspace: in-memory view (must be open). Includes state, seeds, save " +
      "staleness, nextFocus.\n" +
      "  disk: parses the file fresh; no state machine, no states returned. " +
      "Side-effect: writes a touched.json marker so the file shows up in markdown_list.\n\n" +
      "filterStates is comma-separated (e.g., 'pending,in-progress'). Matches by exact " +
      "state name. Filters individual sections, not subtrees -- a parent in 'done' " +
      "state will not surface a child in 'in-progress' state via the parent. " +
      "filterStates is workspace-only (disk has no states to filter).",
    inputSchema: {
      type: "object",
      required: ["filename", "source"],
      properties: {
        filename: { type: "string" },
        source: { type: "string", enum: ["workspace", "disk"] },
        filterStates: { type: "string", description: "Comma-separated state names." },
      },
    },
  },
  {
    name: "markdown_get",
    description:
      "Use this when you want to inspect ONE section in full detail, including its " +
      "revision history (workspace only). For reading multiple sections at once, " +
      "use markdown_read.\n\n" +
      "Returns the section's content + seed (workspace only) + revision history " +
      "(workspace only). Revisions include every state transition with notes -- " +
      "useful for understanding why a section is in its current state. " +
      "Requires `source` ('workspace' or 'disk').",
    inputSchema: {
      type: "object",
      required: ["filename", "sectionNumber", "source"],
      properties: {
        filename: { type: "string" },
        sectionNumber: { type: "string" },
        source: { type: "string", enum: ["workspace", "disk"] },
      },
    },
  },
  {
    name: "markdown_set",
    description:
      "Use this when you've reached a conclusion, finished a work item, captured a " +
      "decision, or want to rewrite a section's body wholesale. For surgical edits " +
      "to part of a section, prefer markdown_patch instead -- it's cheaper and " +
      "leaves an audit trail.\n\n" +
      "For pure state transitions (without rewriting content), use markdown_review.\n\n" +
      "Two call shapes:\n" +
      "  Single: { sectionNumber, content, state?, mode?, title?, seed? }\n" +
      "  Batch:  { writes: [{sectionNumber, content, ...}, ...] }  -- one " +
      "transaction, all-or-nothing.\n\n" +
      "MODES:\n" +
      "  body (default): content replaces section body literally; any markdown " +
      "headings inside the content stay as literal text.\n" +
      "  subtree: content is parsed via CommonMark. Markdown headings at depth+1 " +
      "(or deeper) relative to the target section auto-split into descendant " +
      "sections, in source order. Existing descendants are REPLACED. To preserve " +
      "and reorder descendants, use markdown_move after the set, not subtree mode.\n\n" +
      "STATE: optional, must be in declared schema. If omitted: writing a 'loaded' " +
      "section transitions it to schema's initial state; otherwise the state stays " +
      "put.\n\n" +
      "FORCE: required only when overwriting a section in a TERMINAL state. " +
      "loaded/initial/working/needsAttention sections accept writes freely.\n\n" +
      "Returns standard changeReport + outline + userSummary. Surface userSummary " +
      "verbatim to user when they ask for status. The outline reflects post-mutation " +
      "section numbers -- re-plan against it.",
    inputSchema: {
      type: "object",
      required: ["filename"],
      properties: {
        filename: { type: "string" },
        sectionNumber: { type: "string" },
        content: { type: "string" },
        contentFile: { type: "string" },
        title: { type: "string" },
        seed: { type: "string" },
        state: { type: "string" },
        mode: { type: "string", enum: ["body", "subtree"] },
        force: { type: "boolean" },
        by: { type: "string" },
        sessionId: { type: "string" },
        writes: {
          type: "array",
          items: {
            type: "object",
            required: ["sectionNumber", "content"],
            properties: {
              sectionNumber: { type: "string" },
              content: { type: "string" },
              title: { type: "string" },
              seed: { type: "string" },
              state: { type: "string" },
              mode: { type: "string", enum: ["body", "subtree"] },
            },
          },
        },
      },
    },
  },
  {
    name: "markdown_review",
    description:
      "Use this when a section's STATUS changes without its CONTENT changing -- a " +
      "work item starts, finishes, blocks, gets accepted, or gets deferred. The " +
      "lightweight cousin of markdown_set: state-only transition with audit note, " +
      "no body rewrite, no force needed (terminal states transition freely via review).\n\n" +
      "Use markdown_set instead when content AND state change together (e.g., " +
      "completing a work item by recording its final conclusions in the body).\n\n" +
      "Two modes:\n" +
      "  toState: '<name>'  -- explicit, must be in declared schema.\n" +
      "  action: 'accept'   -- shortcut for the schema's terminal state.\n" +
      "  action: 'reject'   -- shortcut for the needsAttention state (requires notes).\n\n" +
      "Notes are recorded in the revision history as searchable audit trail. Write " +
      "good notes -- one sentence saying why the transition happened. Future-you " +
      "reads these to understand state changes after the fact.",
    inputSchema: {
      type: "object",
      required: ["filename", "sectionNumber"],
      properties: {
        filename: { type: "string" },
        sectionNumber: { type: "string" },
        toState: { type: "string" },
        action: { type: "string", enum: ["accept", "reject"] },
        notes: { type: "string" },
      },
    },
  },
  {
    name: "markdown_save",
    description:
      "Use this at NATURAL PAUSES in your work -- end of a work item, end of a " +
      "planning pass, before a long-running command, before commit. NOT after " +
      "every individual set/insert/patch. Save is a full file write with a large " +
      "response; batch 5-20 mutations per save.\n\n" +
      "Flushes the workspace state to disk. Per-section state is NOT mutated; " +
      "state survives save+reopen. Refuses on unfilled stub sections (titled leaves " +
      "with no body and no children) unless force=true.\n\n" +
      "asFilename: write to <agent-cwd>/<asFilename> instead of the original location, " +
      "creating directories as needed. The workspace key flips to the new local " +
      "name. **REQUIRED for editing foreign (cross-project, read-only) files**: " +
      "open the foreign file (it comes in read-only), then save with asFilename to " +
      "promote it to an editable local copy. From there, edit normally; ship back " +
      "to the source repo via shell cp + git commit when done.",
    inputSchema: {
      type: "object",
      required: ["filename"],
      properties: {
        filename: { type: "string" },
        asFilename: { type: "string" },
        force: { type: "boolean" },
      },
    },
  },
  {
    name: "markdown_insert",
    description:
      "Use this when a new fact, decision, work item, or anti-pattern surfaces " +
      "and you want to record it without disturbing what's already there. The " +
      "primary write tool for brain-mode capture: discover something, insert a " +
      "topic card under the right parent, set its state, move on.\n\n" +
      "Inserts a new section. Use EXACTLY ONE of: before, after, under, topLevel.\n\n" +
      "Positioning semantics (resolve target by TITLE, never by section number -- " +
      "numbers shift on every mutation):\n" +
      "  under: '<parent-title>'  -> appended as the LAST CHILD of <parent>\n" +
      "  before: '<sibling>'      -> inserted as a same-depth sibling, IMMEDIATELY " +
      "BEFORE <sibling>\n" +
      "  after: '<sibling>'       -> inserted as a same-depth sibling, IMMEDIATELY " +
      "AFTER <sibling>\n" +
      "  topLevel: true           -> appended as the LAST TOP-LEVEL section\n\n" +
      "If content has markdown headings and mode='subtree', the headings auto-split " +
      "into descendant sections (in source order). For literal headings inside the " +
      "section body, use mode='body' (the default).\n\n" +
      "Returns standard changeReport + outline + userSummary. SectionNumbers shift " +
      "on insert; re-plan against the post-mutation outline.",
    inputSchema: {
      type: "object",
      required: ["filename"],
      properties: {
        filename: { type: "string" },
        before: { type: "string" },
        after: { type: "string" },
        under: { type: "string" },
        topLevel: { type: "boolean" },
        title: { type: "string" },
        seed: { type: "string" },
        content: { type: "string" },
        contentFile: { type: "string" },
        mode: { type: "string", enum: ["body", "subtree"] },
        state: { type: "string" },
        by: { type: "string" },
      },
    },
  },
  {
    name: "markdown_delete",
    description:
      "Use this when a section is obsolete and the content shouldn't be preserved. " +
      "Before deleting load-bearing content, consider: would future-you want to " +
      "know this existed? If yes, write a sister card capturing what was removed " +
      "and why FIRST, then delete.\n\n" +
      "Removes a section. Deletes descendants by default (recursive=true). " +
      "Refuses if a sub-agent is currently running on the section unless force=true. " +
      "Returns standard changeReport + outline + userSummary.",
    inputSchema: {
      type: "object",
      required: ["filename", "sectionNumber"],
      properties: {
        filename: { type: "string" },
        sectionNumber: { type: "string" },
        recursive: { type: "boolean" },
        force: { type: "boolean" },
      },
    },
  },
  {
    name: "markdown_move",
    description:
      "Use this when a section is at the wrong nesting depth or in the wrong " +
      "reading order. Common after a subtree-insert that promoted children to " +
      "the wrong parent: move them under their intended parent.\n\n" +
      "Moves a section to a new position. UUID + revision history are preserved. " +
      "Use EXACTLY ONE of: before, after, under, topLevel (same semantics as " +
      "markdown_insert).\n\n" +
      "Move-in-batch caveat: section numbers shift on every move. When reordering " +
      "siblings, move them in reverse order (last first) so the earlier ones don't " +
      "shift out from under you. Or use TITLES as handles and re-check the outline " +
      "between moves.\n\n" +
      "Returns standard changeReport + outline + userSummary.",
    inputSchema: {
      type: "object",
      required: ["filename", "sectionNumber"],
      properties: {
        filename: { type: "string" },
        sectionNumber: { type: "string" },
        before: { type: "string" },
        after: { type: "string" },
        under: { type: "string" },
        topLevel: { type: "boolean" },
      },
    },
  },
  {
    name: "markdown_patch",
    description:
      "Use this for small surgical edits to a large section. Cheaper than " +
      "markdown_set (no full-section rewrite) and produces a clearer audit trail. " +
      "Preferred for fixing typos, updating one paragraph, or appending a bullet " +
      "to an existing list.\n\n" +
      "Workflow: markdown_read with format='markdown' to see the EXACT current text " +
      "(matching is literal -- whitespace and indentation must match exactly), then " +
      "markdown_patch with the search-replace block.\n\n" +
      "PATCH FORMAT (multiple blocks supported in one call):\n\n" +
      "  <<<<<<< SEARCH\n  exact text to find\n  =======\n  replacement text\n  >>>>>>> REPLACE\n\n" +
      "Matching rules:\n" +
      "  - SEARCH is LITERAL text, NOT regex. Whitespace, newlines, and indentation " +
      "must match exactly.\n" +
      "  - Each SEARCH must match exactly once. Zero matches and 2+ matches are both " +
      "rejected with an actionable error naming the section and the candidate lines, " +
      "so you can add more surrounding context to disambiguate.\n" +
      "  - REPLACE may be empty (deletes the matched text). SEARCH must be non-empty.\n\n" +
      "scope='body' (default) patches just the section's body. scope='subtree' " +
      "re-parses the patched markdown via auto-split, so introducing a new `## " +
      "Heading` via patch creates a new descendant section automatically. Use " +
      "subtree scope when you want to read+patch a parent + its children as one " +
      "unit (read with format='markdown' scope='subtree' to get the exact text).",
    inputSchema: {
      type: "object",
      required: ["filename", "sectionNumber", "patch"],
      properties: {
        filename: { type: "string" },
        sectionNumber: { type: "string" },
        scope: { type: "string", enum: ["body", "subtree"] },
        patch: { type: "string" },
        state: { type: "string" },
        force: { type: "boolean" },
        by: { type: "string" },
      },
    },
  },
  {
    name: "markdown_list",
    description:
      "Run this NEAR THE START of any session to see what plans, brains, and " +
      "in-flight requirements docs exist in this project. The quickest way to find " +
      "out: is there a prior brain doc I should resume? Are there unsaved changes " +
      "from a previous session? Did the operator leave me a partial plan to pick " +
      "up?\n\n" +
      "Returns open workspaces (with stateCounts -- a quick scoreboard of " +
      "work-in-progress -- and schema, needsSave, lastSavedAt) plus recently-touched " +
      "markdown files (read via source:'disk' but not opened, have lastReadAt + sha " +
      "+ sizeBytes). With filename: detailed status of one file.\n\n" +
      "Use this to: resume prior work, find project planning docs, check what's " +
      "mid-flight before starting new work, and discover unsaved changes that need " +
      "flushing.",
    inputSchema: {
      type: "object",
      properties: {
        filename: { type: "string" },
        filterStates: { type: "string", description: "Comma-separated state names." },
        limit: { type: "integer" },
      },
    },
  },
  {
    name: "markdown_discover",
    description:
      "Use this when you need to find markdown files in the project without " +
      "committing to opening them. Complementary to markdown_list (which only " +
      "shows already-open or already-touched files): discover walks the filesystem.\n\n" +
      "Honors .gitignore by default; agents can override by naming an ignored " +
      "directory in `dir` (gitignore inheritance is transitive; respecting it " +
      "inside an ignored directory would surface nothing). Recursive is OFF by " +
      "default -- agents must opt into wandering.\n\n" +
      "filename can be a glob ('*.md', 'REQ_*.md') OR a regex ('/req/', 'REQ.*'). " +
      "Auto-detected from metacharacters or /.../ wrapping. Case-insensitive match.\n\n" +
      "Typical follow-up: markdown_outline source='disk' on a candidate file (this " +
      "marks it touched and shows section structure without opening), then " +
      "markdown_open if you want to work on it.",
    inputSchema: {
      type: "object",
      properties: {
        dir: { type: "string", description: "cwd-relative directory (default cwd)." },
        filename: { type: "string", description: "Glob or regex name filter." },
        recursive: { type: "boolean", description: "Default false." },
        limit: { type: "integer", description: "Default 50." },
        ignoreGitignore: { type: "boolean" },
      },
    },
  },
  {
    name: "markdown_dispatch",
    description:
      "Use this when a section needs deep elaboration you'd rather delegate -- " +
      "researching a single requirement, drafting a complex design decision, " +
      "writing a long subsection -- so you can keep working on the rest of the doc.\n\n" +
      "Spawns an opencode sub-agent on a section, OR kills a running one (kill=true). " +
      "Requires your declared schema to have a state with the 'working' role flag, " +
      "so the section lifecycle is visible in outlines.\n\n" +
      "Lifecycle:\n" +
      "  1. Section transitions to the 'working' state (or to resultState if you pass it).\n" +
      "  2. Sub-agent receives the section's content, the doc's HOW-TO if present, " +
      "and any extraInstructions you pass.\n" +
      "  3. When the sub-agent calls markdown_set on the section, it transitions out " +
      "of 'working' state into whatever state the set specified (or schema initial).\n" +
      "  4. If the sub-agent fails or never calls set, the section stays in 'working' " +
      "until you kill the dispatch or manually transition it.\n\n" +
      "extraInstructions: free-form text injected into the sub-agent's prompt. Use " +
      "this to constrain scope, point at reference material, or specify output shape.\n\n" +
      "Mid-flight progress: poll markdown_outline -- the section's state will still " +
      "show 'working' until the sub-agent completes its set. There is no live " +
      "progress feed; the state machine IS the progress indicator.",
    inputSchema: {
      type: "object",
      required: ["filename", "sectionNumber"],
      properties: {
        filename: { type: "string" },
        sectionNumber: { type: "string" },
        kill: { type: "boolean" },
        agent: { type: "string" },
        model: { type: "string" },
        sessionId: { type: "string" },
        reuseSession: { type: "boolean" },
        extraInstructions: { type: "string" },
        resultState: { type: "string" },
        force: { type: "boolean" },
      },
    },
  },
  {
    name: "markdown_read",
    description:
      "Use this when you want the BODY of one or more sections. Different from " +
      "markdown_outline (which shows structure + state, no body) and markdown_get " +
      "(which is for one section with full revision history).\n\n" +
      "Requires explicit `source` ('workspace' or 'disk') for plain reads. Disk " +
      "reads write a touched.json marker so the file shows up in markdown_list.\n\n" +
      "FORMATS:\n" +
      "  structured (default): JSON list of sections with content fields. Best for " +
      "programmatic processing.\n" +
      "  markdown: render the requested sections as a single markdown string. Use " +
      "with scope='subtree' to render section + descendants -- this is the form " +
      "markdown_patch operates against. Always do this BEFORE markdown_patch to see " +
      "the exact text the patch needs to match.\n\n" +
      "ASSIST: pass `assist` with a question; spawns a sub-agent via runner that " +
      "reads the file and answers with citations. Returns runId; poll with " +
      "runner_status. Use this when a large doc has the answer you need but you " +
      "don't want to spend tokens reading the whole thing.",
    inputSchema: {
      type: "object",
      required: ["filename"],
      properties: {
        filename: { type: "string" },
        sections: { type: "string", description: "Range spec like 'A.1,A.2-A.4'. Omit for all." },
        source: { type: "string", enum: ["workspace", "disk"] },
        format: { type: "string", enum: ["structured", "markdown"] },
        scope: { type: "string", enum: ["body", "subtree"] },
        assist: { type: "string" },
      },
    },
  },
];

// ---------------------------------------------------------------------------
// Server bootstrap
// ---------------------------------------------------------------------------

const server = new Server(
  { name: "markdown-helper", version: "2.0.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  // dispatch returns our WrappedResult shape; cast to `any` because the
  // MCP SDK's CallToolResult is overly strict (doesn't model the
  // passthrough fields like `title`/`metadata`/`output` we use for
  // Lever A rendering). The SDK passes them through verbatim.
  return (await dispatch(name, args ?? {})) as any;
});

const transport = new StdioServerTransport();
await server.connect(transport);
