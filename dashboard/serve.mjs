#!/usr/bin/env node
// @ts-check
// Local dev server for the JobHunter dashboard.
// Serves the generated HTML and exposes a /api/scan endpoint so the
// in-browser "Scan Now" button can trigger node scan.mjs without a terminal.
//
// Usage: node dashboard/serve.mjs
//        (the .command file on the Desktop does this automatically)

import http from 'node:http';
import { execFile } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve as res, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { runDefaultScan } from '../scan-all.mjs';
import { runDeepScan } from '../deep-scan.mjs';

const configuredPort = Number(process.env.JOBHUNTER_PORT || 7432);
const PORT = Number.isInteger(configuredPort) && configuredPort > 0 ? configuredPort : 7432;
const SHOULD_OPEN_DASHBOARD = process.env.JOBHUNTER_NO_OPEN !== '1';
const __dir = dirname(fileURLToPath(import.meta.url));
const ROOT = res(__dir, '..');
const HTML_PATH = res(__dir, 'job-hunter.html');
const GEN_SCRIPT = res(__dir, 'generate-job-hunter.mjs');
const DASH_URL = `http://localhost:${PORT}`;

/** @type {{ status: 'idle'|'running'|'done'|'error', log: string[], kind: string }} */
let scanState = { status: 'idle', log: [], kind: '' };

/**
 * Run a scan engine, streaming its log into scanState, then regenerate the
 * dashboard. Shared by both the default and deep endpoints so they have one
 * run lock and one polling contract.
 * @param {string} kind  'default' | 'deep'
 * @param {(opts: { onLog: (line: string) => void }) => Promise<any>} runner
 */
function startRun(kind, runner) {
  scanState = { status: 'running', log: [], kind };
  const onLog = (line) => { scanState.log.push(line.endsWith('\n') ? line : line + '\n'); };
  Promise.resolve()
    .then(() => runner({ onLog }))
    .then(async () => {
      await generate();
      scanState.status = 'done';
    })
    .catch(err => {
      scanState.log.push(`\nScan failed: ${err instanceof Error ? err.message : String(err)}\n`);
      scanState.status = 'error';
    });
}

function generate() {
  return new Promise((resolve, reject) => {
    execFile('node', [GEN_SCRIPT], { cwd: ROOT }, (err, _out, stderr) => {
      if (err) reject(new Error(stderr || err.message));
      else resolve(undefined);
    });
  });
}

function setCors(/** @type {http.ServerResponse} */ r) {
  r.setHeader('Access-Control-Allow-Origin', '*');
  r.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  r.setHeader('Access-Control-Allow-Headers', 'Content-Type');
}

function sendJson(/** @type {http.ServerResponse} */ r, code, data) {
  setCors(r);
  r.writeHead(code, { 'Content-Type': 'application/json' });
  r.end(JSON.stringify(data));
}

function openDashboard() {
  if (!SHOULD_OPEN_DASHBOARD) return;
  execFile('open', [DASH_URL], err => {
    if (err) console.warn(`Could not open dashboard browser tab: ${err.message}`);
  });
}

function refreshExistingDashboard() {
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: '127.0.0.1',
      port: PORT,
      path: '/api/refresh',
      method: 'POST',
      timeout: 15000,
    }, response => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => {
        if (response.statusCode && response.statusCode >= 200 && response.statusCode < 300) {
          resolve(undefined);
          return;
        }
        reject(new Error(`refresh returned ${response.statusCode}: ${body.slice(0, 200)}`));
      });
    });
    req.on('timeout', () => req.destroy(new Error('refresh timed out')));
    req.on('error', reject);
    req.end();
  });
}

async function handleServerError(/** @type {NodeJS.ErrnoException} */ err) {
  if (err.code !== 'EADDRINUSE') {
    console.error('Dashboard server failed:', err.message);
    process.exit(1);
  }

  console.log(`\nJobHunter Dashboard is already running → ${DASH_URL}`);
  try {
    await refreshExistingDashboard();
    console.log('Refreshed the existing dashboard.');
  } catch (refreshErr) {
    const message = refreshErr instanceof Error ? refreshErr.message : String(refreshErr);
    console.warn(`Could not refresh the existing dashboard automatically: ${message}`);
  }
  openDashboard();
  process.exit(0);
}

const server = http.createServer((req, r) => {
  const url = new URL(req.url ?? '/', `http://localhost:${PORT}`);
  setCors(r);

  if (req.method === 'OPTIONS') { r.writeHead(204); r.end(); return; }

  // ── Serve dashboard ──────────────────────────────────────────────
  if (req.method === 'GET' && url.pathname === '/') {
    try {
      const html = readFileSync(HTML_PATH, 'utf8');
      r.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      r.end(html);
    } catch {
      r.writeHead(503, { 'Content-Type': 'text/plain' });
      r.end('Generating dashboard… refresh in a moment.');
    }
    return;
  }

  // ── Start default scan (ATS portals + JobSpy aggregators — free) ──
  if (req.method === 'POST' && url.pathname === '/api/scan') {
    if (scanState.status === 'running') {
      sendJson(r, 409, { error: 'Scan already running' });
      return;
    }
    startRun('default', ({ onLog }) => runDefaultScan({ onLog }));
    sendJson(r, 200, { ok: true });
    return;
  }

  // ── Start deep scan (Claude + Codex discovery — spends tokens) ────
  if (req.method === 'POST' && url.pathname === '/api/scan/deep') {
    if (scanState.status === 'running') {
      sendJson(r, 409, { error: 'Scan already running' });
      return;
    }
    startRun('deep', ({ onLog }) => runDeepScan({ onLog }));
    sendJson(r, 200, { ok: true });
    return;
  }

  // ── Poll scan status (shared by both scan kinds) ─────────────────
  if (req.method === 'GET' && url.pathname === '/api/scan/status') {
    sendJson(r, 200, scanState);
    return;
  }

  // ── Regenerate dashboard ─────────────────────────────────────────
  if (req.method === 'POST' && url.pathname === '/api/refresh') {
    generate()
      .then(() => sendJson(r, 200, { ok: true }))
      .catch(e => sendJson(r, 500, { error: e instanceof Error ? e.message : String(e) }));
    return;
  }

  r.writeHead(404); r.end('Not found');
});

process.on('SIGINT', () => { console.log('\nServer stopped.'); process.exit(0); });

server.on('error', err => { void handleServerError(err); });

server.listen(PORT, '127.0.0.1', () => {
  generate()
    .then(() => {
      console.log(`\nJobHunter Dashboard → ${DASH_URL}`);
      console.log('Press Ctrl+C to stop.\n');
      openDashboard();
    })
    .catch(err => {
    console.error('Failed to generate dashboard:', err.message);
      server.close(() => process.exit(1));
    });
});
