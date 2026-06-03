// Profile loader. Single source of truth = career-ops/config/profile.yml
// (the user-layer file that career-ops update-system never overwrites).
//
// We supplement it with the EEO/work-auth/salary defaults the user has
// already vetted in ~/.openclaw/workspace/job_search/application_profile.md
// — those answers are stable and don't belong in profile.yml schema.

import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname, basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');
const PROFILE_YML = resolve(REPO_ROOT, 'config', 'profile.yml');
const CV_MD = resolve(REPO_ROOT, 'cv.md');

// Resume candidates (best wins). Add to this list as new resume artifacts land.
// 2026-05-21 — career-ops/output/ now has tailored PDFs; prefer those.
import { readdirSync } from 'node:fs';
function _outputPdfs() {
  try {
    const dir = resolve(REPO_ROOT, 'output');
    return readdirSync(dir)
      .filter((n) => n.startsWith('cv-thomas-dupkavich-') && n.endsWith('.pdf'))
      .map((n) => resolve(dir, n));
  } catch { return []; }
}
const RESUME_CANDIDATES = [
  ..._outputPdfs(),
  resolve(REPO_ROOT, 'output', 'cv', 'thomas-dupkavich.pdf'),
  // Canonical resume copy inside the repo (synced from the user's GDrive resume).
  resolve(REPO_ROOT, 'data', 'resume', 'Thomas_Dupkavich_Resume.pdf'),
  resolve(REPO_ROOT, 'data', 'resume', 'Thomas_Dupkavich_Resume.docx'),
  // Last-resort fallback, resolved from the home dir (no hardcoded absolute path).
  resolve(homedir(), 'Downloads', 'Thomas_Dupkavich_Resume.pdf'),
];

// Minimal YAML reader — pulls the top-level scalars and one nested level
// for `candidate.*`. We deliberately avoid pulling in a YAML library;
// the file is hand-edited and stable.
function parseSimpleYaml(text) {
  const out = {};
  let currentSection = null;
  for (const rawLine of text.split('\n')) {
    const line = rawLine.replace(/#.*$/, '').trimEnd();
    if (!line.trim()) continue;
    const sectionMatch = line.match(/^([a-z_][a-z0-9_]*):\s*$/i);
    if (sectionMatch) {
      currentSection = sectionMatch[1];
      out[currentSection] = out[currentSection] || {};
      continue;
    }
    const kvMatch = line.match(/^(\s*)([a-z_][a-z0-9_]*):\s*(.*)$/i);
    if (!kvMatch) continue;
    const indent = kvMatch[1].length;
    const key = kvMatch[2];
    let raw = kvMatch[3].trim();
    if (raw.startsWith('"') && raw.endsWith('"')) raw = raw.slice(1, -1);
    else if (raw.startsWith("'") && raw.endsWith("'")) raw = raw.slice(1, -1);
    if (indent === 0) {
      out[key] = raw;
      currentSection = null;
    } else if (currentSection) {
      out[currentSection][key] = raw;
    }
  }
  return out;
}

function bestResume() {
  for (const p of RESUME_CANDIDATES) {
    if (existsSync(p)) return p;
  }
  return null;
}

let cached = null;

export function loadProfile() {
  if (cached) return cached;

  const yml = existsSync(PROFILE_YML) ? parseSimpleYaml(readFileSync(PROFILE_YML, 'utf8')) : {};
  const cand = yml.candidate || {};

  // Name parsing: yml gives "Thomas J. Dupkavich"
  const fullName = (cand.full_name || 'Thomas J. Dupkavich').trim();
  const nameParts = fullName.split(/\s+/);
  const firstName = nameParts[0];
  const lastName = nameParts[nameParts.length - 1];
  const middleInitial = nameParts.length > 2 ? nameParts[1].replace(/\.$/, '') : '';

  // Location parsing: yml gives "Huntington Station, NY"
  const locRaw = (cand.location || 'Huntington Station, NY').trim();
  const [city, stateRaw] = locRaw.split(',').map((s) => s.trim());

  // Phone normalization: yml gives "(631) 338-2304"
  const phoneFormatted = cand.phone || '(631) 338-2304';
  const phone = phoneFormatted.replace(/\D/g, '');

  cached = {
    source: { yml: PROFILE_YML, cv: CV_MD },
    identity: {
      firstName,
      middleInitial,
      lastName,
      legalName: fullName,
      email: cand.email || 'thomasdupkavich@gmail.com',
      phone,
      phoneFormatted,
      address: '12 Oak Avenue',
      city: city || 'Huntington Station',
      state: stateRaw || 'NY',
      zip: '11746',
      country: 'United States',
    },
    current: {
      title: 'Software Engineer',
      company: 'Zebra Technologies',
      location: 'Hauppauge, NY',
      startedYear: 2018,
      currentlyEmployed: true,
      yearsExperience: 6,
    },
    education: {
      degree: 'Bachelor of Science',
      field: 'Computer Programming and Information Systems',
      school: 'SUNY Farmingdale State College',
      location: 'Farmingdale, NY',
      graduationYear: 2018,
      graduationMonth: 12,
    },
    skills: [
      'C#', '.NET Core', 'ASP.NET MVC', 'ASP.NET Web API', 'Entity Framework',
      'SQL Server', 'T-SQL', 'REST APIs', 'JavaScript', 'jQuery', 'AJAX',
      'HTML5', 'CSS3', 'Razor', 'Kendo UI', 'Telerik Controls', 'Bootstrap',
      'Azure DevOps', 'Git', 'JIRA', 'Visual Studio', 'SSMS', 'Postman',
      'Agile', 'Scrum', 'SOLID', 'OOP', 'CI/CD', 'unit testing',
    ],
    authorization: {
      usWorkAuthorized: true,
      requiresSponsorshipNow: false,
      requiresSponsorshipFuture: false,
    },
    selfIdentification: {
      raceEthnicity: 'White',
      gender: null,
      veteranStatus: 'not a protected veteran',
      disabilityStatus: 'no',
      hispanicLatino: false,
    },
    preferences: {
      salaryMin: 120000,
      salaryTarget: 140000,
      salaryAsk: 140000,
      employmentType: 'full-time',
      rejectContract: true,
      remoteOk: true,
      relocate: false,
      locations: ['Remote (US)', 'Long Island, NY', 'Huntington Station, NY'],
      excludeNycOnsite: true,
      availability: 'two weeks after offer acceptance',
    },
    links: {
      linkedin: cand.linkedin || null,
      github: cand.github || null,
      portfolio: cand.portfolio_url || null,
      twitter: cand.twitter || null,
    },
    files: {
      resume: bestResume(),
      cv_md: existsSync(CV_MD) ? CV_MD : null,
    },
    submission: {
      requireExplicitApproval: true,   // submit only when queue item has submit_approved=true
    },
  };
  return cached;
}

export function bestResumePath() { return loadProfile().files.resume; }
export function profileDump() { return JSON.stringify(loadProfile(), null, 2); }
