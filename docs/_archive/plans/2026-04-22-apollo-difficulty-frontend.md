# Apollo Difficulty Choice — Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the difficulty-choice and problem-switching flows in the student UI: pre-handoff picker, post-Done picker, mid-problem "Switch problem", and "Restart this problem".

**Architecture:** Add a shared `DifficultyPicker` primitive. Add two new Next.js proxy routes (`/next`, `/restart_problem`). Extend `lib/apollo/api.ts` with typed clients, types, and a new `invalid_phase` error code. Move the `startSessionFromHoot` call from Hoot's `app/page.tsx` into a pre-handoff picker screen rendered inside `app/apollo/`: Hoot stashes the transcript in `sessionStorage` and redirects to `/apollo?pending=1` with no session yet; the Apollo page renders the picker and calls `/from_hoot` when the student picks.

**Tech Stack:** Next.js 15 App Router, React 19, TypeScript, Tailwind (v4), Framer Motion (installed). No unit test runner is configured in this repo — verification is manual (dev server + browser). Every task ends with an explicit browser-verification step.

**Repo:** `/Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui` (sibling of ai-ta-backend). Dev port 3001. Branch convention: never push to `main` (per repo `CLAUDE.md`).

**Branch for this work:** Create `apollo-v2-difficulty-ui` off the repo's current default. Do not merge to main without explicit user go-ahead.

**Requires backend slice:** The backend plan `2026-04-22-apollo-difficulty-backend.md` must be deployed to a reachable `AI_TA_API_BASE_URL` before manual QA can succeed end-to-end. The frontend can be implemented and type-checked without the backend being live; only manual QA is blocked.

**Spec:** `docs/superpowers/specs/2026-04-22-apollo-difficulty-choice-design.md` (in the ai-ta-backend repo, Sections 7).

---

## File Map

**Modified:**
- `lib/apollo/api.ts` — types, new clients, new `invalid_phase` error code, `attempt_id` in responses, `difficulty` param on `startSessionFromHoot`.
- `app/api/apollo/sessions/from_hoot/route.ts` — no code change needed (raw-body proxy already forwards `difficulty`); file re-read to confirm.
- `app/page.tsx` (Hoot) — `startApollo` stashes transcript, redirects to `/apollo?pending=1` without calling the backend.
- `app/apollo/page.tsx` — route component decides between the picker and the existing client when the `session` query param is absent.
- `app/apollo/ApolloPageClient.tsx` — mount switch + restart buttons, wire `nextProblem` + `restartProblem` handlers, pass post-Done picker into the report panel.
- `components/apollo/ApolloReportPanel.tsx` — Surface 2 (post-Done difficulty picker + "Next problem" button).

**Created:**
- `app/api/apollo/sessions/[id]/next/route.ts` — proxy.
- `app/api/apollo/sessions/[id]/restart_problem/route.ts` — proxy.
- `components/apollo/DifficultyPicker.tsx` — shared tri-tier picker primitive.
- `components/apollo/PreHandoffPicker.tsx` — Surface 1 (first-problem picker).
- `components/apollo/SwitchProblemButton.tsx` — Surface 3 (button + confirm modal).
- `components/apollo/RestartProblemButton.tsx` — Surface 4 (button + confirm modal).

---

## Task 1: Extend `lib/apollo/api.ts`

**Files:**
- Modify: `lib/apollo/api.ts`

- [ ] **Step 1: Extend the `ApolloErrorCode` union.**

Add `"invalid_phase"` to the union:

```typescript
export type ApolloErrorCode =
  | "parser_could_not_extract"
  | "filter_rejected"
  | "malformed_equation"
  | "no_matching_concept"
  | "pool_exhausted"
  | "session_frozen"
  | "invalid_phase"
  | "unknown";
```

- [ ] **Step 2: Add a `Difficulty` type.**

Above the `ApolloProblem` interface:

```typescript
export type Difficulty = "intro" | "standard" | "hard";

export const DIFFICULTIES: Difficulty[] = ["intro", "standard", "hard"];

export const DIFFICULTY_MULTIPLIERS: Record<Difficulty, number> = {
  intro: 1.0,
  standard: 1.5,
  hard: 2.0,
};

export const DIFFICULTY_LABELS: Record<Difficulty, string> = {
  intro: "Intro",
  standard: "Standard",
  hard: "Hard",
};

export const DIFFICULTY_DESCRIPTIONS: Record<Difficulty, string> = {
  intro: "Start here if a concept is brand new.",
  standard: "The usual workload — a fair test of what you know.",
  hard: "For when you want a challenge and more XP.",
};
```

