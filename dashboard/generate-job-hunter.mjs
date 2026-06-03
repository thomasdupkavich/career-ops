#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import yaml from "js-yaml";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const homeDir = os.homedir();

const args = new Map();
for (let i = 2; i < process.argv.length; i += 1) {
  const arg = process.argv[i];
  if (!arg.startsWith("--")) continue;
  const key = arg.slice(2);
  const value = process.argv[i + 1] && !process.argv[i + 1].startsWith("--")
    ? process.argv[++i]
    : "true";
  args.set(key, value);
}

const hermesHome = path.resolve(
  args.get("hermes-home") ||
    process.env.HERMES_HOME ||
    path.join(homeDir, ".hermes"),
);
const jobSearchDir = path.resolve(
  args.get("job-search-dir") ||
    process.env.HERMES_JOB_SEARCH_DIR ||
    path.join(hermesHome, "job-search"),
);
const openclawHome = path.resolve(
  args.get("openclaw-home") ||
    process.env.OPENCLAW_HOME ||
    path.join(homeDir, ".openclaw"),
);
const openclawJobSearchDir = path.resolve(
  args.get("openclaw-job-search-dir") ||
    process.env.OPENCLAW_JOB_SEARCH_DIR ||
    path.join(openclawHome, "workspace", "job_search"),
);
const projectRoot = path.resolve(
  args.get("project-root") ||
    process.env.CAREER_OPS_ROOT ||
    path.resolve(scriptDir, ".."),
);
const outputPath = path.resolve(
  args.get("out") || path.join(scriptDir, "job-hunter.html"),
);

function readText(filePath, fallback = "") {
  try {
    return fs.readFileSync(filePath, "utf8");
  } catch {
    return fallback;
  }
}

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

function readJsonl(filePath) {
  return readText(filePath)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean);
}

function listFiles(dirPath, predicate = () => true) {
  try {
    return fs.readdirSync(dirPath, { withFileTypes: true })
      .filter((entry) => entry.isFile())
      .map((entry) => path.join(dirPath, entry.name))
      .filter(predicate);
  } catch {
    return [];
  }
}

function splitMarkdownRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((field) => field.trim());
}

function parseApplications() {
  const candidates = [
    path.join(projectRoot, "applications.md"),
    path.join(projectRoot, "data", "applications.md"),
  ];
  const filePath = candidates.find((candidate) => fs.existsSync(candidate));
  if (!filePath) return [];

  return readText(filePath)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.startsWith("|") && !line.startsWith("|---") && !line.startsWith("| #"))
    .map(splitMarkdownRow)
    .filter((fields) => fields.length >= 8)
    .map((fields, index) => ({
      number: Number.parseInt(fields[0], 10) || index + 1,
      date: fields[1],
      company: fields[2],
      role: fields[3],
      score: fields[4],
      numericScore: Number.parseFloat(String(fields[4]).replace("/5", "")) || 0,
      status: fields[5],
      hasPdf: /check|yes|\u2705/i.test(fields[6]),
      report: fields[7],
      notes: fields[8] || "",
    }));
}

function parsePipeline() {
  const filePath = path.join(projectRoot, "data", "pipeline.md");
  return readText(filePath)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => /^- \[[ xX]\]/.test(line))
    .map((line) => {
      const done = /^- \[[xX]\]/.test(line);
      const body = line.replace(/^- \[[ xX]\]\s*/, "");
      const [url = "", company = "Unknown", role = "Untitled"] = body
        .split("|")
        .map((part) => part.trim());
      return { done, url, company, role };
    });
}

function parseScanHistory() {
  const filePath = path.join(projectRoot, "data", "scan-history.tsv");
  const lines = readText(filePath)
    .split(/\r?\n/)
    .filter(Boolean);
  if (lines.length < 2) return [];

  const headers = lines[0].split("\t");
  return lines.slice(1).map((line) => {
    const fields = line.split("\t");
    return Object.fromEntries(headers.map((header, index) => [header, fields[index] || ""]));
  });
}

