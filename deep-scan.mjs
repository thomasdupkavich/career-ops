#!/usr/bin/env node
/**
 * deep-scan.mjs — on-demand AI discovery layer (spends tokens).
 *
 * Fans out two headless agents IN PARALLEL — `claude -p` and `codex exec` —
 * each asked to DISCOVER new companies / niche job boards that aren't already
 * tracked, matching the user's targeting. Each returns a single fenced ```json
 * block. Their postings flow through the same shared sink (scan-lib), and their
 * company suggestions are appended to data/portals-suggestions.md for the user
 * to review — portals.yml (user-layer config) is NEVER auto-edited.
 *
 *   import { runDeepScan } from './deep-scan.mjs'
 *   const result = await runDeepScan({ onLog })
 *
 * Standalone:  node deep-scan.mjs            (LIVE — uses tokens)
 *              node deep-scan.mjs --dry-run  (no writes; agents still run)
 *
 * This is only invoked when the user clicks "Deep Scan" — the default Scan Now
 * path never spawns these agents.
 */

import { spawn } from 'node:child_process';
import { appendFileSync, existsSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { appendPostings, today } from './scan-lib.mjs';

const ROOT = dirname(fileURLToPath(import.meta.url));
const SUGGESTIONS_PATH = join(ROOT, 'data', 'portals-suggestions.md');
const AGENT_TIMEOUT_MS = Number(process.env.DEEP_SCAN_TIMEOUT_MS || 6 * 60 * 1000);

const PROMPT = `You are a job-discovery agent for the "career-ops" job search system. Working directory is the career-ops project root.

TASK — discover NEW software-engineering job postings and companies that are NOT already tracked:
1. Read config/profile.yml and modes/_profile.md to learn the user's target roles, stack, location policy, and salary floor.
2. Read portals.yml to see which companies are ALREADY tracked — do NOT repeat those.
3. Read the first ~30 lines of data/scan-history.tsv to see recently-seen URLs — do NOT repeat those.
4. Use web search and any available job-board tools (Indeed / Dice / ZipRecruiter / Greenhouse / Ashby / Lever) to find fresh postings that match the targeting and respect the location policy.
5. Also identify NEW companies (with an ATS careers URL) worth adding to the tracker.

DISCOVERY ONLY — do not score, do not write any files, do not apply.

OUTPUT CONTRACT — your FINAL message must be ONLY a single fenced json block, nothing before or after:
\`\`\`json
{"postings":[{"url":"https://...","company":"...","title":"...","location":"..."}],
 "portals_suggestions":[{"name":"...","careers_url":"https://...","provider":"greenhouse|ashby|lever|recruitee|smartrecruiters"}]}
\`\`\`
Use real, verified URLs only. If you find nothing, return empty arrays. Keep to at most 25 postings.`;

const AGENTS = [
  {
    name: 'claude',
    portal: 'deep-claude',
    cmd: 'claude',
    args: [
      '-p', PROMPT,
      '--model', process.env.DEEP_SCAN_CLAUDE_MODEL || 'sonnet',
      '--dangerously-skip-permissions',
    ],
  },
  {
    name: 'codex',
    portal: 'deep-codex',
    cmd: 'codex',
    args: [
      'exec', PROMPT,
      '--cd', ROOT,
      '--skip-git-repo-check',
      '--dangerously-bypass-approvals-and-sandbox',
      ...(process.env.DEEP_SCAN_CODEX_MODEL ? ['--model', process.env.DEEP_SCAN_CODEX_MODEL] : []),
    ],
  },
];

/** Pull the JSON payload out of an agent's free-form stdout. Never throws. */
export function extractJsonBlock(text) {
  if (!text) return null;
  // Prefer a ```json fenced block (last one wins).
  const fences = [...text.matchAll(/```(?:json)?\s*([\s\S]*?)```/gi)];
  for (let i = fences.length - 1; i >= 0; i--) {
    try {
      const obj = JSON.parse(fences[i][1].trim());
      if (obj && typeof obj === 'object') return obj;
    } catch { /* try next fence */ }
  }
  // Fallback: last balanced object that parses and looks like our contract.
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start !== -1 && end > start) {
    try {
      const obj = JSON.parse(text.slice(start, end + 1));
      if (obj && typeof obj === 'object') return obj;
    } catch { /* give up */ }
  }
  return null;
}

