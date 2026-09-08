// `node --test pi-web/tests/cloneProvider.test.mjs`
//
// Drives the real workspace provider against a faked host `execFile`, so the
// argv git actually receives — and the refusals that must never reach git at
// all — are asserted rather than assumed.

import assert from "node:assert/strict";
import { mkdtemp, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { cloneFailure, createCloneWorkspaceProvider, workspaceRoot } from "../plugins/solaris-clone/server-plugin.js";

const CLONE_URL = "https://github.com/mdopp/solarisbay.git";

function hostStub(results = {}) {
  const calls = [];
  return {
    calls,
    execFile: async (request) => {
      calls.push(request);
      const outcome = results[request.file] ?? { exitCode: 0, stdout: "", stderr: "" };
      return { signal: null, stdoutTruncated: false, stderrTruncated: false, ...outcome };
    },
  };
}

async function workspace() {
  return mkdtemp(join(tmpdir(), "solaris-clone-"));
}

test("claims the workspace root and nothing below it", async () => {
  const root = await workspace();
  const provider = createCloneWorkspaceProvider(hostStub(), root);
  assert.equal(await provider.probe({ id: "1", name: "w", path: root }), "claim");
  assert.equal(await provider.probe({ id: "2", name: "r", path: join(root, "repo") }), "pass");
  assert.equal(await provider.probe({ id: "3", name: "o", path: "/srv/andere" }), "pass");
});

test("lists one workspace carrying the flag the panel keys off", async () => {
  const root = await workspace();
  const provider = createCloneWorkspaceProvider(hostStub(), root);
  const listed = await provider.list({ id: "1", name: "w", path: root });
  assert.equal(listed.length, 1);
  assert.equal(listed[0].path, root);
  assert.equal(listed[0].isMain, true);
  assert.equal(listed[0].publicMetadata.clonesProjects, true);
});

test("a refused address never reaches git", async () => {
  const root = await workspace();
  const host = hostStub();
  const provider = createCloneWorkspaceProvider(host, root);
  const answer = await provider.request({
    operation: "clone",
    input: { url: "file:///etc/passwd" },
    signal: new AbortController().signal,
  });
  assert.equal(answer.ok, false);
  assert.equal(host.calls.length, 0);
});

test("an existing target is refused and nothing is overwritten", async () => {
  const root = await workspace();
  await mkdir(join(root, "solarisbay"));
  const host = hostStub();
  const provider = createCloneWorkspaceProvider(host, root);
  const answer = await provider.request({
    operation: "clone",
    input: { url: CLONE_URL },
    signal: new AbortController().signal,
  });
  assert.equal(answer.ok, false);
  assert.match(answer.message, /solarisbay/u);
  assert.equal(host.calls.length, 0, "git must not run when the target already exists");
});

test("a clone runs git with the address as an argument and then mints the project token", async () => {
  const root = await workspace();
  const host = hostStub();
  const provider = createCloneWorkspaceProvider(host, root);
  const answer = await provider.request({
    operation: "clone",
    input: { url: CLONE_URL },
    signal: new AbortController().signal,
  });
  assert.equal(answer.ok, true);
  assert.equal(answer.name, "solarisbay");
  assert.equal(answer.path, join(root, "solarisbay"));
  assert.equal(answer.token, true);

  const [gitCall, projectCall] = host.calls;
  assert.equal(gitCall.file, "git");
  assert.deepEqual(gitCall.args, ["clone", "--", CLONE_URL, join(root, "solarisbay")]);
  assert.equal(gitCall.env.GIT_TERMINAL_PROMPT, "0");
  assert.deepEqual(projectCall.args, ["add", "solarisbay"]);
});

test("a failed clone reports in German and skips the token step", async () => {
  const root = await workspace();
  const host = hostStub({
    git: { exitCode: 128, stdout: "", stderr: "fatal: could not read Username for 'https://github.com'" },
  });
  const provider = createCloneWorkspaceProvider(host, root);
  const answer = await provider.request({
    operation: "clone",
    input: { url: CLONE_URL },
    signal: new AbortController().signal,
  });
  assert.equal(answer.ok, false);
  assert.match(answer.message, /PI_WEB_GIT_TOKEN/u);
  assert.equal(host.calls.length, 1, "the token step must not run for a clone that failed");
});

test("a clone that works but has no ServiceBay token says so instead of failing", async () => {
  const root = await workspace();
  const host = hostStub({
    "pi-web-project": { exitCode: 1, stdout: "", stderr: "this container holds no ServiceBay token" },
  });
  const provider = createCloneWorkspaceProvider(host, root);
  const answer = await provider.request({
    operation: "clone",
    input: { url: CLONE_URL },
    signal: new AbortController().signal,
  });
  assert.equal(answer.ok, true);
  assert.equal(answer.token, false);
  assert.match(answer.tokenNote, /ServiceBay/u);
});

test("git output shown to a person never carries the token", () => {
  const failure = cloneFailure({
    exitCode: 128,
    stdout: "",
    stderr: "fatal: unable to access 'https://x-access-token:ghp_supersecret@github.com/a.git/'",
  });
  assert.doesNotMatch(failure.detail, /ghp_supersecret/u);
  assert.match(failure.detail, /\*\*\*@github\.com/u);
});

test("an unknown operation is refused", async () => {
  const provider = createCloneWorkspaceProvider(hostStub(), await workspace());
  await assert.rejects(
    () => provider.request({ operation: "delete", input: {}, signal: new AbortController().signal }),
    /Unbekannter Vorgang/u,
  );
});

test("the workspace root follows the environment, defaulting to /workspace", () => {
  assert.equal(workspaceRoot({}), "/workspace");
  assert.equal(workspaceRoot({ PI_WEB_WORKSPACE: "" }), "/workspace");
  assert.equal(workspaceRoot({ PI_WEB_WORKSPACE: "/srv/code" }), "/srv/code");
});
