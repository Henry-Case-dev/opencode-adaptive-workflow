import fs from "node:fs/promises"
import path from "node:path"
import os from "node:os"
import crypto from "node:crypto"
import { fileURLToPath } from "node:url"

const PLUGIN_ID = "workflow.intelligence"
const MANAGED_BY = "opencode-global-intelligence-v3"
const here = path.dirname(fileURLToPath(import.meta.url))
const backend = path.join(here, "workflow_intel.py")
const initializedScratch = new Set()

// Single portable storage root: derived from the installed global plugin location.
// Never derive workflow files from the current repository's parent or system temp.
const workspace = path.resolve(here, "..", "..", "WorkspaceData")
function scratchBase() { return path.join(workspace, "temp", "intelligence") }
function cacheBase() { return path.join(workspace, "cache", "intelligence") }

function repoKey(repo) {
  const normalized = path.resolve(repo)
  const digest = crypto.createHash("sha1").update(normalized).digest("hex").slice(0, 12)
  return `${path.basename(normalized)}-${digest}`
}

function repoScratch(repo) {
  return path.join(scratchBase(), repoKey(repo))
}

async function exists(p) {
  try {
    await fs.stat(p)
    return true
  } catch {
    return false
  }
}

async function findGitRoot(start) {
  let current = path.resolve(start || process.cwd())
  while (true) {
    if (await exists(path.join(current, ".git"))) return current
    const parent = path.dirname(current)
    if (parent === current) return path.resolve(start || process.cwd())
    current = parent
  }
}

async function ensureScratch(repo) {
  const root = repoScratch(repo)
  const tmp = path.join(root, "tmp")
  const lanes = path.join(root, "lanes")
  const pycache = path.join(root, "pycache")
  await fs.mkdir(tmp, { recursive: true })
  await fs.mkdir(lanes, { recursive: true })
  await fs.mkdir(pycache, { recursive: true })

  const marker = path.join(root, ".opencode-workflow-scratch.json")
  if (!(await exists(marker))) {
    await fs.writeFile(
      marker,
      JSON.stringify({ repo: path.resolve(repo), managedBy: MANAGED_BY }, null, 2),
      "utf8",
    )
  }

  // No TTL-based cleanup. Owned scratch is retired at verified epic completion.
  initializedScratch.add(root)

  return { root, tmp, lanes, pycache }
}

async function sessionDirectory(ctx, toolContext) {
  try {
    const raw = await ctx.session.get({ sessionID: toolContext.sessionID })
    const session = raw?.data ?? raw
    const dir = session?.location?.directory
    if (typeof dir === "string" && dir.trim()) return dir
  } catch {
    // Fall through to process.cwd().
  }
  return process.cwd()
}

function pythonCandidates() {
  const explicit = (process.env.OPENCODE_PYTHON || "").trim()
  if (explicit) return [[explicit]]
  if (process.platform === "win32") return [["py", "-3"], ["python"], ["python3"]]
  return [["python3"], ["python"], ["py", "-3"]]
}

async function runBackend(repo, args) {
  const scratch = await ensureScratch(repo)
  const env = {
    ...process.env,
    OPENCODE_WORKFLOW_CACHE_ROOT: cacheBase(),
    OPENCODE_WORKFLOW_SCRATCH_ROOT: scratchBase(),
    OPENCODE_WORKFLOW_SCRATCH: scratch.root,
    TMP: scratch.tmp,
    TEMP: scratch.tmp,
    TMPDIR: scratch.tmp,
    XDG_CACHE_HOME: path.join(workspace, "cache", "xdg"),
    PIP_CACHE_DIR: path.join(workspace, "cache", "pip"),
    UV_CACHE_DIR: path.join(workspace, "cache", "uv"),
    npm_config_cache: path.join(workspace, "cache", "npm"),
    PYTHONPYCACHEPREFIX: scratch.pycache,
    PYTHONUTF8: "1",
    PYTHONIOENCODING: "utf-8",
  }

  const failures = []
  for (const prefix of pythonCandidates()) {
    try {
      const proc = Bun.spawn([...prefix, backend, ...args], {
        cwd: repo,
        stdout: "pipe",
        stderr: "pipe",
        env,
      })
      const [stdout, stderr, code] = await Promise.all([
        new Response(proc.stdout).text(),
        new Response(proc.stderr).text(),
        proc.exited,
      ])
      if (code === 0) return stdout.trim()
      failures.push(`${prefix.join(" ")}: exit ${code}: ${stderr.trim() || stdout.trim()}`)
    } catch (error) {
      failures.push(`${prefix.join(" ")}: ${String(error)}`)
    }
  }
  throw new Error(
    `workflow_intel backend failed. Python candidates were tried successfully or failed to launch; inspect the per-candidate errors. Set OPENCODE_PYTHON only if no usable Python 3 executable is found. ${failures.join(" | ")}`,
  )
}