function parseProviderInventory() {
  return listFiles(
    path.join(projectRoot, "providers"),
    (filePath) => filePath.endsWith(".mjs") && !path.basename(filePath).startsWith("_"),
  )
    .map((filePath) => ({
      id: path.basename(filePath, ".mjs"),
      file: path.relative(projectRoot, filePath),
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
}

function classifyScore(score) {
  if (score >= 80) return "elite";
  if (score >= 70) return "strong";
  if (score >= 55) return "review";
  return "weak";
}

function cleanSnippet(value) {
  return String(value || "")
    .replace(/\\([*_`~\-[\]()])/g, "$1")
    .replace(/\*\*/g, "")
    .replace(/[_`~]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeJob(job, index, queueByKey, sourceSystem = "Hermes") {
  const key = [
    job.company || "",
    job.title || "",
    job.url || "",
  ].join("|").toLowerCase();
  const queued = queueByKey.get(key);
  const score = Number(job.score || 0);
  return {
    id: job.id || job.job_key || job.url || `job-${index + 1}`,
    number: index + 1,
    title: job.title || "Untitled role",
    company: job.company || "Unknown company",
    location: job.location || "Location not listed",
    url: job.url || "",
    source: job.source || "unknown",
    sourceSystem,
    score,
    priority: classifyScore(score),
    salary: job.salary || "Unlisted",
    posted: job.posted || "",
    category: job.category || job.query_kind || "",
    interviewChance: job.interview_chance || "",
    tags: Array.isArray(job.keywords_matched) ? job.keywords_matched.slice(0, 8) : [],
    snippet: cleanSnippet(job.snippet).slice(0, 260),
    queueStatus: queued?.status || "",
    submitted: Boolean(queued?.submitted),
    prepared: Boolean(queued?.cover_letter_path),
    notes: queued?.notes || "",
  };
}

function flattenJobPayload(payload) {
  if (Array.isArray(payload.jobs) && payload.jobs.length) return payload.jobs;
  if (!payload.sections || typeof payload.sections !== "object") return [];
  return Object.values(payload.sections)
    .flatMap((section) => Array.isArray(section) ? section : [])
    .filter(Boolean);
}

function normalizeQueueItem(item, index) {
  return {
    id: item.job_key || item.url || `queue-${index + 1}`,
    number: item.n || index + 1,
    title: item.title || "Untitled role",
    company: item.company || "Unknown company",
    location: item.location || "",
    url: item.url || "",
    source: item.source || "",
    score: Number(item.score || 0),
    status: item.submitted ? "submitted" : item.status || "queued",
    submitted: Boolean(item.submitted),
    approved: Boolean(item.submit_approved),
    salary: item.salary || "Unlisted",
    coverLetterPath: item.cover_letter_path || "",
    updatedAt: item.updated_at || item.created_at || "",
    notes: item.notes || item.instruction || "",
    evidencePaths: Array.isArray(item.evidence_paths) ? item.evidence_paths : [],
  };
}

function normalizeCareerOpsLead(item, index, sourceLabel) {
  const source = item.portal || item.source || sourceLabel || "career-ops";
  const status = item.status || (sourceLabel === "pipeline" ? "pending" : "added");
  const firstSeen = item.first_seen || item.date || "";
  return {
    id: `career-ops:${item.url || `${item.company || ""}:${item.title || item.role || ""}:${index + 1}`}`,
    number: index + 1,
    title: item.title || item.role || "Untitled role",
    company: item.company || "Unknown company",
    location: item.location || "",
    url: item.url || "",
    source,
    sourceSystem: "Career-Ops",
    score: "",
    priority: "review",
    salary: "Unlisted",
    posted: firstSeen,
    category: sourceLabel || "provider scan",
    interviewChance: "",
    tags: [source, status].filter(Boolean).slice(0, 8),
    snippet: cleanSnippet([
      sourceLabel === "pipeline" ? "Pending career-ops pipeline entry" : "Career-ops provider scan result",
      firstSeen ? `first seen ${firstSeen}` : "",
    ].filter(Boolean).join(" | ")).slice(0, 260),
    queueStatus: status,
    submitted: false,
    prepared: false,
    notes: item.notes || "",
  };
}

function parseCareerOpsLeads(scanHistory, pipeline) {
  const byUrl = new Map();

  for (const row of scanHistory) {
    const lead = normalizeCareerOpsLead(row, byUrl.size, "provider scan");
    const key = String(lead.url || lead.id).toLowerCase();
    if (!key || byUrl.has(key)) continue;
    byUrl.set(key, lead);
  }

  for (const item of pipeline.filter((entry) => !entry.done)) {
    const lead = normalizeCareerOpsLead(item, byUrl.size, "pipeline");
    const key = String(lead.url || lead.id).toLowerCase();
    if (!key || byUrl.has(key)) continue;
    byUrl.set(key, lead);
  }

  return [...byUrl.values()].map((lead, index) => ({
    ...lead,
    number: index + 1,
  }));
}

function countBy(items, getKey) {
  const counts = new Map();
  for (const item of items) {
    const key = getKey(item);
    if (!key) continue;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([name, count]) => ({ name, count }));
}

function mergeCounts(groups) {
  const counts = new Map();
  for (const group of groups) {
    for (const item of group) {
      if (!item.name) continue;
      counts.set(item.name, (counts.get(item.name) || 0) + Number(item.count || 0));
    }
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([name, count]) => ({ name, count }));
}

function msToIso(value) {
  const numeric = Number(value);
  if (!numeric) return "";
  const date = new Date(numeric);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

function normalizeSchedule(schedule, fallback = "") {
  if (!schedule) return fallback;
  if (typeof schedule === "string") return schedule;
  if (schedule.display) return schedule.display;
  if (schedule.expr) return schedule.tz ? `${schedule.expr} ${schedule.tz}` : schedule.expr;
  if (schedule.kind === "at" && schedule.at) return `at ${schedule.at}`;
  return fallback;
}

function normalizeCronJob(job, sourceSystem, state = {}) {
  const lastStatus = state.lastStatus || state.lastRunStatus || job.last_status || "";
  const lastError = state.lastError || state.lastDiagnosticSummary || job.last_error || "";
  return {
    id: `${sourceSystem.toLowerCase()}:${job.id || job.name || job.script || "job"}`,
    sourceSystem,
    name: job.name || job.script || "Unnamed job",
    script: job.script || job.payload?.kind || "",
    enabled: Boolean(job.enabled),
    state: job.state || "",
    schedule: job.schedule_display || normalizeSchedule(job.schedule),
    lastRunAt: job.last_run_at || msToIso(state.lastRunAtMs),
    nextRunAt: job.next_run_at || msToIso(state.nextRunAtMs),
    lastStatus,
    lastError,
    deliver: job.deliver || job.delivery?.mode || "",
    noAgent: Boolean(job.no_agent),
  };
}

function parseHermesCronJobs(cronPayload) {
  return Array.isArray(cronPayload.jobs)
    ? cronPayload.jobs
        .filter((job) => /job|apply|career|operator/i.test([
          job.name,
          job.script,
        ].filter(Boolean).join(" ")))
        .map((job) => normalizeCronJob(job, "Hermes"))
    : [];
}

function parseOpenClawCronJobs(openclawRoot) {
  const payload = readJson(path.join(openclawRoot, "cron", "jobs.json"), { jobs: [] });
  const statePayload = readJson(path.join(openclawRoot, "cron", "jobs-state.json"), { jobs: {} });
  return Array.isArray(payload.jobs)
    ? payload.jobs
        .filter((job) => /job|apply|career|software|resume/i.test([
          job.name,
          job.description,
          job.payload?.message,
        ].filter(Boolean).join(" ")))
        .map((job) => normalizeCronJob(job, "OpenClaw", statePayload.jobs?.[job.id]?.state || {}))
    : [];
}

function extractScore(value) {
  const match = String(value || "").match(/(\d{2,3})(?:\/100)?/);
  return match ? Number(match[1]) : 0;
}

function parseOpenClawSummaryCounts(summary = "") {
  const plain = String(summary || "").replace(/\*\*/g, "");
  const checked = plain.match(/Checked:\s*([\d,]+)\s+raw postings\s*[·-]\s*([\d,]+)\s+viable scored\s*[·-]\s*([\d,]+)\s+shown/i);
  if (checked) {
    return {
      raw: Number(checked[1].replace(/,/g, "")),
      viable: Number(checked[2].replace(/,/g, "")),
      shown: Number(checked[3].replace(/,/g, "")),
    };
  }

  const sourceStats = plain.match(/Source stats:\s*([\d,]+)\s+raw postings checked,\s*([\d,]+)\s+passed.*?([\d,]+)\s+worth surfacing/i);
  if (sourceStats) {
    return {
      raw: Number(sourceStats[1].replace(/,/g, "")),
      viable: Number(sourceStats[2].replace(/,/g, "")),
      shown: Number(sourceStats[3].replace(/,/g, "")),
    };
  }

  const runCounts = plain.match(/Run counts:\s*([\d,]+)\s+raw postings,\s*([\d,]+)\s+viable scored.*?([\d,]+)\s+shown/i);
  if (runCounts) {
    return {
      raw: Number(runCounts[1].replace(/,/g, "")),
      viable: Number(runCounts[2].replace(/,/g, "")),
      shown: Number(runCounts[3].replace(/,/g, "")),
    };
  }

  return { raw: 0, viable: 0, shown: 0 };
}

function cleanMarkdown(value) {
  return cleanSnippet(value)
    .replace(/^#+\s*/, "")
    .replace(/^[*-]\s*/, "")
    .trim();
}

function parseOpenClawLeadLine(line) {
  const cleaned = cleanMarkdown(line);
  if (!/^\d+\./.test(cleaned)) return null;
  const body = cleaned.replace(/^\d+\.\s*/, "");

  let match = body.match(/^\*\*([^*]+)\*\*\s+[—-]\s+(.+?)(?:\s+[—-]\s+(\d{2,3})(?:\/100)?)?$/);
  if (match) {
    return {
      company: cleanMarkdown(match[1]),
      title: cleanMarkdown(match[2]),
      score: extractScore(match[3]),
    };
  }

  match = body.match(/^(.+?)\s+[—-]\s+(.+?)\s+[—-]\s+(\d{2,3})(?:\/100)?$/);
  if (match) {
    return {
      title: cleanMarkdown(match[1]),
      company: cleanMarkdown(match[2]),
      score: extractScore(match[3]),
    };
  }

  match = body.match(/^(.+?),\s+(.+?)\s+[—-]\s+(\d{2,3})(?:\/100)?$/);
  if (match) {
    return {
      title: cleanMarkdown(match[1]),
      company: cleanMarkdown(match[2]),
      score: extractScore(match[3]),
    };
  }

  match = body.match(/^(.+?)\s+[—-]\s+(.+)$/);
  if (match) {
    return {
      company: cleanMarkdown(match[1]),
      title: cleanMarkdown(match[2]),
      score: 0,
    };
  }

  return null;
}

function parseOpenClawSummaryJobs(summary, run) {
  const jobs = [];
  let current = null;
  let section = "";

  function pushCurrent() {
    if (!current) return;
    current.id = `openclaw-${run.runId || run.jobId || run.ts}-${current.number}`;
    current.sourceSystem = "OpenClaw";
    current.source = current.source || "openclaw cron";
    current.category = current.category || section;
    current.priority = classifyScore(current.score);
    current.snippet = cleanSnippet(current.snippet).slice(0, 260);
    jobs.push(current);
    current = null;
  }

  for (const rawLine of String(summary || "").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;

    if (/^\*\*.*(Long Island|LI|Hybrid|Local|Remote|Remote-Friendly|Remote-First).*?\*\*/i.test(line)) {
      section = cleanMarkdown(line).replace(/^Fully\s+/i, "");
      continue;
    }

    const lead = parseOpenClawLeadLine(line);
    if (lead) {
      pushCurrent();
      current = {
        number: Number(line.match(/^(\d+)\./)?.[1] || jobs.length + 1),
        title: lead.title || "Untitled role",
        company: lead.company || "Unknown company",
        location: "",
        url: "",
        score: lead.score || 0,
        salary: "Unlisted",
        posted: "",
        tags: [],
        snippet: "",
        queueStatus: "",
        submitted: false,
        prepared: false,
        notes: "",
        runAt: msToIso(run.runAtMs || run.ts),
      };
      continue;
    }

    if (!current) continue;

    const scoreLine = line.match(/⭐\s*(\d{2,3})(?:\/100)?(?:\s*·\s*💰\s*([^·]+))?(?:\s*·\s*📍\s*(.+))?/);
    if (scoreLine) {
      current.score = Number(scoreLine[1]);
      if (scoreLine[2]) current.salary = cleanMarkdown(scoreLine[2]);
      if (scoreLine[3]) current.location = cleanMarkdown(scoreLine[3]);
      continue;
    }

    const detailLine = line.match(/^(?:Salary:\s*)?([^|]+)\s*\|\s*(?:Source:\s*)?([^|]+)\s*\|\s*(?:Posted:?\s*)?(.+)$/i);
    if (detailLine && /\$|salary|unknown|not listed|remote|ny|source|posted/i.test(line)) {
      const first = cleanMarkdown(detailLine[1]);
      const second = cleanMarkdown(detailLine[2]);
      const third = cleanMarkdown(detailLine[3]);
      if (/\$|unknown|not listed|salary/i.test(first)) current.salary = first.replace(/^Salary:\s*/i, "");
      else if (!current.location) current.location = first;
      if (/remote|ny|us|hybrid|local|island|hauppauge|edgewood|melville|holtsville/i.test(second)) current.location = second;
      else current.source = second.replace(/^Source:\s*/i, "");
      if (/posted/i.test(third)) current.posted = third.replace(/^Posted:?\s*/i, "");
      else if (!current.source || current.source === "openclaw cron") current.source = third.replace(/^Source:\s*/i, "");
      continue;
    }

    const matchLine = line.match(/^Match:\s*(.+)$/i);
    if (matchLine) {
      current.tags = matchLine[1].split(",").map((tag) => cleanMarkdown(tag)).filter(Boolean).slice(0, 8);
      continue;
    }

    const urlLine = line.match(/^(?:🔗|URL:)?\s*(https?:\/\/\S+)/i);
    if (urlLine) {
      current.url = urlLine[1];
      continue;
    }

    if (!current.snippet && !/^(\*\*|Source stats|Resume note|Profile note)/i.test(line)) {
      current.snippet = cleanMarkdown(line);
    }
  }
  pushCurrent();
  return jobs;
}

function parseOpenClawRuns(openclawRoot) {
  const runDir = path.join(openclawRoot, "cron", "runs");
  const entries = listFiles(runDir, (filePath) => filePath.endsWith(".jsonl"))
    .flatMap(readJsonl)
    .filter((entry) => /job|apply|career|software|resume/i.test([
      entry.jobId,
      entry.summary,
      entry.error,
    ].filter(Boolean).join(" ")))
    .sort((a, b) => Number(b.ts || b.runAtMs || 0) - Number(a.ts || a.runAtMs || 0));

  const latestSuccessful = entries.find((entry) => entry.status === "ok" && entry.summary);
  const leads = latestSuccessful ? parseOpenClawSummaryJobs(latestSuccessful.summary, latestSuccessful) : [];
  const runs = entries.slice(0, 8).map((entry) => ({
    id: entry.runId || `${entry.jobId || "openclaw"}:${entry.ts || entry.runAtMs || ""}`,
    status: entry.status || "",
    jobId: entry.jobId || "",
    runAt: msToIso(entry.runAtMs || entry.ts),
    durationMs: Number(entry.durationMs || 0),
    deliveryStatus: entry.deliveryStatus || "",
    error: entry.error || "",
    counts: parseOpenClawSummaryCounts(entry.summary || ""),
    summaryPreview: cleanSnippet(entry.summary || entry.error || "").slice(0, 220),
  }));

  return {
    latestSuccessfulRunAt: latestSuccessful ? msToIso(latestSuccessful.runAtMs || latestSuccessful.ts) : "",
    leads,
    runs,
  };
}

function readYaml(filePath, fallback) {
  try {
    return yaml.load(fs.readFileSync(filePath, "utf8")) || fallback;
  } catch {
    return fallback;
  }
}

function buildSnapshot() {
  // Career-ops is the single source of truth. Every engine — ATS (scan.mjs),
  // JobSpy (scan-engine), and Deep Scan (claude/codex) — writes into
  // data/scan-history.tsv + data/pipeline.md, so the leads parsed here already
  // include all of them. No Hermes or OpenClaw paths are read.
  const applications = parseApplications();
  const pipeline = parsePipeline();
  const scanHistory = parseScanHistory();
  const providerInventory = parseProviderInventory();
  const careerOpsLeadsRaw = parseCareerOpsLeads(scanHistory, pipeline);
  const jobs = careerOpsLeadsRaw.map((job, index) => ({ ...job, number: index + 1 }));

  const profileYml = readYaml(path.join(projectRoot, "config", "profile.yml"), {});
  const candidate = profileYml.candidate || {};
  const targetRoles = (profileYml.target_roles && profileYml.target_roles.primary) || [];
  const comp = profileYml.compensation || profileYml.salary || {};
  const locPolicy = profileYml.location_policy || profileYml.location || {};

  const scannedPortals = countBy(scanHistory, (item) => item.portal || item.source);
  const sources = mergeCounts([scannedPortals]);

  return {
    generatedAt: new Date().toISOString(),
    paths: { projectRoot },
    user: {
      name: candidate.full_name || candidate.name || "Thomas Dupkavich",
      title: targetRoles[0] || "Software Engineer",
      location: candidate.location || "",
      remotePreference: (typeof locPolicy === "string" ? locPolicy : locPolicy.summary) || "",
      salaryMin: comp.target_min || comp.minimum || comp.min || "",
      yearsExperience: candidate.years_experience || "",
    },
    stats: {
      totalLeads: jobs.length,
      careerOpsLeads: jobs.length,
      scanHistoryCount: scanHistory.length,
      pendingUrls: pipeline.filter((item) => !item.done).length,
      trackedApplications: applications.length,
      activeApplications: applications.filter((item) => !/reject|discard|skip|closed/i.test(item.status)).length,
      providerAdapters: providerInventory.length,
    },
    jobs,
    applications,
    pipeline,
    scanHistory,
    sourceBreakdown: sources,
    scannedPortals,
    providerInventory,
  };
}

function safeJson(value) {
  return JSON.stringify(value).replace(/[<>&]/g, (char) => ({
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
  })[char]);
}

function renderHtml(snapshot) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JobHunter Career Command Center</title>
<style>
@font-face {
  font-family: "DM Sans Local";
  src: url("../fonts/dm-sans-latin.woff2") format("woff2");
  font-weight: 100 900;
}
* { box-sizing: border-box; }
:root {
  --bg: #080c10;
  --surface: #101820;
  --surface-2: #16212a;
  --surface-3: #1d2a34;
  --border: #263844;
  --border-soft: #1d2b34;
  --text: #edf5f7;
  --text-dim: #b8c7cf;
  --text-faint: #7c8f99;
  --accent: #3ddbd9;
  --accent-dim: rgba(61, 219, 217, 0.12);
  --green: #59d487;
  --amber: #e6b450;
  --rose: #ff6b7a;
  --violet: #a78bfa;
  --radius: 8px;
  --font: "DM Sans Local", "DM Sans", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
html, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--text); font-family: var(--font); letter-spacing: 0; }
body::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  background-image:
    linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
  background-size: 38px 38px;
  opacity: .35;
}
a { color: inherit; }
button, input, select { font: inherit; }
.shell { min-height: 100vh; display: grid; grid-template-columns: 228px minmax(0, 1fr); position: relative; z-index: 1; }
.sidebar { background: rgba(16, 24, 32, .96); border-right: 1px solid var(--border); padding: 16px 10px; display: flex; flex-direction: column; gap: 16px; position: sticky; top: 0; height: 100vh; }
.brand { display: flex; align-items: center; gap: 10px; padding: 2px 6px 14px; border-bottom: 1px solid var(--border-soft); }
.mark { width: 32px; height: 32px; border: 1px solid rgba(61,219,217,.28); border-radius: 8px; display: grid; place-items: center; color: #061113; background: var(--accent); font-weight: 900; }
.brand-name { font-size: 15px; font-weight: 700; }
.brand-name span { color: var(--accent); }
.nav { display: grid; gap: 4px; }
.nav button { display: flex; justify-content: space-between; align-items: center; gap: 8px; width: 100%; padding: 9px 10px; border: 0; border-radius: 6px; background: transparent; color: var(--text-dim); cursor: pointer; text-align: left; }
.nav button:hover { color: var(--text); background: var(--surface-2); }
.nav button.active { color: var(--accent); background: var(--accent-dim); }
.badge { min-width: 22px; text-align: center; border-radius: 999px; padding: 1px 7px; color: #061113; background: var(--accent); font-size: 11px; font-weight: 800; }
.badge.rose { background: var(--rose); color: #fff; }
.profile { margin-top: auto; display: grid; grid-template-columns: 34px minmax(0,1fr); gap: 10px; align-items: center; padding: 10px; border: 1px solid var(--border-soft); border-radius: var(--radius); background: var(--surface-2); }
.avatar { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 50%; background: var(--green); color: #061113; font-weight: 900; font-size: 12px; }
.profile strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.profile span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-dim); font-size: 11px; margin-top: 2px; }
.main { min-width: 0; padding: 28px 32px; }
.view { display: none; animation: enter .18s ease-out; }
.view.active { display: block; }
@keyframes enter { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
.view-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 22px; }
h1 { margin: 0; font-size: clamp(24px, 3vw, 32px); line-height: 1.05; }
.sub { margin: 7px 0 0; color: var(--text-dim); font-size: 13px; line-height: 1.5; }
.controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.btn, .chip { border: 1px solid var(--border); border-radius: 6px; background: var(--surface-2); color: var(--text); padding: 8px 12px; cursor: pointer; text-decoration: none; white-space: nowrap; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #061113; font-weight: 800; }
.btn:hover, .chip:hover { border-color: var(--accent); }
.chip.active { background: var(--accent-dim); border-color: rgba(61,219,217,.5); color: var(--accent); }
.input { min-width: 260px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface-2); color: var(--text); padding: 9px 11px; outline: none; }
.input:focus { border-color: var(--accent); }
.stats { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 14px; margin-bottom: 20px; }
.stat { border: 1px solid var(--border); border-radius: var(--radius); background: rgba(16,24,32,.92); padding: 18px; min-height: 112px; }
.stat.highlight { border-color: rgba(89,212,135,.55); background: rgba(89,212,135,.08); }
.stat-value { font-size: 32px; line-height: 1; font-weight: 900; }
.stat-label { margin-top: 8px; color: var(--text-dim); font-size: 12px; }
.stat-note { margin-top: 5px; color: var(--text-faint); font-size: 11px; }
.grid { display: grid; gap: 16px; }
.grid.two { grid-template-columns: minmax(0, 1fr) 340px; }
.panel { border: 1px solid var(--border); border-radius: var(--radius); background: rgba(16,24,32,.92); padding: 18px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 12px; }
.panel h2, .panel h3 { margin: 0; font-size: 14px; }
.meta { color: var(--text-dim); font-size: 12px; }
.bar { height: 9px; display: flex; gap: 2px; overflow: hidden; border-radius: 999px; background: var(--surface-2); margin-bottom: 13px; }
.bar span { min-width: 2px; }
.legend { display: flex; flex-wrap: wrap; gap: 10px 16px; color: var(--text-dim); font-size: 12px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
.job-list { display: grid; gap: 12px; }
.job-card, .row-card { border: 1px solid var(--border-soft); border-radius: var(--radius); background: var(--surface-2); padding: 14px; }
.job-card.strong { border-color: rgba(61,219,217,.42); }
.job-card.elite { border-color: rgba(89,212,135,.52); }
.job-top { display: grid; grid-template-columns: 42px minmax(0,1fr) auto; gap: 12px; align-items: start; }
.logo { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 8px; background: var(--surface-3); color: var(--accent); font-weight: 900; border: 1px solid var(--border); }
.role { font-weight: 800; line-height: 1.25; }
.company { color: var(--text-dim); margin-top: 3px; font-size: 13px; }
.job-meta { color: var(--text-faint); margin-top: 4px; font-size: 12px; }
.score { border: 1px solid currentColor; border-radius: 999px; padding: 3px 8px; font-size: 12px; font-weight: 900; color: var(--accent); background: rgba(61,219,217,.1); }
.score.elite { color: var(--green); background: rgba(89,212,135,.1); }
.score.weak { color: var(--text-dim); background: transparent; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.tag { border-radius: 999px; padding: 3px 8px; background: var(--surface-3); color: var(--text-dim); font-size: 11px; }
.tag.system { color: var(--accent); background: var(--accent-dim); }
.tag.prepared { color: var(--green); background: rgba(89,212,135,.12); }
.tag.status { color: var(--amber); background: rgba(230,180,80,.12); }
.snippet { margin-top: 12px; color: var(--text-dim); line-height: 1.45; font-size: 13px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 13px; }
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius); }
table { width: 100%; border-collapse: collapse; min-width: 760px; background: rgba(16,24,32,.92); }
th, td { text-align: left; border-bottom: 1px solid var(--border-soft); padding: 11px 12px; font-size: 13px; vertical-align: top; }
th { color: var(--text-dim); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
tr:last-child td { border-bottom: 0; }
.status-pill { display: inline-block; border-radius: 999px; padding: 3px 8px; font-size: 11px; font-weight: 800; background: var(--surface-3); color: var(--text-dim); }
.status-pill.submitted, .status-pill.applied { color: var(--green); background: rgba(89,212,135,.12); }
.status-pill.failed, .status-pill.failed_not_found, .status-pill.failed_auth, .status-pill.failed_captcha { color: var(--rose); background: rgba(255,107,122,.12); }
.status-pill.ready, .status-pill.queued { color: var(--accent); background: var(--accent-dim); }
.split-list { display: grid; gap: 8px; }
.kv { display: flex; justify-content: space-between; gap: 14px; padding: 9px 10px; border-radius: 6px; background: var(--surface-2); }
.kv span:first-child { color: var(--text-dim); }
.source-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; }
.empty { color: var(--text-dim); border: 1px dashed var(--border); border-radius: var(--radius); padding: 24px; text-align: center; background: rgba(16,24,32,.45); }
.fine-print { margin-top: 14px; color: var(--text-faint); font-size: 11px; }
.scan-btn { display:flex; align-items:center; gap:7px; width:100%; padding:9px 10px; border:1px solid rgba(61,219,217,.35); border-radius:6px; background:var(--accent-dim); color:var(--accent); cursor:pointer; font-size:13px; font-weight:700; }
.scan-btn:hover { border-color:var(--accent); background:rgba(61,219,217,.18); }
.scan-btn:disabled { opacity:.45; cursor:not-allowed; }
.scan-overlay { display:none; position:fixed; inset:0; z-index:200; background:rgba(0,0,0,.72); backdrop-filter:blur(4px); align-items:center; justify-content:center; }
.scan-overlay.open { display:flex; }
.scan-modal { width:min(700px,95vw); max-height:82vh; display:flex; flex-direction:column; border:1px solid var(--border); border-radius:12px; background:var(--surface); overflow:hidden; }
.scan-modal-head { display:flex; align-items:center; justify-content:space-between; padding:14px 18px; border-bottom:1px solid var(--border); }
.scan-modal-head h2 { margin:0; font-size:14px; }
.scan-log { flex:1; overflow-y:auto; padding:14px 16px; font-family:ui-monospace,monospace; font-size:11.5px; line-height:1.65; color:var(--text-dim); background:var(--bg); white-space:pre-wrap; word-break:break-all; min-height:180px; max-height:420px; }
.scan-foot { padding:12px 16px; border-top:1px solid var(--border); display:flex; gap:10px; align-items:center; min-height:52px; }
.spin { width:13px; height:13px; border:2px solid var(--border); border-top-color:var(--accent); border-radius:50%; animation:spin .75s linear infinite; flex-shrink:0; }
@keyframes spin { to { transform:rotate(360deg); } }
@media (max-width: 1050px) {
  .shell { grid-template-columns: 1fr; }
  .sidebar { position: relative; height: auto; }
  .nav { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .profile { display: none; }
  .main { padding: 22px 18px; }
  .stats { grid-template-columns: repeat(2, minmax(0,1fr)); }
  .grid.two { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .nav { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .view-header { display: grid; }
  .stats { grid-template-columns: 1fr; }
  .input { min-width: 100%; width: 100%; }
  .job-top { grid-template-columns: 36px minmax(0,1fr); }
  .score { grid-column: 2; width: max-content; }
}
</style>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <div class="brand"><div class="mark">JH</div><div class="brand-name">Job<span>Hunter</span></div></div>
    <nav class="nav" id="nav"></nav>
    <button class="scan-btn" id="scanBtn" onclick="startScan()">&#x27F3; Scan Now</button>
    <div class="profile">
      <div class="avatar" id="avatar">TD</div>
      <div><strong id="profileName"></strong><span id="profileTitle"></span></div>
    </div>
  </aside>
  <main class="main">
    <section class="view active" id="dashboard"></section>
    <section class="view" id="discover"></section>
    <section class="view" id="applications"></section>
    <section class="view" id="queue"></section>
    <section class="view" id="engine"></section>
  </main>
</div>
<div class="scan-overlay" id="scanOverlay">
  <div class="scan-modal">
    <div class="scan-modal-head">
      <h2>Portal Scan</h2>
      <button onclick="closeScanModal()" style="background:none;border:0;color:var(--text-dim);cursor:pointer;font-size:20px;line-height:1;padding:0 4px">&times;</button>
    </div>
    <div class="scan-log" id="scanLog">Ready to scan…</div>
    <div class="scan-foot" id="scanFoot"></div>
  </div>
</div>
<script>
const DATA = ${safeJson(snapshot)};
const NAV = [
  ["dashboard", "Dashboard", ""],
  ["discover", "Discover Jobs", DATA.jobs.length],
  ["applications", "Applications", DATA.applications.length],
  ["queue", "Apply Queue", DATA.queue.length],
  ["engine", "Engine", DATA.cronJobs.length],
];
let leadFilter = "all";
let leadSearch = "";
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, function(char) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#039;"}[char];
  });
}
function fmt(value) {
  return Number(value || 0).toLocaleString();
}
function dt(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
function initials(name) {
  return String(name || "TJ").split(/\\s+/).filter(Boolean).slice(0,2).map(function(part) { return part[0]; }).join("").toUpperCase();
}
function scoreClass(score) {
  if (score >= 80) return "elite";
  if (score >= 70) return "strong";
  if (score < 55) return "weak";
  return "";
}
function setView(id) {
  document.querySelectorAll(".view").forEach(function(node) { node.classList.toggle("active", node.id === id); });
  document.querySelectorAll(".nav button").forEach(function(node) { node.classList.toggle("active", node.dataset.view === id); });
}
function renderNav() {
  const nav = document.getElementById("nav");
  nav.innerHTML = NAV.map(function(item) {
    const badge = item[2] !== "" ? '<span class="badge' + (item[0] === "queue" && DATA.stats.queueFailed ? " rose" : "") + '">' + esc(item[2]) + '</span>' : "";
    return '<button data-view="' + item[0] + '"><span>' + esc(item[1]) + '</span>' + badge + '</button>';
  }).join("");
  nav.querySelectorAll("button").forEach(function(button) {
    button.addEventListener("click", function() { setView(button.dataset.view); });
  });
  document.querySelector('.nav button[data-view="dashboard"]').classList.add("active");
}
function stat(label, value, note, highlight) {
  return '<div class="stat' + (highlight ? " highlight" : "") + '"><div class="stat-value">' + esc(value) + '</div><div class="stat-label">' + esc(label) + '</div><div class="stat-note">' + esc(note || "") + '</div></div>';
}
function renderDashboard() {
  const s = DATA.stats;
  const submittedPct = s.queueSize ? Math.round((s.queueSubmitted / s.queueSize) * 100) : 0;
  const failedPct = s.queueSize ? Math.round((s.queueFailed / s.queueSize) * 100) : 0;
  const queuedPct = Math.max(0, 100 - submittedPct - failedPct);
  const topLeads = DATA.jobs.slice()
    .sort(function(a, b) { return Number(b.score || 0) - Number(a.score || 0); })
    .slice(0, 4)
    .map(renderLeadCard).join("") || '<div class="empty">No leads found yet.</div>';
  document.getElementById("dashboard").innerHTML =
    '<div class="view-header"><div><h1>Dashboard</h1><p class="sub">Career command center for ' + esc(DATA.user.title) + '. Snapshot generated ' + esc(dt(DATA.generatedAt)) + '.</p></div><div class="controls"><button class="btn" onclick="refreshSnapshot()">Refresh snapshot</button><a class="btn primary" href="#discover" data-jump="discover">View leads</a></div></div>' +
    '<div class="stats">' +
      stat("Raw jobs scanned", fmt(s.rawCount), fmt(s.afterDedupe) + " after dedupe") +
      stat("Leads shown", fmt(s.totalLeads), fmt(s.hermesLeads) + " Hermes, " + fmt(s.openclawLeads) + " OpenClaw, " + fmt(s.careerOpsLeads) + " Career-Ops", true) +
      stat("Apply queue", fmt(s.queueSize), fmt(s.queueSubmitted) + " submitted, " + fmt(s.queueFailed) + " blocked") +
      stat("Agent crons", fmt(DATA.cronJobs.length), fmt(s.hermesCronHooks) + " Hermes, " + fmt(s.openclawCronHooks) + " OpenClaw") +
    '</div>' +
    '<div class="grid two"><div class="panel"><div class="panel-head"><h2>Top Leads Ready</h2><span class="meta">Hermes + OpenClaw score</span></div><div class="job-list">' + topLeads + '</div></div>' +
    '<div class="grid">' +
      '<div class="panel"><div class="panel-head"><h2>Apply Queue</h2><span class="meta">' + fmt(s.queueSize) + ' items</span></div><div class="bar"><span style="width:' + submittedPct + '%;background:var(--green)"></span><span style="width:' + failedPct + '%;background:var(--rose)"></span><span style="width:' + queuedPct + '%;background:var(--accent)"></span></div><div class="legend"><span><i class="dot" style="background:var(--green)"></i>Submitted ' + fmt(s.queueSubmitted) + '</span><span><i class="dot" style="background:var(--rose)"></i>Blocked ' + fmt(s.queueFailed) + '</span><span><i class="dot" style="background:var(--accent)"></i>Queued ' + fmt(Math.max(0, s.queueSize - s.queueSubmitted - s.queueFailed)) + '</span></div></div>' +
      '<div class="panel"><div class="panel-head"><h2>Sources</h2><span class="meta">' + DATA.sourceBreakdown.length + ' active feeds</span></div><div class="split-list">' + DATA.sourceBreakdown.slice(0, 6).map(function(src) { return '<div class="kv"><span>' + esc(src.name) + '</span><strong>' + fmt(src.count) + '</strong></div>'; }).join("") + '</div></div>' +
      '<div class="panel"><div class="panel-head"><h2>Engine</h2><span class="meta">' + DATA.cronJobs.length + ' job hooks</span></div><div class="split-list">' + DATA.cronJobs.slice(0, 4).map(function(job) { return '<div class="kv"><span>' + esc(job.name) + '</span><strong>' + esc(job.enabled ? "on" : "paused") + '</strong></div>'; }).join("") + '</div></div>' +
    '</div></div>';
  bindJumpLinks();
}
function renderLeadCard(job) {
  const cls = scoreClass(job.score);
  const scoreLabel = job.score === "" || job.score == null ? "new" : job.score;
  const tags = job.tags.slice(0, 7).map(function(tag) { return '<span class="tag">' + esc(tag) + '</span>'; }).join("");
  const system = job.sourceSystem ? '<span class="tag system">' + esc(job.sourceSystem) + '</span>' : "";
  const prepared = job.prepared ? '<span class="tag prepared">prepped</span>' : "";
  const qstatus = job.queueStatus ? '<span class="tag status">' + esc(job.queueStatus) + '</span>' : "";
  const href = job.url ? '<a class="btn" href="' + esc(job.url) + '" target="_blank" rel="noreferrer">Open posting</a>' : "";
  const meta = [job.location, job.source, job.salary, job.posted ? "Posted " + job.posted : ""].filter(Boolean).join(" | ");
  return '<article class="job-card ' + cls + '">' +
    '<div class="job-top"><div class="logo">' + esc((job.company || "?")[0].toUpperCase()) + '</div><div><div class="role">' + esc(job.title) + '</div><div class="company">' + esc(job.company) + '</div><div class="job-meta">' + esc(meta) + '</div></div><span class="score ' + cls + '">' + esc(scoreLabel) + '</span></div>' +
    '<div class="tags">' + system + tags + prepared + qstatus + '</div>' +
    (job.snippet ? '<div class="snippet">' + esc(job.snippet) + '</div>' : '') +
    '<div class="actions">' + href + '</div>' +
    '</article>';
}
function filteredLeads() {
  return DATA.jobs.filter(function(job) {
    const matchesFilter = leadFilter === "all" ||
      (leadFilter === "elite" && job.score >= 80) ||
      (leadFilter === "strong" && job.score >= 70) ||
      (leadFilter === "prepared" && job.prepared) ||
      (leadFilter === "providers" && job.sourceSystem === "Career-Ops");
    const haystack = [job.title, job.company, job.location, job.source, job.sourceSystem, job.tags.join(" ")].join(" ").toLowerCase();
    return matchesFilter && haystack.includes(leadSearch.toLowerCase());
  });
}
function renderDiscover() {
  document.getElementById("discover").innerHTML =
    '<div class="view-header"><div><h1>Discover Jobs</h1><p class="sub">' + fmt(DATA.stats.rawCount) + ' raw Hermes jobs, ' + fmt(DATA.stats.afterDedupe) + ' deduped, ' + fmt(DATA.stats.openclawLeads) + ' OpenClaw digest leads, and ' + fmt(DATA.stats.careerOpsLeads) + ' Career-Ops provider leads.</p></div><div class="controls"><input class="input" id="leadSearch" placeholder="Search leads, companies, skills"><button class="btn primary" id="clearLeadSearch">Clear</button></div></div>' +
    '<div class="controls" style="margin-bottom:16px">' +
      ['all','elite','strong','prepared','providers'].map(function(key) { return '<button class="chip" data-filter="' + key + '">' + ({all:'All leads', elite:'Elite 80+', strong:'Strong 70+', prepared:'Prepped', providers:'Providers'})[key] + '</button>'; }).join("") +
    '</div><div class="job-list" id="leadResults"></div>';
  const input = document.getElementById("leadSearch");
  input.value = leadSearch;
  input.addEventListener("input", function(event) {
    leadSearch = event.target.value;
    renderLeadResults();
  });
  document.getElementById("clearLeadSearch").addEventListener("click", function() {
    leadSearch = "";
    renderDiscover();
  });
  document.querySelectorAll("[data-filter]").forEach(function(button) {
    button.classList.toggle("active", button.dataset.filter === leadFilter);
    button.addEventListener("click", function() {
      leadFilter = button.dataset.filter;
      renderDiscover();
    });
  });
  renderLeadResults();
}
function renderLeadResults() {
  const leads = filteredLeads();
  document.getElementById("leadResults").innerHTML = leads.length
    ? leads.map(renderLeadCard).join("")
    : '<div class="empty">No leads match the current filter.</div>';
}
function renderApplications() {
  const appRows = DATA.applications.map(function(app) {
    return '<tr><td>#' + esc(app.number) + '</td><td>' + esc(app.date) + '</td><td><strong>' + esc(app.company) + '</strong></td><td>' + esc(app.role) + '</td><td>' + esc(app.score) + '</td><td><span class="status-pill ' + esc(app.status.toLowerCase()) + '">' + esc(app.status) + '</span></td><td>' + esc(app.notes) + '</td></tr>';
  }).join("");
  const pendingRows = DATA.pipeline.filter(function(item) { return !item.done; }).map(function(item) {
    return '<tr><td>pending</td><td></td><td><strong>' + esc(item.company) + '</strong></td><td>' + esc(item.role) + '</td><td></td><td><span class="status-pill ready">queued</span></td><td><a href="' + esc(item.url) + '" target="_blank" rel="noreferrer">posting</a></td></tr>';
  }).join("");
  document.getElementById("applications").innerHTML =
    '<div class="view-header"><div><h1>Applications</h1><p class="sub">Career-ops tracker plus pending scan pipeline entries.</p></div></div>' +
    '<div class="table-wrap"><table><thead><tr><th>#</th><th>Date</th><th>Company</th><th>Role</th><th>Score</th><th>Status</th><th>Notes</th></tr></thead><tbody>' + (appRows + pendingRows || '<tr><td colspan="7">No tracked applications yet.</td></tr>') + '</tbody></table></div>';
}
function renderQueue() {
  const rows = DATA.queue.map(function(item) {
    return '<tr><td>#' + esc(item.number) + '</td><td><strong>' + esc(item.company) + '</strong><div class="meta">' + esc(item.location) + '</div></td><td>' + esc(item.title) + '</td><td>' + esc(item.score) + '</td><td><span class="status-pill ' + esc(item.status) + '">' + esc(item.status) + '</span></td><td>' + esc(item.salary) + '</td><td>' + esc(dt(item.updatedAt)) + '</td><td>' + (item.url ? '<a href="' + esc(item.url) + '" target="_blank" rel="noreferrer">open</a>' : '') + '</td></tr>';
  }).join("");
  document.getElementById("queue").innerHTML =
    '<div class="view-header"><div><h1>Apply Queue</h1><p class="sub">Prepared application queue from the Hermes job-search run. Submit safeguards stay outside this dashboard.</p></div></div>' +
    '<div class="table-wrap"><table><thead><tr><th>#</th><th>Company</th><th>Role</th><th>Score</th><th>Status</th><th>Salary</th><th>Updated</th><th>Link</th></tr></thead><tbody>' + (rows || '<tr><td colspan="8">No queued applications found.</td></tr>') + '</tbody></table></div>';
}
function renderEngine() {
  const cronRows = DATA.cronJobs.map(function(job) {
    const statusClass = /error|fail/i.test(job.lastStatus) ? "failed" : (job.enabled ? "submitted" : "queued");
    const error = job.lastError ? '<div class="snippet">' + esc(job.lastError) + '</div>' : "";
    return '<div class="row-card"><div class="panel-head"><h3>' + esc(job.name) + '</h3><span class="status-pill ' + statusClass + '">' + esc(job.enabled ? "enabled" : "paused") + '</span></div><div class="meta">' + esc(job.script || "agent prompt") + '</div><div class="tags"><span class="tag system">' + esc(job.sourceSystem) + '</span><span class="tag">' + esc(job.schedule || "unscheduled") + '</span><span class="tag">' + esc(job.lastStatus || "no status") + '</span><span class="tag">last ' + esc(dt(job.lastRunAt) || "never") + '</span><span class="tag">next ' + esc(dt(job.nextRunAt) || "none") + '</span></div>' + error + '</div>';
  }).join("");
  const runRows = DATA.openclaw.runs.map(function(run) {
    const statusClass = /error|fail/i.test(run.status) ? "failed" : "submitted";
    const counts = run.counts && run.counts.raw ? fmt(run.counts.raw) + " raw / " + fmt(run.counts.viable) + " viable / " + fmt(run.counts.shown) + " shown" : "counts unavailable";
    return '<div class="row-card"><div class="panel-head"><h3>OpenClaw run</h3><span class="status-pill ' + statusClass + '">' + esc(run.status || "unknown") + '</span></div><div class="tags"><span class="tag">ran ' + esc(dt(run.runAt) || "unknown") + '</span><span class="tag">' + esc(counts) + '</span><span class="tag">' + esc(run.deliveryStatus || "delivery unknown") + '</span></div><div class="snippet">' + esc(run.summaryPreview || run.error || "") + '</div></div>';
  }).join("");
  const sourceRows = DATA.sourceBreakdown.map(function(source) {
    return '<div class="kv"><span>' + esc(source.name) + '</span><strong>' + fmt(source.count) + '</strong></div>';
  }).join("");
  const portalRows = DATA.scannedPortals.map(function(source) {
    return '<div class="kv"><span>' + esc(source.name) + '</span><strong>' + fmt(source.count) + '</strong></div>';
  }).join("");
  const providerRows = DATA.providerInventory.map(function(provider) {
    return '<div class="kv"><span>' + esc(provider.id) + '</span><strong>' + esc(provider.file) + '</strong></div>';
  }).join("");
  document.getElementById("engine").innerHTML =
    '<div class="view-header"><div><h1>Engine</h1><p class="sub">Local JobSpy, apply queue, cron, and career-ops scan signals in one place.</p></div></div>' +
    '<div class="stats">' +
      stat("Skipped below threshold", fmt(DATA.stats.skippedBelowThreshold), "filtered before display") +
      stat("Sources", fmt(DATA.sourceBreakdown.length), "JobSpy, MCP, and provider feeds", true) +
      stat("Provider adapters", fmt(DATA.stats.providerAdapters), "from career-ops providers/") +
      stat("Cron hooks", fmt(DATA.cronJobs.length), "job/apply related") +
    '</div>' +
    '<div class="grid two"><div class="panel"><div class="panel-head"><h2>Cron Jobs</h2><span class="meta">Hermes + OpenClaw</span></div><div class="job-list">' + (cronRows || '<div class="empty">No job-related cron hooks found.</div>') + '</div></div>' +
    '<div class="grid"><div class="panel"><div class="panel-head"><h2>OpenClaw Runs</h2><span class="meta">' + esc(dt(DATA.openclaw.latestSuccessfulRunAt) || "no successful run") + '</span></div><div class="job-list">' + (runRows || '<div class="empty">No OpenClaw cron runs found.</div>') + '</div></div><div class="panel"><div class="panel-head"><h2>Job Sources</h2></div><div class="split-list">' + (sourceRows || '<div class="empty">No source counts found.</div>') + '</div></div><div class="panel"><div class="panel-head"><h2>Provider Adapters</h2></div><div class="split-list">' + (providerRows || '<div class="empty">No provider adapters found.</div>') + '</div></div><div class="panel"><div class="panel-head"><h2>Career-Ops Portals</h2></div><div class="split-list">' + (portalRows || '<div class="empty">No portal scan history found.</div>') + '</div></div><div class="panel"><div class="panel-head"><h2>Profile</h2></div><div class="split-list"><div class="kv"><span>Location</span><strong>' + esc(DATA.user.location || "not set") + '</strong></div><div class="kv"><span>Remote</span><strong>' + esc(DATA.user.remotePreference || "not set") + '</strong></div><div class="kv"><span>Salary min</span><strong>' + esc(DATA.user.salaryMin || "not set") + '</strong></div></div></div></div></div>' +
    '<div class="fine-print">Snapshot sources: ' + esc(DATA.paths.jobSearchDir) + ', ' + esc(DATA.paths.openclawHome) + ', and ' + esc(DATA.paths.projectRoot) + '/data.</div>';
}
function bindJumpLinks() {
  document.querySelectorAll("[data-jump]").forEach(function(link) {
    link.addEventListener("click", function(event) {
      event.preventDefault();
      setView(link.dataset.jump);
    });
  });
}
async function refreshSnapshot() {
  try {
    const response = await fetch(API + '/api/refresh', { method: 'POST' });
    if (!response.ok) throw new Error('HTTP ' + response.status);
  } catch (_) {
    // Static-file mode cannot reach the local refresh API, but a reload still
    // shows the latest generated HTML if the launcher/server refreshed it.
  }
  window.location.reload();
}
function boot() {
  document.getElementById("profileName").textContent = DATA.user.name;
  document.getElementById("profileTitle").textContent = DATA.user.title;
  document.getElementById("avatar").textContent = initials(DATA.user.name);
  renderNav();
  renderDashboard();
  renderDiscover();
  renderApplications();
  renderQueue();
  renderEngine();
}
boot();

// ── Scan button ────────────────────────────────────────────────────
const API = window.location.hostname === 'localhost' ? '' : 'http://localhost:7432';
let pollTimer = null;

function openScanModal() { document.getElementById('scanOverlay').classList.add('open'); }
function closeScanModal() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  document.getElementById('scanOverlay').classList.remove('open');
}
document.getElementById('scanOverlay').addEventListener('click', function(e) {
  if (e.target === this) closeScanModal();
});

async function startScan() {
  const btn = document.getElementById('scanBtn');
  btn.disabled = true;
  document.getElementById('scanLog').textContent = 'Starting scan…\\n';
  document.getElementById('scanFoot').innerHTML = '<div class="spin"></div><span style="color:var(--text-dim);font-size:12px;margin-left:8px">Scanning portals — this takes a minute…</span>';
  openScanModal();
  try {
    const r = await fetch(API + '/api/scan', { method: 'POST' });
    if (!r.ok && r.status !== 409) throw new Error('HTTP ' + r.status);
    pollScan();
  } catch (err) {
    document.getElementById('scanLog').textContent = 'Could not reach the scan server.\\n\\nMake sure the dashboard was launched via the JobHunter Dashboard.command file.\\n\\nError: ' + err.message;
    document.getElementById('scanFoot').innerHTML = '<span style="color:var(--rose);font-size:12px">Server not reachable</span><button class="btn" onclick="closeScanModal()" style="margin-left:auto">Close</button>';
    btn.disabled = false;
  }
}

function pollScan() {
  pollTimer = setInterval(async function() {
    try {
      const r = await fetch(API + '/api/scan/status');
      const d = await r.json();
      const logEl = document.getElementById('scanLog');
      logEl.textContent = d.log.join('') || 'Running…';
      logEl.scrollTop = logEl.scrollHeight;
      if (d.status === 'done') {
        clearInterval(pollTimer); pollTimer = null;
        document.getElementById('scanFoot').innerHTML = '<span style="color:var(--green);font-size:12px;font-weight:700">Scan complete!</span><button class="btn primary" onclick="window.location.reload()" style="margin-left:auto">Reload Dashboard</button>';
        document.getElementById('scanBtn').disabled = false;
      } else if (d.status === 'error') {
        clearInterval(pollTimer); pollTimer = null;
        document.getElementById('scanFoot').innerHTML = '<span style="color:var(--rose);font-size:12px">Scan ended with errors — check log above</span><button class="btn" onclick="closeScanModal()" style="margin-left:auto">Close</button>';
        document.getElementById('scanBtn').disabled = false;
      }
    } catch (_) { /* transient — keep polling */ }
  }, 1000);
}
</script>
</body>
</html>
`;
}

const snapshot = buildSnapshot();
fs.mkdirSync(path.dirname(outputPath), { recursive: true });
fs.writeFileSync(outputPath, renderHtml(snapshot), "utf8");
console.log(outputPath);
