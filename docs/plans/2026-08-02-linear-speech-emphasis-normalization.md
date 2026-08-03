# Linear Speech Emphasis Normalization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make TTS emphasis normalization linear-time for the maximum companion response while preserving existing speech behavior.

**Architecture:** Replace backtracking substitutions with bounded line-scoped forward scans for strong and ordinary emphasis. Each scan tracks one pending opener and removes only completed pairs, preserving the existing strong-then-ordinary normalization order in linear time.

**Tech Stack:** Python 3.13, pytest, Ruff

---

### Task 1: Add the adversarial regression

**Files:**
- Modify: `api/tests/test_game_companion_speech_text.py`

**Step 1: Write the failing test**

Add a test that normalizes `" **a" * 16_000`, asserts the literal output is
unchanged, and asserts completion within a deliberately broad one-second
ceiling.

**Step 2: Run the test to verify it fails**

Run:

```bash
DATABASE_URL=postgresql://test:test@localhost/test \
REDIS_URL=redis://localhost:6379 \
/tmp/dograh-review-venv/bin/python -m pytest \
  api/tests/test_game_companion_speech_text.py::test_normalize_speech_text_is_linear_for_unmatched_markers \
  -q --capture=fd
```

Expected: FAIL because the current regex takes far longer than one second.

### Task 2: Implement the linear parser

**Files:**
- Modify: `api/services/game_companion/speech_text.py`
- Test: `api/tests/test_game_companion_speech_text.py`

**Step 1: Replace the regex substitutions**

Implement bounded forward scans that:

1. treats escaped asterisks and intraword asterisks as literal;
2. scans strong (`**`) before ordinary (`*`) emphasis;
3. tracks at most one pending opener per scan within one line;
4. removes only marker positions belonging to completed pairs; and
5. flushes unmatched openers unchanged at a newline or end of input.

**Step 2: Run focused tests to verify GREEN**

Run:

```bash
DATABASE_URL=postgresql://test:test@localhost/test \
REDIS_URL=redis://localhost:6379 \
/tmp/dograh-review-venv/bin/python -m pytest \
  api/tests/test_game_companion_speech_text.py -q --capture=fd
```

Expected: all speech normalization tests pass, including the adversarial case.

### Task 3: Verify, commit, and publish the review repair

**Files:**
- Modify: `api/services/game_companion/speech_text.py`
- Modify: `api/tests/test_game_companion_speech_text.py`
- Add: `docs/plans/2026-08-02-linear-speech-emphasis-normalization-design.md`
- Add: `docs/plans/2026-08-02-linear-speech-emphasis-normalization.md`

**Step 1: Run the expanded companion suite**

Run the nine established companion test modules. Expected: all tests pass with
only the inherited Starlette deprecation warning.

**Step 2: Run static gates**

Run Ruff check, Ruff format check, and `git diff --check`. Expected: clean.

**Step 3: Commit and push**

```bash
git add api/services/game_companion/speech_text.py \
  api/tests/test_game_companion_speech_text.py docs/plans
git commit -m "fix: bound companion speech normalization"
git push origin feature/salvage-companion-direct-fish-tts
```

**Step 4: Close the review loop**

Reply in the Codex inline thread with the commit and verification evidence, then
request one final `@codex review`. Do not invoke Claude or merge the PR.