const ACTIONS = [
  "refresh",
  "locate",
  "refs",
  "search",
  "impact",
  "stats",
  "test_audit",
  "test_baseline",
  "manifest_validate",
  "feature_context",
  "scratch_status",
]

function makeToolDefinition(ctx) {
  return {
  name: "workflow_intel",
  description:
    "Global repository intelligence and maintenance cache. Git-incremental SQLite + Python AST/light structural index, feature-manifest context/validation, test lifecycle audit, and managed scratch status. Use before broad repository rediscovery; current code/runtime remain authoritative.",
  input: {
    type: "object",
    properties: {
      action: { type: "string", enum: ACTIONS },
      query: { type: "string" },
      feature: { type: "string" },
      limit: { type: "integer", minimum: 1, maximum: 100 },
      full: { type: "boolean" },
    },
    required: ["action"],
    additionalProperties: false,
  },
  options: { codemode: true },
  async execute(input, toolContext) {
    const dir = await sessionDirectory(ctx, toolContext)
    const repo = await findGitRoot(dir)
    const argv = [input.action, "--repo", repo, "--limit", String(input.limit ?? 20)]
    if (input.query) argv.push("--query", input.query)
    if (input.feature) argv.push("--feature", input.feature)
    if (input.full) argv.push("--full")
    const stdout = await runBackend(repo, argv)
    let structured = null
    try {
      structured = JSON.parse(stdout)
    } catch {
      structured = { raw: stdout }
    }
    return {
      structured,
      content: [{ type: "text", text: stdout }],
    }
  },
}
}

export default {
  id: PLUGIN_ID,
  async setup(ctx) {
    // Avoid @opencode-ai/plugin imports: current V2 local-plugin loading accepts
    // the plain {id, setup} module shape and this avoids local package resolver issues.
    const toolDefinition = makeToolDefinition(ctx)

    await ctx.tool.transform((tools) => {
      tools.add(toolDefinition)
    })

    await ctx.shell.hook("create.before", async (event) => {
      try {
        const repo = await findGitRoot(event.cwd)
        const scratch = await ensureScratch(repo)
        event.env.OPENCODE_WORKFLOW_SCRATCH = scratch.root
        event.env.OPENCODE_WORKFLOW_CACHE_ROOT = cacheBase()
        event.env.OPENCODE_WORKFLOW_SCRATCH_ROOT = scratchBase()
        event.env.TMP = scratch.tmp
        event.env.TEMP = scratch.tmp
        event.env.TMPDIR = scratch.tmp
        event.env.PYTHONPYCACHEPREFIX = scratch.pycache
        event.env.XDG_CACHE_HOME = path.join(workspace, "cache", "xdg")
        event.env.PIP_CACHE_DIR = path.join(workspace, "cache", "pip")
        event.env.UV_CACHE_DIR = path.join(workspace, "cache", "uv")
        event.env.npm_config_cache = path.join(workspace, "cache", "npm")
        event.env.PYTHONUTF8 = "1"
        event.env.PYTHONIOENCODING = "utf-8"
      } catch {
        // Fail open: shell execution must continue even if hygiene setup fails.
      }
    })
  },
}
