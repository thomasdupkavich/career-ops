/**
 * scan-lib.mjs — Shared write/dedup sink for every scan engine.
 *
 * Extracted verbatim from scan.mjs so the ATS scanner, the JobSpy adapter,
 * and the Deep Scan agents all funnel postings through ONE dedup + append
 * path into data/pipeline.md + data/scan-history.tsv.
 *
 * Paths resolve from this file's directory (the project root) so the writers
 * work no matter what cwd the caller runs from.
 */

import { readFileSync, writeFileSync, appendFileSync, existsSync, mkdirSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

const ROOT = path.dirname(fileURLToPath(import.meta.url));

export const SCAN_HISTORY_PATH = path.join(ROOT, 'data/scan-history.tsv');
export const PIPELINE_PATH = path.join(ROOT, 'data/pipeline.md');
export const APPLICATIONS_PATH = path.join(ROOT, 'data/applications.md');

export function today() {
  return new Date().toISOString().slice(0, 10);
}

function ensureDataDir() {
  mkdirSync(path.join(ROOT, 'data'), { recursive: true });
}

// ── Dedup ───────────────────────────────────────────────────────────

export function loadSeenUrls() {
  const seen = new Set();

  // scan-history.tsv
  if (existsSync(SCAN_HISTORY_PATH)) {
    const lines = readFileSync(SCAN_HISTORY_PATH, 'utf-8').split('\n');
    for (const line of lines.slice(1)) { // skip header
      const url = line.split('\t')[0];
      if (url) seen.add(url);
    }
  }

  // pipeline.md — extract URLs from checkbox lines
  if (existsSync(PIPELINE_PATH)) {
    const text = readFileSync(PIPELINE_PATH, 'utf-8');
    for (const match of text.matchAll(/- \[[ x]\] (https?:\/\/\S+)/g)) {
      seen.add(match[1]);
    }
  }

  // applications.md — extract URLs from report links and any inline URLs
  if (existsSync(APPLICATIONS_PATH)) {
    const text = readFileSync(APPLICATIONS_PATH, 'utf-8');
    for (const match of text.matchAll(/https?:\/\/[^\s|)]+/g)) {
      seen.add(match[0]);
    }
  }

  return seen;
}

export function loadSeenCompanyRoles() {
  const seen = new Set();
  if (existsSync(APPLICATIONS_PATH)) {
    const text = readFileSync(APPLICATIONS_PATH, 'utf-8');
    // Parse markdown table rows: | # | Date | Company | Role | ...
    for (const match of text.matchAll(/\|[^|]+\|[^|]+\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|/g)) {
      const company = match[1].trim().toLowerCase();
      const role = match[2].trim().toLowerCase();
      if (company && role && company !== 'company') {
        seen.add(`${company}::${role}`);
      }
    }
  }
  return seen;
}

// ── Pipeline writer ─────────────────────────────────────────────────

export function appendToPipeline(offers) {
  if (offers.length === 0) return;
  ensureDataDir();

  // pipeline.md is a user-layer file that should already exist; if a fresh
  // setup is missing it, seed a minimal skeleton so the writer never throws.
  let text = existsSync(PIPELINE_PATH)
    ? readFileSync(PIPELINE_PATH, 'utf-8')
    : '# Pipeline\n\n## Pendientes\n\n## Procesadas\n';

  // Find "## Pendientes" section and append after it
  const marker = '## Pendientes';
  const idx = text.indexOf(marker);
  if (idx === -1) {
    // No Pendientes section — append at end before Procesadas
    const procIdx = text.indexOf('## Procesadas');
    const insertAt = procIdx === -1 ? text.length : procIdx;
    const block = `\n${marker}\n\n` + offers.map(o =>
      `- [ ] ${o.url} | ${o.company} | ${o.title}`
    ).join('\n') + '\n\n';
    text = text.slice(0, insertAt) + block + text.slice(insertAt);
  } else {
    // Find the end of existing Pendientes content (next ## or end)
    const afterMarker = idx + marker.length;
    const nextSection = text.indexOf('\n## ', afterMarker);
    const insertAt = nextSection === -1 ? text.length : nextSection;

    const block = '\n' + offers.map(o =>
      `- [ ] ${o.url} | ${o.company} | ${o.title}`
    ).join('\n') + '\n';
    text = text.slice(0, insertAt) + block + text.slice(insertAt);
  }

  writeFileSync(PIPELINE_PATH, text, 'utf-8');
}

export function appendToScanHistory(offers, date) {
  // Ensure file + header exist. Location appended as 7th column for non-breaking
  // backward compat — older scan-history.tsv files with 6 columns still parse fine
  // since loadSeenUrls only reads column 0.
  ensureDataDir();
  if (!existsSync(SCAN_HISTORY_PATH)) {
    writeFileSync(SCAN_HISTORY_PATH, 'url\tfirst_seen\tportal\ttitle\tcompany\tstatus\tlocation\n', 'utf-8');
  }

  const lines = offers.map(o =>
    `${o.url}\t${date}\t${o.source}\t${o.title}\t${o.company}\tadded\t${o.location || ''}`
  ).join('\n') + '\n';

  appendFileSync(SCAN_HISTORY_PATH, lines, 'utf-8');
}

// ── High-level shared sink ──────────────────────────────────────────

/**
 * appendPostings — the single entry point new engines use.
 *
 * @param {Array<{url, company, title, portal, location}>} postings
 * @param {{ dryRun?: boolean, date?: string }} [opts]
 * @returns {{ added: number, skipped: number, addedItems: Array }}
 *
 * Re-loads the dedup sets from disk on every call, so when an orchestrator
 * runs several engines and calls appendPostings sequentially, each call sees
 * what the previous one just wrote — no cross-engine duplicates.
 */
export function appendPostings(postings, { dryRun = false, date = today() } = {}) {
  const seenUrls = loadSeenUrls();
  const seenCompanyRoles = loadSeenCompanyRoles();

  const kept = [];
  let skipped = 0;

  for (const p of Array.isArray(postings) ? postings : []) {
    const url = (p?.url || '').trim();
    const company = (p?.company || '').trim();
    const title = (p?.title || '').trim();
    const location = (p?.location || '').trim();
    const portal = (p?.portal || 'unknown').trim();

    if (!url || !company || !title) { skipped++; continue; }
    if (seenUrls.has(url)) { skipped++; continue; }
    const key = `${company.toLowerCase()}::${title.toLowerCase()}`;
    if (seenCompanyRoles.has(key)) { skipped++; continue; }

    // Mark seen intra-batch so duplicates within one call don't slip through.
    seenUrls.add(url);
    seenCompanyRoles.add(key);
    kept.push({ url, company, title, location, source: portal });
  }

  if (!dryRun && kept.length > 0) {
    appendToPipeline(kept);
    appendToScanHistory(kept, date);
  }

  return { added: kept.length, skipped, addedItems: kept };
}
