# Architectural deviations

When a ticket's premise turns out to be wrong — the file it says to create already
exists, the design it assumes was superseded, the bug it describes was fixed — the fix
is not to follow the ticket anyway, and it is not to silently do something else. It is
to write down what was actually true and what was built instead.

One file per ticket, named for the ticket: `W3-A-01.md`, `S4-A-00.md`. A follow-up to an
earlier deviation gets its own file with a `-followup-<topic>` suffix.

Each note records:

1. **What the ticket assumed** — quoted or closely paraphrased, so the gap is legible.
2. **What was actually true** — the state of the code when the work started, with file
   references.
3. **What was decided, and by whom** — adapt, replace, or defer. Say who approved it.
4. **What changed as a result** — the sub-decisions that differ from the literal ticket
   text, each with its reason.
5. **Accepted residual risk** — what is still not handled, and why that was acceptable.

A note here is not a changelog. The diff already says what changed; this says why the
change does not look like what was asked for. Summarize the durable conclusions into
`CLAUDE.md` as well — a deviation note is the record, `CLAUDE.md` is what the next
person reads first.
