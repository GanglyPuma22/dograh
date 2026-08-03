# Linear Speech Emphasis Normalization Design

## Problem

The TTS-only speech normalizer uses backtracking regular expressions to remove
paired Markdown asterisk emphasis. A 64 KiB response containing many unmatched
`**` candidates takes roughly 19 seconds to normalize synchronously, blocking
the companion event loop before the TTS timeout begins.

## Approved design

Replace the two backtracking substitutions with a line-scoped, single-pass
delimiter parser. Scan each character once, classify unescaped asterisk runs as
opening or closing emphasis only at the same word/whitespace boundaries already
covered by the tests, and pair one- and two-character markers with a stack.
Matched markers are omitted; unmatched, escaped, intraword, and whitespace-only
asterisks remain literal. Newlines reset the delimiter stack so emphasis never
crosses lines.

The parser operates only on the copy passed to TTS. Conversation history,
captions, tool results, provider selection, and the companion protocol remain
unchanged.

## Alternatives rejected

- **Truncate input:** hides the event-loop stall by discarding valid response
  text and changes speech semantics.
- **Run the regex in a worker thread:** protects the event loop but retains the
  quadratic algorithm and consumes worker capacity on malformed input.
- **Tune the existing regex:** small regex changes are difficult to prove
  linear while preserving nested and triple emphasis behavior.

## Verification

- Add a regression using the allowed 64 KiB unmatched-marker input and a broad
  runtime ceiling that fails the current 19-second implementation without being
  sensitive to ordinary CI variance.
- Preserve the existing semantic and provider-boundary tests.
- Run the focused speech tests, the expanded game-companion suite, Ruff,
  formatting, and `git diff --check`.

