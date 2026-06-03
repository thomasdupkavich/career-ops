import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const root = path.resolve(import.meta.dirname, "..");
const generator = path.join(root, "dashboard", "generate-job-hunter.mjs");

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(value, null, 2));
}

test("job hunter dashboard reads ONLY career-ops data and ignores Hermes/OpenClaw", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "job-hunter-decoupled-fixture-"));
  const careerOpsRoot = path.join(tmp, "career-ops");
  const hermesHome = path.join(tmp, "hermes");
  const hermesJobSearch = path.join(hermesHome, "job-search");
  const openclawHome = path.join(tmp, "openclaw");
  const out = path.join(tmp, "job-hunter.html");

  // Career-ops canonical sink: rows written by the local engines (ATS + JobSpy + Deep Scan).
  fs.mkdirSync(path.join(careerOpsRoot, "data"), { recursive: true });
  fs.writeFileSync(
    path.join(careerOpsRoot, "data", "scan-history.tsv"),
    [
      "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\tlocation",
      "https://example.com/ats-role\t2026-06-03\tgreenhouse-api\tATS Portal Role\tAtsCo\tadded\tRemote",
      "https://example.com/jobspy-role\t2026-06-03\tjobspy-indeed\tJobSpy Aggregator Role\tSpyCo\tadded\tNew York, NY",
      "https://example.com/deep-role\t2026-06-03\tdeep-claude\tDeep Scan Discovered Role\tDeepCo\tadded\tRemote",
    ].join("\n") + "\n",
  );
  fs.writeFileSync(path.join(careerOpsRoot, "data", "pipeline.md"),
    "# Job URL Pipeline\n\n## Pendientes\n\n## Procesadas\n");

  // Hermes + OpenClaw content that the OLD dashboard merged in — must be IGNORED now.
  writeJson(path.join(hermesJobSearch, "jobs-last.json"), {
    sections: { remote: [{ n: 1, score: 81, title: "Hermes Remote Role", company: "HermesCo", url: "https://example.com/hermes" }] },
  });
  writeJson(path.join(hermesHome, "cron", "jobs.json"), {
    jobs: [{ id: "hermes-job", name: "Hermes daily job search", enabled: true }],
  });
  writeJson(path.join(openclawHome, "cron", "jobs.json"), {
    jobs: [{ id: "openclaw-job", name: "OpenClaw daily resume-based software job search", enabled: true }],
  });

  execFileSync("node", [generator, "--project-root", careerOpsRoot, "--out", out], { cwd: root });

  const html = fs.readFileSync(out, "utf8");
  // Career-ops leads from all three local engines are present…
  assert.match(html, /ATS Portal Role/);
  assert.match(html, /JobSpy Aggregator Role/);
  assert.match(html, /Deep Scan Discovered Role/);
  // …the Deep Scan button is wired…
  assert.match(html, /startDeepScan/);
  assert.match(html, /api\/scan\/deep/);
  // …and NO Hermes/OpenClaw content leaks in.
  assert.doesNotMatch(html, /Hermes Remote Role/);
  assert.doesNotMatch(html, /Hermes daily job search/);
  assert.doesNotMatch(html, /OpenClaw daily resume-based software job search/);
});

test("job hunter dashboard promotes career-ops provider scan results into discoverable leads", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "job-hunter-provider-fixture-"));
  const careerOpsRoot = path.join(tmp, "career-ops");
  const hermesHome = path.join(tmp, "hermes");
  const hermesJobSearch = path.join(hermesHome, "job-search");
  const openclawHome = path.join(tmp, "openclaw");
  const out = path.join(tmp, "job-hunter.html");

  fs.mkdirSync(path.join(careerOpsRoot, "data"), { recursive: true });
  fs.writeFileSync(
    path.join(careerOpsRoot, "data", "scan-history.tsv"),
    [
      "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\tlocation",
      "https://example.com/provider-role\t2026-05-19\trecruitee-api\tFixture Provider Role\tFixtureCo\tadded\tRemote",
    ].join("\n") + "\n",
  );
  fs.writeFileSync(
    path.join(careerOpsRoot, "data", "pipeline.md"),
    [
      "# Job URL Pipeline",
      "",
      "## Pendientes",
      "",
      "- [ ] https://example.com/pending-provider-role | PendingCo | SmartRecruiters Platform Engineer",
    ].join("\n") + "\n",
  );

  fs.mkdirSync(hermesJobSearch, { recursive: true });
  writeJson(path.join(hermesJobSearch, "jobs-last.json"), { stats: {}, sections: {} });
  writeJson(path.join(hermesJobSearch, "apply-queue.json"), []);
  writeJson(path.join(hermesHome, "cron", "jobs.json"), { jobs: [] });
  writeJson(path.join(openclawHome, "cron", "jobs.json"), { jobs: [] });
  writeJson(path.join(openclawHome, "cron", "jobs-state.json"), { jobs: {} });

  execFileSync("node", [
    generator,
    "--project-root",
    careerOpsRoot,
    "--hermes-home",
    hermesHome,
    "--job-search-dir",
    hermesJobSearch,
    "--openclaw-home",
    openclawHome,
    "--out",
    out,
  ], { cwd: root });

  const html = fs.readFileSync(out, "utf8");
  assert.match(html, /Fixture Provider Role/);
  assert.match(html, /recruitee-api/);
  assert.match(html, /SmartRecruiters Platform Engineer/);
  assert.match(html, /Career-Ops/);
});
