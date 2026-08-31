#!/usr/bin/env node

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';

const SHA_RE = /^[0-9a-f]{40}$/;
const TAG_RE = /^v([0-9]+)\.([0-9]+)\.([0-9]+)$/;
const DATE_RE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;
const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

function fail(message) {
  throw new Error(message);
}

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name?.startsWith('--') || value === undefined) {
      fail(`invalid argument list near ${name || '<end>'}`);
    }
    result[name.slice(2)] = value;
  }
  return result;
}

function requireValue(args, name, pattern) {
  const value = args[name];
  if (!value || (pattern && !pattern.test(value))) {
    fail(`invalid --${name}`);
  }
  return value;
}

function git(cwd, args) {
  return execFileSync('git', args, {cwd, encoding: 'utf8'}).trim();
}

function pluginOptions(config, name) {
  for (const entry of config.plugins || []) {
    if (Array.isArray(entry) && entry[0] === name) {
      return entry[1] || {};
    }
    if (entry === name) {
      return {};
    }
  }
  return {};
}

function versionTuple(tag) {
  const match = TAG_RE.exec(tag);
  if (!match) {
    fail(`unsupported release tag: ${tag}`);
  }
  return match.slice(1).map(Number);
}

function compareVersions(left, right) {
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) {
      return left[index] - right[index];
    }
  }
  return 0;
}

function nextVersion(previous, releaseType) {
  const [major, minor, patch] = previous;
  if (releaseType === 'major') return [major + 1, 0, 0];
  if (releaseType === 'minor') return [major, minor + 1, 0];
  if (releaseType === 'patch') return [major, minor, patch + 1];
  fail(`unsupported release type: ${releaseType || 'none'}`);
}

function readCommits(cwd, baselineTag, productSha) {
  const raw = execFileSync(
    'git',
    ['log', '--format=%H%x00%s%x00%b%x00%cI%x00==END==', `${baselineTag}..${productSha}`],
    {cwd, encoding: 'utf8'},
  );
  return raw.split('\u0000==END==\n').filter(Boolean).map((entry) => {
    const [hash, subject, body, committerDate] = entry.split('\u0000');
    return {
      hash,
      subject,
      message: [subject, body].filter(Boolean).join('\n\n'),
      committerDate,
    };
  });
}

