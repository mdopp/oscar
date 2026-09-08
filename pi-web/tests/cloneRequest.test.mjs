// `node --test pi-web/tests/`
//
// CI does not run this (ci.yml watches Python paths only, and image builds do
// not run tests), so it is a local gate — the same one the builder runs before
// committing a change to the clone plugin.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  deriveProjectName,
  describeCloneRequest,
  redactCredentials,
  validateRemoteUrl,
} from "../plugins/solaris-clone/browser/cloneRequest.js";

test("accepts the four forms claude-dev accepts", () => {
  for (const url of [
    "https://github.com/mdopp/solarisbay.git",
    "http://gitea.example.lan/mdopp/solarisbay",
    "ssh://git@github.com/mdopp/solarisbay.git",
    "git@github.com:mdopp/solarisbay.git",
  ]) {
    assert.equal(validateRemoteUrl(url).ok, true, url);
  }
});

test("refuses every other form", () => {
  for (const url of [
    "file:///etc/passwd",
    "/workspace/etwas-anderes",
    "../../etc",
    "javascript:alert(1)",
    "github.com/mdopp/solarisbay",
    "ftp://example.com/repo.git",
    "--upload-pack=/bin/sh",
    "-oProxyCommand=id",
    "https://github.com",
    "https://github.com/",
    "git@github.com:",
    "",
    "   ",
  ]) {
    const result = validateRemoteUrl(url);
    assert.equal(result.ok, false, `expected refusal for ${JSON.stringify(url)}`);
    assert.match(result.message, /\S/u);
  }
});

test("refuses an address carrying whitespace or a control character", () => {
  assert.equal(validateRemoteUrl("https://github.com/a b.git").ok, false);
  assert.equal(validateRemoteUrl("https://github.com/a\u0007b.git").ok, false);
  assert.equal(validateRemoteUrl("https://github.com/a\tb.git").ok, false);
  // A hyphen inside the address is ordinary and must survive.
  assert.equal(validateRemoteUrl("https://github.com/mdopp/pi-web-thing.git").ok, true);
});

test("derives the folder name from the last path segment without .git", () => {
  const cases = [
    ["https://github.com/mdopp/solarisbay.git", "solarisbay"],
    ["https://github.com/mdopp/solarisbay", "solarisbay"],
    ["https://github.com/mdopp/solarisbay/", "solarisbay"],
    ["git@github.com:mdopp/pi-web.git", "pi-web"],
    ["ssh://git@example.lan/srv/git/haus.notizen.git", "haus.notizen"],
  ];
  for (const [url, expected] of cases) {
    const described = describeCloneRequest(url);
    assert.equal(described.ok, true, url);
    assert.equal(described.name, expected);
    assert.equal(described.path, `/workspace/${expected}`);
  }
});

test("refuses a derived name that would escape the workspace or break the token step", () => {
  for (const path of ["", "/", "..", "../..", "/a/./", "/a/-leading"]) {
    assert.equal(deriveProjectName(path).ok, false, JSON.stringify(path));
  }
  assert.equal(deriveProjectName(`/a/${"x".repeat(65)}`).ok, false);
  assert.equal(deriveProjectName(`/a/${"x".repeat(64)}`).ok, true);
});

test("a derived name never contains a path separator", () => {
  const described = describeCloneRequest("https://github.com/mdopp/deep/nested/repo.git");
  assert.equal(described.ok, true);
  assert.equal(described.name, "repo");
  assert.equal(described.path, "/workspace/repo");
});

test("the workspace root is honoured", () => {
  const described = describeCloneRequest("https://github.com/mdopp/x.git", "/srv/code/");
  assert.equal(described.path, "/srv/code/x");
});

test("credentials are stripped from anything shown to a person", () => {
  assert.equal(
    redactCredentials("fatal: could not read https://x-access-token:ghp_secret@github.com/a.git"),
    "fatal: could not read https://***@github.com/a.git",
  );
  assert.equal(redactCredentials("fatal: repository not found"), "fatal: repository not found");
});
