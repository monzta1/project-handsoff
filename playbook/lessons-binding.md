# Lessons: module boundaries and name binding

Split from lessons-evidence.md when the monolith split (#284) started
producing a defect shape of its own: state that reads correctly and code that
is never reached, because a name has more than one binding.

- A patch replaces one binding, not a symbol. After a split, `from X import f`
  gives each importing module its own binding; `mock.patch.object(lib, "f")`
  replaces the monolith's only. Two tests stayed green while exercising the real
  code: a commit that ran inside another module, and a sleep fixture that read
  the operator's real pmset log, 281 intervals where it supplied 1. Whether a
  patch lands depends on the call path, which the syntax does not reveal, so
  patch every binding (`tests/engine_patch.patch_engine`) instead of reasoning
  about which one runs.
- When a rule cannot be decided, change the shape so it cannot be broken. Four
  attempts at deciding early binding statically each gave false positives;
  making the inert patch unexpressible took one helper and no judgement calls.
- A pattern pinned to today's magnitude fails tomorrow for a reason unrelated
  to what it checks: `\b1\d,\d{3}\b` matched the line-count claim until the
  file dropped under 10,000 lines, then failed whatever the document said.