async function apiRequest(repo, endpoint, {method = 'GET', body, allowNotFound = false} = {}) {
  const token = process.env.GH_TOKEN || process.env.GITHUB_TOKEN;
  if (!token) fail('GH_TOKEN is required');
  const apiRoot = (process.env.GITHUB_API_URL || 'https://api.github.com').replace(/\/$/, '');
  const response = await fetch(`${apiRoot}/repos/${repo}${endpoint}`, {
    method,
    headers: {
      Accept: 'application/vnd.github+json',
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (allowNotFound && response.status === 404) return null;
  const text = await response.text();
  if (!response.ok) {
    fail(`GitHub API ${method} ${endpoint} failed with HTTP ${response.status}: ${text}`);
  }
  return text ? JSON.parse(text) : {};
}

function refObject(payload, label) {
  const object = payload?.object;
  if (!object || !SHA_RE.test(object.sha || '')) fail(`${label} ref is invalid`);
  return object;
}

async function remoteTagTarget(repo, tag) {
  const ref = await apiRequest(repo, `/git/ref/tags/${encodeURIComponent(tag)}`);
  const object = refObject(ref, tag);
  if (object.type !== 'tag') return object.sha;
  const annotated = await apiRequest(repo, `/git/tags/${object.sha}`);
  if (annotated?.object?.type !== 'commit' || !SHA_RE.test(annotated.object.sha || '')) {
    fail(`annotated tag ${tag} must point directly to a commit`);
  }
  return annotated.object.sha;
}

function releaseMatches(release, {tag, notes}) {
  return release?.tag_name === tag
    && release?.name === tag
    && release?.body === notes
    && release?.draft === false
    && release?.prerelease === false;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const cwd = path.resolve(requireValue(args, 'repo-root'));
  const repo = requireValue(args, 'repo', REPO_RE);
  const productSha = requireValue(args, 'product-sha', SHA_RE);
  const releaseSha = requireValue(args, 'release-sha', SHA_RE);
  const tag = requireValue(args, 'tag', TAG_RE);
  const releaseDate = requireValue(args, 'release-date', DATE_RE);
  const output = path.resolve(requireValue(args, 'output'));

  if (git(cwd, ['rev-parse', 'HEAD']) !== releaseSha) fail('checked out HEAD does not match release SHA');
  if (git(cwd, ['rev-parse', `${tag}^{commit}`]) !== productSha) fail('local tag does not point to product SHA');

  const mainRef = await apiRequest(repo, '/git/ref/heads/main');
  if (refObject(mainRef, 'main').sha !== releaseSha) fail('public main moved from release SHA');
  if (await remoteTagTarget(repo, tag) !== productSha) fail('remote tag does not point to product SHA');

  const targetVersion = versionTuple(tag);
  const reachableTags = git(cwd, ['tag', '--merged', productSha, '--list', 'v*'])
    .split('\n')
    .filter(Boolean)
    .filter((candidate) => TAG_RE.test(candidate));
  const previousTags = reachableTags
    .filter((candidate) => candidate !== tag && compareVersions(versionTuple(candidate), targetVersion) < 0)
    .sort((left, right) => compareVersions(versionTuple(right), versionTuple(left)));
  if (previousTags.length === 0) fail('orphan release repair requires a previous semantic version tag');
  const baselineTag = previousTags[0];
  const baselineVersion = versionTuple(baselineTag);

  const config = JSON.parse(fs.readFileSync(path.join(cwd, '.releaserc.json'), 'utf8'));
  const analyzerPath = pathToFileURL(path.join(cwd, 'node_modules/@semantic-release/commit-analyzer/index.js'));
  const notesPath = pathToFileURL(path.join(cwd, 'node_modules/@semantic-release/release-notes-generator/index.js'));
  const {analyzeCommits} = await import(analyzerPath);
  const {generateNotes} = await import(notesPath);
  const commits = readCommits(cwd, baselineTag, productSha);
  const context = {
    cwd,
    env: {},
    commits,
    lastRelease: {
      version: baselineTag.slice(1),
      gitTag: baselineTag,
      gitHead: git(cwd, ['rev-list', '-n', '1', baselineTag]),
    },
    nextRelease: {
      version: tag.slice(1),
      gitTag: tag,
      gitHead: productSha,
    },
    options: {
      repositoryUrl: `https://github.com/${repo}.git`,
      tagFormat: config.tagFormat || 'v${version}',
    },
    logger: {log() {}, error() {}, warn() {}},
  };
  const releaseType = await analyzeCommits(
    pluginOptions(config, '@semantic-release/commit-analyzer'),
    context,
  );
  const calculatedVersion = nextVersion(baselineVersion, releaseType).join('.');
  if (`v${calculatedVersion}` !== tag) {
    fail(`orphan tag ${tag} does not match calculated release v${calculatedVersion}`);
  }
  context.nextRelease.type = releaseType;
  const generatedNotes = await generateNotes(
    pluginOptions(config, '@semantic-release/release-notes-generator'),
    context,
  );
  if (!/\([0-9]{4}-[0-9]{2}-[0-9]{2}\)/.test(generatedNotes)) {
    fail('generated release notes have no release date header');
  }
  const notes = generatedNotes.replace(
    /\([0-9]{4}-[0-9]{2}-[0-9]{2}\)/,
    `(${releaseDate})`,
  );
  if (!notes) fail('generated release notes are empty');

  let release = await apiRequest(repo, `/releases/tags/${encodeURIComponent(tag)}`, {allowNotFound: true});
  let created = false;
  if (release === null) {
    release = await apiRequest(repo, '/releases', {
      method: 'POST',
      body: {
        tag_name: tag,
        target_commitish: productSha,
        name: tag,
        body: notes,
        draft: false,
        prerelease: false,
        generate_release_notes: false,
      },
    });
    created = true;
  }
  if (!releaseMatches(release, {tag, notes})) fail('GitHub Release metadata conflicts with repair output');
  if (await remoteTagTarget(repo, tag) !== productSha) fail('release tag target changed during repair');

  const result = {
    ok: true,
    created,
    tag,
    version: tag.slice(1),
    product_sha: productSha,
    release_sha: releaseSha,
    baseline_tag: baselineTag,
    release_date: releaseDate,
    notes_digest: `sha256:${crypto.createHash('sha256').update(notes).digest('hex')}`,
    github_release_url: release.html_url,
  };
  fs.writeFileSync(output, `${JSON.stringify(result, null, 2)}\n`, 'utf8');
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.message || error}\n`);
  process.exitCode = 1;
});
