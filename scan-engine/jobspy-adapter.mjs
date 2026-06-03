#!/usr/bin/env node
/**
 * jobspy-adapter.mjs — bridge between the Python JobSpy engine and the shared sink.
 *
 * Spawns scan-engine/.venv/bin/python jobspy_search.py, parses the single JSON
 * object it prints to stdout, and writes the postings via scan-lib.appendPostings
 * (the same dedup/append path the ATS scanner uses).
 *
 *   import { runJobSpy } from './scan-engine/jobspy-adapter.mjs'
 *   const { added, skipped, total, stats, skipped: engineSkipped } = await runJobSpy({ onLog })
 *
 * Standalone:  node scan-engine/jobspy-adapter.mjs [--dry-run]
 *
 * If the venv is missing it logs a one-line hint and returns { skipped: true }
 * WITHOUT throwing, so the orchestrator's ATS scan still completes.
 */

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { appendPostings } from '../scan-lib.mjs';

const ENGINE_DIR = dirname(fileURLToPath(import.meta.url));
const PY = join(ENGINE_DIR, '.venv', 'bin', 'python');
const SCRIPT = join(ENGINE_DIR, 'jobspy_search.py');

/** Parse the engine's stdout: a single JSON object `{ postings, stats }`. */
function parseEngineOutput(stdout) {
  const trimmed = stdout.trim();
  if (!trimmed) return { postings: [], stats: {} };
  try {
    const obj = JSON.parse(trimmed);
    return { postings: Array.isArray(obj.postings) ? obj.postings : [], stats: obj.stats || {} };
  } catch {
    // Tolerant fallback: grab the last balanced {...} block in case a stray
    // line leaked onto stdout ahead of the JSON.
    const start = trimmed.indexOf('{');
    const end = trimmed.lastIndexOf('}');
    if (start !== -1 && end > start) {
      try {
        const obj = JSON.parse(trimmed.slice(start, end + 1));
        return { postings: Array.isArray(obj.postings) ? obj.postings : [], stats: obj.stats || {} };
      } catch { /* fall through */ }
    }
    return { postings: [], stats: {}, parseError: true };
  }
}

/**
 * @param {{ onLog?: (line: string) => void, dryRun?: boolean, collectOnly?: boolean }} [opts]
 *   collectOnly — scrape + return postings WITHOUT writing. Lets an orchestrator
 *   run this in parallel with the ATS scan and serialize the writes afterward.
 * @returns {Promise<{ skipped?: boolean, reason?: string, added?: number,
 *   dedupSkipped?: number, total?: number, stats?: object, postings?: Array }>}
 */
export function runJobSpy({ onLog = () => {}, dryRun = false, collectOnly = false } = {}) {
  return new Promise((resolve) => {
    if (!existsSync(PY)) {
      onLog('JobSpy engine not set up — run `sh scan-engine/setup.sh` to enable aggregator search. Skipping.');
      resolve({ skipped: true, reason: 'venv-missing' });
      return;
    }

    onLog('JobSpy: scraping Indeed/LinkedIn/Google/Remotive/… (this takes a minute)…');
    const proc = spawn(PY, [SCRIPT], { cwd: ENGINE_DIR, env: process.env });

    let stdout = '';
    proc.stdout.on('data', d => { stdout += String(d); });
    // The engine sends all diagnostics to stderr — stream them to the live log.
    proc.stderr.on('data', d => {
      String(d).split('\n').filter(Boolean).forEach(line => onLog(`  jobspy: ${line}`));
    });

    proc.on('error', err => {
      onLog(`JobSpy failed to start: ${err.message}`);
      resolve({ skipped: true, reason: err.message });
    });

    proc.on('close', code => {
      const { postings, stats, parseError } = parseEngineOutput(stdout);
      if (parseError) {
        onLog('JobSpy: could not parse engine output (no postings ingested).');
        resolve({ skipped: true, reason: 'parse-error', stats });
        return;
      }
      if (collectOnly) {
        onLog(`JobSpy: scraped ${postings.length} scored postings (write deferred to orchestrator)`);
        resolve({ total: postings.length, postings, stats });
        return;
      }
      const { added, skipped } = appendPostings(postings, { dryRun });
      onLog(
        `JobSpy: scraped ${postings.length} scored postings → ${added} new, ${skipped} dup/seen` +
        (dryRun ? ' (dry run — not written)' : '') +
        (code !== 0 ? ` [engine exit ${code}]` : '')
      );
      resolve({ added, dedupSkipped: skipped, total: postings.length, stats });
    });
  });
}

// ── CLI ─────────────────────────────────────────────────────────────
if (import.meta.url === `file://${process.argv[1]}`) {
  const dryRun = process.argv.includes('--dry-run');
  runJobSpy({ onLog: l => console.log(l), dryRun }).then(r => {
    console.log('\n' + '─'.repeat(45));
    console.log('JobSpy adapter result:', JSON.stringify(r, null, 2));
  });
}
