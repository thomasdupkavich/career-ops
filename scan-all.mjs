#!/usr/bin/env node
/**
 * scan-all.mjs — default scan orchestrator.
 *
 * Runs the two FREE, zero-token engines and funnels both into the shared sink:
 *   1. scan.mjs       — ATS portal scanner (Greenhouse/Ashby/Lever/…)
 *   2. JobSpy adapter — Indeed/LinkedIn/Google/Remotive aggregators
 *
 * Both scrapes run in PARALLEL (logs stream live), but the file writes are
 * serialized: the ATS scan writes its own rows, then we append JobSpy's
 * postings deduped against everything the ATS scan just added — so two engines
 * never race on pipeline.md / scan-history.tsv.
 *
 *   import { runDefaultScan } from './scan-all.mjs'
 *   const result = await runDefaultScan({ onLog })
 *
 * Standalone:  node scan-all.mjs [--dry-run]
 */

import { spawn } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { runJobSpy } from './scan-engine/jobspy-adapter.mjs';
import { appendPostings } from './scan-lib.mjs';

const ROOT = dirname(fileURLToPath(import.meta.url));

/** Spawn `node scan.mjs` (ATS), streaming output and returning its summary. */
function runAts({ onLog = () => {}, dryRun = false } = {}) {
  return new Promise((resolve) => {
    onLog('ATS: scanning company portals (Greenhouse/Ashby/Lever/…)…');
    const args = ['scan.mjs'];
    if (dryRun) args.push('--dry-run');
    const proc = spawn('node', args, { cwd: ROOT, env: process.env });

    let stdout = '';
    proc.stdout.on('data', d => {
      stdout += String(d);
      String(d).split('\n').filter(Boolean).forEach(line => onLog(`  ats: ${line}`));
    });
    proc.stderr.on('data', d => {
      String(d).split('\n').filter(Boolean).forEach(line => onLog(`  ats: ${line}`));
    });
    proc.on('error', err => {
      onLog(`ATS scan failed to start: ${err.message}`);
      resolve({ skipped: true, reason: err.message, added: 0 });
    });
    proc.on('close', code => {
      const m = stdout.match(/New offers added:\s+(\d+)/);
      const added = m ? Number(m[1]) : 0;
      resolve({ added, exitCode: code });
    });
  });
}

/**
 * @param {{ onLog?: (line: string) => void, dryRun?: boolean }} [opts]
 * @returns {Promise<{ ats: object, jobspy: object }>}
 */
export async function runDefaultScan({ onLog = () => {}, dryRun = false } = {}) {
  onLog('Default scan — ATS portals + JobSpy aggregators (free, no tokens).');

  // Parallel scrapes; JobSpy collects only (no write) so the ATS scan owns the
  // first write and JobSpy's append dedups against it afterward.
  const [ats, jobspy] = await Promise.all([
    runAts({ onLog, dryRun }),
    runJobSpy({ onLog, dryRun, collectOnly: true }),
  ]);

  let jobspyResult = { added: 0, dedupSkipped: 0, total: 0 };
  if (jobspy && !jobspy.skipped && Array.isArray(jobspy.postings)) {
    const { added, skipped } = appendPostings(jobspy.postings, { dryRun });
    jobspyResult = { added, dedupSkipped: skipped, total: jobspy.postings.length };
    onLog(
      `JobSpy: ${added} new, ${skipped} dup/seen` +
      (dryRun ? ' (dry run — not written)' : ' written')
    );
  } else if (jobspy?.skipped) {
    jobspyResult = { added: 0, dedupSkipped: 0, total: 0, skipped: true, reason: jobspy.reason };
  }

  const totalAdded = (ats.added || 0) + (jobspyResult.added || 0);
  onLog(`Scan complete — ${totalAdded} new postings (ATS ${ats.added || 0}, JobSpy ${jobspyResult.added || 0}).`);
  return { ats, jobspy: jobspyResult, totalAdded };
}

// ── CLI ─────────────────────────────────────────────────────────────
if (import.meta.url === `file://${process.argv[1]}`) {
  const dryRun = process.argv.includes('--dry-run');
  runDefaultScan({ onLog: l => console.log(l), dryRun }).then(r => {
    console.log('\n' + '─'.repeat(45));
    console.log('Default scan result:', JSON.stringify(r, null, 2));
  });
}
