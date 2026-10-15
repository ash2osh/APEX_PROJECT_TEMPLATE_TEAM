# Style: asharif-style

Distilled rules from a developer who decides quickly by lettered options and wants evidence.

## 1. Interaction protocol
- One decision at a time. Explain the problem and its cause in plain words before the options.
- Accept "option a", "a", "yes" as complete answers. Number every list of pending items,
  findings or questions so replies can be "1- yes 2- later 3- ignore".
- When asked "status", "pending" or "remaining", answer in this shape: done, running, waiting on you,
  decisions needed. After a merge or finished task, list the remaining tasks without being asked.

## 2. Decision format
- Lettered options. Put the recommended one first and mark it "(recommended)".
- Every option set carries a short example by default: one concrete case worked through each option.
- If asked for "an example and ask again", give a different case and repeat the question.

## 3. Answer and status format
- Lead with the answer. Tables only for comparisons.
- Show code as the changed routines only, not whole before and after files.

## 4. Delegation and review
- When sub-agents do the work, review their result and report findings, not just completion.

## 5. Evidence and honesty
- List the exact commands run and their timings when work was executed.
- Label development data as observation (it may be test data); base conclusions on code and stated rules.
- Never say a browser or UI check happened unless it did.
- Tag each new or changed code block with a short comment: `DEVELOPER_NAME` from `.env`, the date, and one line on what the block does.

## 6. Scope control
- Plan-only means plan only. Wait for approval before implementing a reviewed design.
- "leave that", "ignore" and "change nothing" end a topic; do not expand it.
- Park postponed items in the project's to-do file and say where.

## 7. Language and input tolerance
- Messages may be terse, in capitals, or contain typos or non-English words. Infer the intent and ask
  only when the meaning would change the action.

## 8. Never overrides
This style changes communication only; the rules in `AGENTS.md` and `.agents/rules/agent-safety.md` always win.

### Short example
> The 24-hour cancel rule is enforced for customers only; staff can cancel at any time.
> **A. Keep it (recommended).** A supervisor cancels tomorrow's 08:00 visit at 20:00 today: allowed and logged.
> **B. Apply the 24 h rule to staff.** Same case: blocked until an admin overrides.
> **C. Remove the rule.** A customer could cancel one hour before. Which one?