function runAgent(agent, onLog) {
  return new Promise((resolve) => {
    onLog(`${agent.name}: starting discovery (uses tokens)…`);
    let proc;
    try {
      proc = spawn(agent.cmd, agent.args, { cwd: ROOT, env: process.env });
    } catch (err) {
      onLog(`${agent.name}: could not start (${err.message}) — skipping.`);
      resolve({ agent, postings: [], suggestions: [] });
      return;
    }

    let stdout = '';
    const timer = setTimeout(() => {
      onLog(`${agent.name}: timed out after ${Math.round(AGENT_TIMEOUT_MS / 1000)}s — killing.`);
      proc.kill('SIGKILL');
    }, AGENT_TIMEOUT_MS);

    proc.stdout.on('data', d => { stdout += String(d); });
    proc.stderr.on('data', d => {
      String(d).split('\n').filter(Boolean).slice(0, 3).forEach(line => onLog(`  ${agent.name}: ${line.slice(0, 160)}`));
    });
    proc.on('error', err => {
      clearTimeout(timer);
      onLog(`${agent.name}: error (${err.message}) — skipping.`);
      resolve({ agent, postings: [], suggestions: [] });
    });
    proc.on('close', () => {
      clearTimeout(timer);
      const parsed = extractJsonBlock(stdout);
      if (!parsed) {
        onLog(`${agent.name}: no parseable JSON returned (0 postings).`);
        resolve({ agent, postings: [], suggestions: [] });
        return;
      }
      const postings = Array.isArray(parsed.postings) ? parsed.postings : [];
      const suggestions = Array.isArray(parsed.portals_suggestions) ? parsed.portals_suggestions : [];
      onLog(`${agent.name}: ${postings.length} postings, ${suggestions.length} company suggestions.`);
      resolve({ agent, postings, suggestions });
    });
  });
}

function writeSuggestions(rows, { dryRun }) {
  const clean = rows.filter(s => s && s.name && s.careers_url);
  if (clean.length === 0 || dryRun) return clean.length;
  const block = [
    `\n## ${today()} — Deep Scan suggestions (review before adding to portals.yml)\n`,
    ...clean.map(s => `- **${s.name}** — ${s.careers_url}${s.provider ? ` (provider: ${s.provider})` : ''}`),
    '',
  ].join('\n');
  if (!existsSync(SUGGESTIONS_PATH)) {
    writeFileSync(SUGGESTIONS_PATH,
      '# Portals Suggestions\n\nNew companies surfaced by Deep Scan. Review, then add the good ones to `portals.yml`.\n', 'utf-8');
  }
  appendFileSync(SUGGESTIONS_PATH, block, 'utf-8');
  return clean.length;
}

/**
 * @param {{ onLog?: (line: string) => void, dryRun?: boolean }} [opts]
 * @returns {Promise<{ added: number, dedupSkipped: number, suggestions: number, agents: object[] }>}
 */
export async function runDeepScan({ onLog = () => {}, dryRun = false } = {}) {
  onLog('Deep Scan — Claude + Codex discovering new companies/boards (this spends tokens)…');

  const results = await Promise.all(AGENTS.map(a => runAgent(a, onLog)));

  // Write each agent's postings through the shared sink with its own portal
  // label, sequentially so dedup is race-free.
  let added = 0;
  let dedupSkipped = 0;
  for (const { agent, postings } of results) {
    const tagged = postings.map(p => ({ ...p, portal: agent.portal }));
    const r = appendPostings(tagged, { dryRun });
    added += r.added;
    dedupSkipped += r.skipped;
  }

  const allSuggestions = results.flatMap(r => r.suggestions);
  const suggestionCount = writeSuggestions(allSuggestions, { dryRun });

  onLog(
    `Deep Scan complete — ${added} new postings, ${dedupSkipped} dup/seen, ` +
    `${suggestionCount} company suggestions${dryRun ? ' (dry run — not written)' : ` → ${SUGGESTIONS_PATH.replace(ROOT + '/', '')}`}.`
  );
  return {
    added, dedupSkipped, suggestions: suggestionCount,
    agents: results.map(r => ({ name: r.agent.name, postings: r.postings.length, suggestions: r.suggestions.length })),
  };
}

// ── CLI ─────────────────────────────────────────────────────────────
if (import.meta.url === `file://${process.argv[1]}`) {
  const dryRun = process.argv.includes('--dry-run');
  runDeepScan({ onLog: l => console.log(l), dryRun }).then(r => {
    console.log('\n' + '─'.repeat(45));
    console.log('Deep Scan result:', JSON.stringify(r, null, 2));
  });
}