- [ ] **Step 3: Update `startSessionFromHoot` to require `difficulty` and return `attempt_id`.**

Replace the existing function:

```typescript
export async function startSessionFromHoot(
  studentId: string,
  hootTranscript: string,
  difficulty: Difficulty,
): Promise<{
  session_id: number;
  problem: ApolloProblem;
  attempt_id: number;
}> {
  const res = await fetch("/api/apollo/sessions/from_hoot", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      student_id: studentId,
      hoot_transcript: hootTranscript,
      difficulty,
    }),
  });
  return (await _handle(res)) as {
    session_id: number;
    problem: ApolloProblem;
    attempt_id: number;
  };
}
```

- [ ] **Step 4: Add `nextProblem` and `restartProblem` clients.**

Below `retryProblem`:

```typescript
export async function nextProblem(
  sessionId: number,
  difficulty: Difficulty,
): Promise<{
  session_id: number;
  problem: ApolloProblem;
  attempt_id: number;
}> {
  const res = await fetch(`/api/apollo/sessions/${sessionId}/next`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ difficulty }),
  });
  return (await _handle(res)) as {
    session_id: number;
    problem: ApolloProblem;
    attempt_id: number;
  };
}

export async function restartProblem(sessionId: number): Promise<{ ok: boolean }> {
  const res = await fetch(`/api/apollo/sessions/${sessionId}/restart_problem`, {
    method: "POST",
  });
  return (await _handle(res)) as { ok: boolean };
}
```

- [ ] **Step 5: Verify typecheck.**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui && npx tsc --noEmit
```

Expected: FAIL — `app/page.tsx:589` calls `startSessionFromHoot(studentId, transcript)` with only two args. That's fixed in Task 5 and is the expected next failure. Everything else in the typecheck must pass.

- [ ] **Step 6: Commit.**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
git checkout -b apollo-v2-difficulty-ui
git add lib/apollo/api.ts
git commit -m "feat(apollo): typed clients for /next, /restart_problem, and difficulty"
git push -u origin apollo-v2-difficulty-ui
```

---

## Task 2: Add proxy routes for `/next` and `/restart_problem`

**Files:**
- Create: `app/api/apollo/sessions/[id]/next/route.ts`
- Create: `app/api/apollo/sessions/[id]/restart_problem/route.ts`

- [ ] **Step 1: Create the `/next` proxy, mirroring `/retry`.**

`app/api/apollo/sessions/[id]/next/route.ts`:

```typescript
export const runtime = 'nodejs';

export async function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const rawBackend = process.env.AI_TA_API_BASE_URL;
  const backend = rawBackend ? rawBackend.replace(/\/+$/, '') : '';
  if (!backend) {
    return new Response('AI_TA_API_BASE_URL missing', { status: 500 });
  }

  const body = await req.text();
  const authHeader = req.headers.get('authorization');
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authHeader) headers.Authorization = authHeader;
  const { id } = await ctx.params;
  const resp = await fetch(`${backend}/apollo/sessions/${encodeURIComponent(id)}/next`, {
    method: 'POST',
    headers,
    body,
  });

  return new Response(resp.body, {
    status: resp.status,
    headers: {
      'Content-Type': resp.headers.get('content-type') ?? 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}
```

- [ ] **Step 2: Create the `/restart_problem` proxy.**

`app/api/apollo/sessions/[id]/restart_problem/route.ts`:

```typescript
export const runtime = 'nodejs';

export async function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const rawBackend = process.env.AI_TA_API_BASE_URL;
  const backend = rawBackend ? rawBackend.replace(/\/+$/, '') : '';
  if (!backend) {
    return new Response('AI_TA_API_BASE_URL missing', { status: 500 });
  }

  const authHeader = req.headers.get('authorization');
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authHeader) headers.Authorization = authHeader;
  const { id } = await ctx.params;
  const resp = await fetch(
    `${backend}/apollo/sessions/${encodeURIComponent(id)}/restart_problem`,
    { method: 'POST', headers }
  );

  return new Response(resp.body, {
    status: resp.status,
    headers: {
      'Content-Type': resp.headers.get('content-type') ?? 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}
```

