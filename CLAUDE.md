# General guidelines

## Writing

- Do not use em dashes; use a regular hyphen (`-`) instead.

## Technical decisions

- Do not optimize primarily for development cost. Prefer **quality**, **simplicity**, **robustness**, and **scalability** when choosing approaches.

## Bug fixes

- Before changing code, reproduce the issue in an end-to-end setting that matches how a user (or an internal system under load) would hit it. Confirm the root cause in that context, then implement the fix.

## Engineering standards

- Hold a high bar for engineering excellence and apply **SOLID** principles where they fit the problem.

## Planning

- In plan mode, assume as little as possible. Ask clarifying questions instead of filling gaps with guesses, so design stays aligned with intent and good system design practice.
- Once a plan is agreed, break work into **incremental build slices**. Present each slice with a short description of what it delivers and how it moves the overall goal forward.

## Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## Maintain Consistency

- Match existing project conventions and style unless they are harmful.

- Reuse existing patterns when reasonable.

- Do not silently introduce conflicting architectural patterns.

- Remove only the dead code directly caused by your own changes.

## Commenting Rules

- Prefer to use less jargon without sacrificing technical information that's to be conveyed

- On top of each code file generated, make sure to write a comment about what this file does,. 

- Every funtion, api, or other important functionality should have a comment before it saying what's happening there. 

## Decision Heuristics

Prefer:

- explicit over implicit

- simple over clever

- concrete over abstract

- verified over assumed

- focused changes over broad rewrites

Avoid:

- speculative engineering

- hallucinated implementations

- hidden side effects

- silent behavior changes

- unnecessary complexity
