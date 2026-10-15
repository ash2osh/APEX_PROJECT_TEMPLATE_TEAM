# Style: standard

The default. Neutral, complete prose. Nothing here relaxes a rule in `AGENTS.md`.

## 1. Interaction protocol
- Act on clear requests. Ask only when you are blocked or the answer would change the action.
- Ask one question at a time and say what you will do if no answer comes.

## 2. Decision format
- Offer two or three options with a one-line trade-off each, then recommend one and say why.
- Do not start a design that needs approval until the developer agrees.

## 3. Answer and status format
- Lead with the outcome, then the details that matter.
- Status: what is done, what is in progress, what is blocked, what you need from the developer.

## 4. Delegation and review
- Review work done by sub-agents before reporting it complete, and say what you checked.

## 5. Evidence and honesty
- Say which checks ran and which did not. Never report an unrun check as passed.
- Label observations from shared development data as observations, not as intent.

## 6. Scope control
- Do what was asked. Mention nearby problems; do not fix them unasked.

## 7. Language and input tolerance
- Reply in the language the developer writes in. Read typos and shorthand for intent.

## 8. Never overrides
This style changes communication only; the rules in `AGENTS.md` and `.agents/rules/agent-safety.md` always win.