- [ ] **Step 3: Typecheck.**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui && npx tsc --noEmit
```

Expected: same failure as Task 1 step 5 (the two-arg `startSessionFromHoot` call). No new errors from these files.

- [ ] **Step 4: Commit.**

```bash
git add app/api/apollo/sessions/
git commit -m "feat(apollo): proxy routes for /next and /restart_problem"
git push
```

---

## Task 3: Build the shared `DifficultyPicker` primitive

**Files:**
- Create: `components/apollo/DifficultyPicker.tsx`

- [ ] **Step 1: Write the component.**

```tsx
"use client";

import {
  DIFFICULTIES,
  DIFFICULTY_DESCRIPTIONS,
  DIFFICULTY_LABELS,
  DIFFICULTY_MULTIPLIERS,
  type Difficulty,
} from "@/lib/apollo/api";

export interface DifficultyPickerProps {
  value: Difficulty | null;
  onChange: (d: Difficulty) => void;
  disabled?: boolean;
}

export default function DifficultyPicker({
  value,
  onChange,
  disabled,
}: DifficultyPickerProps) {
  return (
    <div className="apollo-difficulty-picker flex flex-col gap-2">
      {DIFFICULTIES.map((d) => {
        const selected = value === d;
        return (
          <button
            key={d}
            type="button"
            disabled={disabled}
            onClick={() => onChange(d)}
            aria-pressed={selected}
            className={[
              "text-left rounded-lg border px-4 py-3 transition",
              selected
                ? "border-amber-400 bg-amber-50"
                : "border-slate-200 bg-white hover:border-slate-300",
              disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer",
            ].join(" ")}
          >
            <div className="flex items-center justify-between">
              <span className="font-medium text-slate-900">
                {DIFFICULTY_LABELS[d]}
              </span>
              <span className="text-sm font-mono text-slate-600">
                ×{DIFFICULTY_MULTIPLIERS[d].toFixed(1)} XP
              </span>
            </div>
            <p className="mt-1 text-sm text-slate-600">
              {DIFFICULTY_DESCRIPTIONS[d]}
            </p>
          </button>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 2: Typecheck.**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui && npx tsc --noEmit
```

Expected: only the unchanged `app/page.tsx:589` error.

- [ ] **Step 3: Commit.**

```bash
git add components/apollo/DifficultyPicker.tsx
git commit -m "feat(apollo): DifficultyPicker shared primitive"
git push
```

---

## Task 4: Build the `PreHandoffPicker` (Surface 1)

**Files:**
- Create: `components/apollo/PreHandoffPicker.tsx`

- [ ] **Step 1: Write the component.**

```tsx
"use client";

import { useState } from "react";
import DifficultyPicker from "./DifficultyPicker";
import type { Difficulty } from "@/lib/apollo/api";

export interface PreHandoffPickerProps {
  onStart: (difficulty: Difficulty) => Promise<void>;
  disabled?: boolean;
  errorMessage?: string | null;
}

export default function PreHandoffPicker({
  onStart,
  disabled,
  errorMessage,
}: PreHandoffPickerProps) {
  const [choice, setChoice] = useState<Difficulty | null>(null);
  const [busy, setBusy] = useState(false);

  async function go() {
    if (!choice) return;
    setBusy(true);
    try {
      await onStart(choice);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="apollo-pre-handoff max-w-xl mx-auto py-12 px-6">
      <h1 className="text-2xl font-semibold text-slate-900">Ready to start teaching?</h1>
      <p className="mt-2 text-slate-600">
        Pick a difficulty to start. You can change it any time.
      </p>

      <div className="mt-6">
        <DifficultyPicker
          value={choice}
          onChange={setChoice}
          disabled={disabled || busy}
        />
      </div>

      {errorMessage ? (
        <p className="mt-4 text-sm text-red-600" role="alert">
          {errorMessage}
        </p>
      ) : null}

      <button
        type="button"
        onClick={go}
        disabled={!choice || disabled || busy}
        className="mt-6 inline-flex items-center rounded-md bg-amber-500 px-4 py-2 font-medium text-white disabled:opacity-50"
      >
        {busy ? "Starting…" : "Start teaching"}
      </button>
    </div>
  );
}
```

- [ ] **Step 2: Typecheck.**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui && npx tsc --noEmit
```

Expected: same `app/page.tsx` error, no new errors.

- [ ] **Step 3: Commit.**

```bash
git add components/apollo/PreHandoffPicker.tsx
git commit -m "feat(apollo): PreHandoffPicker — Surface 1"
git push
```

---

## Task 5: Rewire Hoot's `startApollo` to stash transcript and defer the backend call

**Files:**
- Modify: `app/page.tsx`

- [ ] **Step 1: Read the current `startApollo` function (lines ~583-600) to see state names.**

Already captured in this plan: the current call is

```typescript
const { session_id } = await startSessionFromHoot(studentId, transcript);
router.push(`/apollo?session=${session_id}`);
```

and uses `setApolloError` + `setApolloStarting`. Confirm by reading.

- [ ] **Step 2: Replace the body of `startApollo`.**

```typescript
const startApollo = async () => {
  setApolloError(null);
  setApolloStarting(true);
  try {
    const transcript = messages.map((m) => `${m.role}: ${m.content}`).join('\n');
    const studentId = session?.user_id ?? 'unknown';
    // Stash transcript + studentId for the Apollo-side picker screen.
    // No backend call yet — /apollo renders a difficulty picker and fires
    // startSessionFromHoot once the student picks.
    sessionStorage.setItem('apollo_pending_transcript', transcript);
    sessionStorage.setItem('apollo_pending_student_id', studentId);
    router.push('/apollo?pending=1');
  } catch (err) {
    setApolloError((err as Error).message);
  } finally {
    setApolloStarting(false);
  }
};
```

Remove the `ApolloApiError` import if `startSessionFromHoot` and `ApolloApiError` are no longer referenced in `app/page.tsx` after this change. Leave `startSessionFromHoot` unimported.

- [ ] **Step 3: Typecheck.**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui && npx tsc --noEmit
```

Expected: previous `app/page.tsx:589` error is gone. Any new error likely reflects unused imports — fix inline.

- [ ] **Step 4: Browser verification.**

```bash
npm run dev
```

Open `http://localhost:3001`, sign in, chat a little with Hoot, click "Teach Apollo". Verify the URL becomes `/apollo?pending=1` and the page shows whatever the old Apollo page rendered when no session was loaded (an error or blank — this is fixed in the next task). Verify `sessionStorage` contains `apollo_pending_transcript` and `apollo_pending_student_id` (DevTools → Application → Session Storage).

- [ ] **Step 5: Commit.**

```bash
git add app/page.tsx
git commit -m "feat(apollo): stash transcript, defer from_hoot call to Apollo page"
git push
```

---

## Task 6: Render `PreHandoffPicker` on `/apollo?pending=1`

**Files:**
- Modify: `app/apollo/ApolloPageClient.tsx`

- [ ] **Step 1: Handle three states in `ApolloPageClient`.**

The page currently reads `session=<id>` from the query string. Add a second state: if `session` is absent AND `pending=1` is present, render `PreHandoffPicker` and, on pick, call `startSessionFromHoot`, then update the URL to `/apollo?session=<id>` via `router.replace`.

Near the top of `ApolloPageClient`, after `const sessionId = Number(searchParams.get("session"));` add:

```typescript
const pending = searchParams.get("pending") === "1";
const [pendingError, setPendingError] = useState<string | null>(null);
```

Import `startSessionFromHoot`, `nextProblem`, `restartProblem`, `ApolloApiError`, and `DifficultyPicker` types / functions as needed:

```typescript
import {
  ApolloApiError,
  endSession,
  finishTeaching,
  getSessionState,
  getStudentProgress,
  nextProblem,
  restartProblem,
  retryProblem,
  startSessionFromHoot,
  type ApolloKG,
  type ApolloSessionState,
  type Difficulty,
  type DoneResponse,
  type StudentProgress,
} from "@/lib/apollo/api";
import PreHandoffPicker from "@/components/apollo/PreHandoffPicker";
```

- [ ] **Step 2: Add the pre-handoff handler.**

Inside the component, above the `useEffect`:

```typescript
async function handlePreHandoffStart(difficulty: Difficulty) {
  setPendingError(null);
  const transcript = sessionStorage.getItem("apollo_pending_transcript") ?? "";
  const studentId = sessionStorage.getItem("apollo_pending_student_id") ?? "unknown";
  try {
    const res = await startSessionFromHoot(studentId, transcript, difficulty);
    sessionStorage.removeItem("apollo_pending_transcript");
    sessionStorage.removeItem("apollo_pending_student_id");
    router.replace(`/apollo?session=${res.session_id}`);
  } catch (err) {
    if (err instanceof ApolloApiError && err.errorCode === "no_matching_concept") {
      setPendingError("Apollo doesn't cover this topic yet.");
    } else {
      setPendingError((err as Error).message);
    }
  }
}
```

- [ ] **Step 3: Render the picker when `pending && !sessionId`.**

Near the top of the JSX, before the existing render branches:

```tsx
if (pending && !sessionId) {
  return (
    <div className="apollo-container">
      {returnLink}
      <PreHandoffPicker
        onStart={handlePreHandoffStart}
        errorMessage={pendingError}
      />
    </div>
  );
}
```

- [ ] **Step 4: Typecheck.**

```bash
npx tsc --noEmit
```

Expected: pass.

- [ ] **Step 5: Browser verification.**

With `npm run dev` running, click "Teach Apollo" in Hoot. Confirm the picker shows up. Pick "Standard". Confirm the backend is called (Network tab) with `{difficulty: "standard"}` and the URL changes to `/apollo?session=<id>`. Confirm the Apollo session loads and the first problem is shown.

If the backend isn't running, a 500 or network error surfaces as a red inline message — acceptable; the UI path still works.

- [ ] **Step 6: Commit.**

```bash
git add app/apollo/ApolloPageClient.tsx
git commit -m "feat(apollo): render PreHandoffPicker when /apollo is pending"
git push
```

---

## Task 7: Build `SwitchProblemButton` (Surface 3)

**Files:**
- Create: `components/apollo/SwitchProblemButton.tsx`

- [ ] **Step 1: Write the component.**

```tsx
"use client";

import { useState } from "react";
import DifficultyPicker from "./DifficultyPicker";
import type { Difficulty } from "@/lib/apollo/api";

export interface SwitchProblemButtonProps {
  onSwitch: (difficulty: Difficulty) => Promise<void>;
  disabled?: boolean;
}

export default function SwitchProblemButton({
  onSwitch,
  disabled,
}: SwitchProblemButtonProps) {
  const [open, setOpen] = useState(false);
  const [choice, setChoice] = useState<Difficulty | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    if (!choice) return;
    setBusy(true);
    setError(null);
    try {
      await onSwitch(choice);
      setOpen(false);
      setChoice(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen(true)}
        className="apollo-secondary-btn"
      >
        Switch problem
      </button>

      {open ? (
        <div className="apollo-modal-overlay fixed inset-0 bg-black/40 flex items-center justify-center z-40">
          <div className="apollo-modal bg-white rounded-xl shadow-xl max-w-md w-full p-6">
            <h2 className="text-lg font-semibold text-slate-900">
              Switch to a different problem?
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              This problem won&rsquo;t be graded. Pick a new difficulty to
              start over with a different problem.
            </p>

            <div className="mt-4">
              <DifficultyPicker
                value={choice}
                onChange={setChoice}
                disabled={busy}
              />
            </div>

            {error ? (
              <p className="mt-3 text-sm text-red-600" role="alert">
                {error}
              </p>
            ) : null}

            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  setChoice(null);
                  setError(null);
                }}
                disabled={busy}
                className="apollo-ghost-btn"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirm}
                disabled={!choice || busy}
                className="apollo-danger-btn"
              >
                {busy ? "Switching…" : "Switch problem"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
```

- [ ] **Step 2: Typecheck.**

```bash
npx tsc --noEmit
```

Expected: pass.

- [ ] **Step 3: Commit.**

```bash
git add components/apollo/SwitchProblemButton.tsx
git commit -m "feat(apollo): SwitchProblemButton — Surface 3"
git push
```

---

## Task 8: Build `RestartProblemButton` (Surface 4)

**Files:**
- Create: `components/apollo/RestartProblemButton.tsx`

- [ ] **Step 1: Write the component.**

```tsx
"use client";

import { useState } from "react";

export interface RestartProblemButtonProps {
  onRestart: () => Promise<void>;
  disabled?: boolean;
}

export default function RestartProblemButton({
  onRestart,
  disabled,
}: RestartProblemButtonProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await onRestart();
      setOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen(true)}
        className="apollo-secondary-btn"
      >
        Restart this problem
      </button>

      {open ? (
        <div className="apollo-modal-overlay fixed inset-0 bg-black/40 flex items-center justify-center z-40">
          <div className="apollo-modal bg-white rounded-xl shadow-xl max-w-md w-full p-6">
            <h2 className="text-lg font-semibold text-slate-900">
              Restart this problem?
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              Wipe your teaching for this problem and start over? Same
              problem, same difficulty.
            </p>

            {error ? (
              <p className="mt-3 text-sm text-red-600" role="alert">
                {error}
              </p>
            ) : null}

            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  setError(null);
                }}
                disabled={busy}
                className="apollo-ghost-btn"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirm}
                disabled={busy}
                className="apollo-danger-btn"
              >
                {busy ? "Restarting…" : "Restart"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
```

- [ ] **Step 2: Typecheck.**

```bash
npx tsc --noEmit
```

Expected: pass.

- [ ] **Step 3: Commit.**

```bash
git add components/apollo/RestartProblemButton.tsx
git commit -m "feat(apollo): RestartProblemButton — Surface 4"
git push
```

---

## Task 9: Surface 2 — post-Done picker in `ApolloReportPanel`

**Files:**
- Modify: `components/apollo/ApolloReportPanel.tsx`

- [ ] **Step 1: Read the component to find where the current "Teach more and retry" button lives.**

(No command — manual read.)

- [ ] **Step 2: Add picker + "Next problem" props and UI.**

Extend the component's props to accept:

```typescript
onNextProblem: (difficulty: Difficulty) => Promise<void>;
defaultDifficulty: Difficulty;
```

Inside the component, add state:

```typescript
const [nextChoice, setNextChoice] = useState<Difficulty>(defaultDifficulty);
const [nextBusy, setNextBusy] = useState(false);
const [nextError, setNextError] = useState<string | null>(null);
```

Below the rubric card (and the existing "Teach more and retry" button), add:

```tsx
<section className="apollo-next-problem mt-6">
  <h3 className="text-base font-semibold text-slate-900">Move on to a new problem</h3>
  <p className="mt-1 text-sm text-slate-600">
    Pick the difficulty of your next problem — you can always switch later.
  </p>
  <div className="mt-3">
    <DifficultyPicker value={nextChoice} onChange={setNextChoice} disabled={nextBusy} />
  </div>
  {nextError ? (
    <p className="mt-2 text-sm text-red-600" role="alert">{nextError}</p>
  ) : null}
  <button
    type="button"
    onClick={async () => {
      setNextBusy(true);
      setNextError(null);
      try {
        await onNextProblem(nextChoice);
      } catch (e) {
        setNextError((e as Error).message);
      } finally {
        setNextBusy(false);
      }
    }}
    disabled={nextBusy}
    className="mt-4 apollo-primary-btn"
  >
    {nextBusy ? "Loading…" : "Next problem"}
  </button>
</section>
```

Import at the top:

```typescript
import { useState } from "react";
import DifficultyPicker from "./DifficultyPicker";
import type { Difficulty } from "@/lib/apollo/api";
```

- [ ] **Step 3: Typecheck.**

```bash
npx tsc --noEmit
```

Expected: FAIL on `ApolloPageClient.tsx` — it doesn't yet pass `onNextProblem` / `defaultDifficulty`. That's fixed in Task 10 and is the expected next failure.

- [ ] **Step 4: Commit.**

```bash
git add components/apollo/ApolloReportPanel.tsx
git commit -m "feat(apollo): post-Done difficulty picker in report panel"
git push
```

---

## Task 10: Wire Switch + Restart buttons and post-Done picker into `ApolloPageClient`

**Files:**
- Modify: `app/apollo/ApolloPageClient.tsx`

- [ ] **Step 1: Add handlers for `/next` and `/restart_problem`.**

Inside the component, below the existing `handleDone`:

```typescript
async function handleNextProblem(difficulty: Difficulty) {
  if (!sessionId) return;
  setBusy(true);
  setError(null);
  try {
    const res = await nextProblem(sessionId, difficulty);
    // Reload session state to pick up the new problem + empty KG.
    const s = await getSessionState(res.session_id);
    setState(s);
    setKg(s.kg);
    setReport(null);
  } catch (e) {
    setError(e as Error);
  } finally {
    setBusy(false);
  }
}

async function handleRestartProblem() {
  if (!sessionId) return;
  setBusy(true);
  setError(null);
  try {
    await restartProblem(sessionId);
    const s = await getSessionState(sessionId);
    setState(s);
    setKg(s.kg);
    setReport(null);
  } catch (e) {
    setError(e as Error);
  } finally {
    setBusy(false);
  }
}
```

- [ ] **Step 2: Mount switch + restart buttons in the session header area.**

Find the existing header or "End session" button location and add nearby:

```tsx
{state && state.phase !== "SOLVING" ? (
  <div className="apollo-session-controls flex gap-2">
    <SwitchProblemButton
      onSwitch={handleNextProblem}
      disabled={busy}
    />
    <RestartProblemButton
      onRestart={handleRestartProblem}
      disabled={busy}
    />
  </div>
) : null}
```

Import at the top:

```typescript
import SwitchProblemButton from "@/components/apollo/SwitchProblemButton";
import RestartProblemButton from "@/components/apollo/RestartProblemButton";
```

- [ ] **Step 3: Pass the new props into `ApolloReportPanel`.**

Find where `<ApolloReportPanel ... />` is rendered and add:

```tsx
<ApolloReportPanel
  {/* existing props */}
  onNextProblem={handleNextProblem}
  defaultDifficulty={(state?.problem?.difficulty as Difficulty | undefined) ?? "intro"}
/>
```

- [ ] **Step 4: Typecheck.**

```bash
npx tsc --noEmit
```

Expected: pass.

- [ ] **Step 5: Browser verification — happy path.**

```bash
npm run dev
```

With the backend live:
1. Hoot → "Teach Apollo" → pick `intro` → land on a problem.
2. Click "Switch problem" → pick `standard` → confirm → verify the URL stays the same but the problem changes, KG is empty, phase is TEACHING. Check DevTools Network: the `POST /api/apollo/sessions/{id}/next` response carries a new `attempt_id`.
3. Teach a couple of messages → click "Restart this problem" → confirm → verify KG is empty and messages are gone.
4. Teach something, hit Done → on the report, scroll to "Move on to a new problem" → pick `hard` → click "Next problem" → verify new problem loads at hard difficulty.

- [ ] **Step 6: Browser verification — errors.**

With the backend live:
1. With a cluster that only has `intro` problems (current fluid_mechanics bank), attempt all intro problems, then click "Switch problem" and pick `intro` again → confirm a red error message "No more unattempted problems at intro." appears in the modal. Pick another difficulty → confirm success.

- [ ] **Step 7: Commit.**

```bash
git add app/apollo/ApolloPageClient.tsx
git commit -m "feat(apollo): wire switch/restart/next into Apollo client"
git push
```

---

## Task 11: Full-page QA pass

- [ ] **Step 1: Lint + typecheck the full repo.**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
npm run lint
npx tsc --noEmit
```

Expected: both pass with no errors.

- [ ] **Step 2: Smoke the dev build.**

```bash
npm run build
```

Expected: build succeeds with no type errors.

- [ ] **Step 3: Manual regression QA of existing flows.**

Run `npm run dev` and confirm:

- Hoot chat still works (messages send, citations render).
- Student can open an existing Apollo session URL (`/apollo?session=<id>` direct nav) and the session loads normally with KG + messages for the *current* attempt.
- `/retry` still works from the report panel (teach-more path).
- `/end session` still ends the session.
- Progress card (XP + level) still renders on the Apollo page.

- [ ] **Step 4: Final commit (if any fixes).**

```bash
git add -u
git commit -m "chore(apollo): lint + build fixes after difficulty-UI work"
git push
```

---

## Notes for the Executor

- **No new packages.** Everything is already in `package.json`.
- **Branch is `apollo-v2-difficulty-ui`.** Never push to `main` per repo `CLAUDE.md`.
- **Tailwind classnames** in this plan are illustrative — if the repo has its own design tokens (inspect sibling components for class naming), prefer matching those. `apollo-*` class names are used throughout as placeholder hooks; replace with actual ones from the existing components or accept the plan's names and add styles.
- **TSX for modals** is built with plain divs + overlay. If the repo already uses a Dialog component (check Framer Motion / Radix / shadcn), prefer the existing primitive.
- **No unit test runner.** All verification is manual via `npm run dev` + the browser. Do not skip the browser-verification steps — the plan depends on them for confidence.
- **Backend dependency:** manual QA needs the backend plan's endpoints live on whatever `AI_TA_API_BASE_URL` points at. If the backend isn't deployed yet, the frontend still compiles and renders the picker screens, but the live flow will fail at the first backend call.
