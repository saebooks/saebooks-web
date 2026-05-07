# Contributing to SAE Books — Web Frontend

`saebooks-web` is in public alpha (v0.1) alongside the
[saebooks](https://github.com/saebooks/saebooks) engine. The rules
below mirror the engine repo so contributors face one consistent
process across both repos.

## Licence

`saebooks-web` is licensed under AGPL-3.0, the same licence as the
`saebooks` engine. By contributing, you license your contribution to
the project under the same terms.

## Contributor License Agreement (CLA)

Every contributor must sign the SAE Books CLA before any pull request
can be merged. The CLA covers contributions to **all** SAE Books
repositories — engine, frontend, plugins, docs — under one signature
per contributor.

The CLA text lives in `CLA.md` in the `saebooks` repo. In plain
English, it does two things:

1. You grant Richard Sauer / SAE Engineering the right to distribute
   your contribution under AGPL-3.0 *and* under alternative commercial
   licences. This keeps dual-licensing and paid exemptions possible
   without re-contacting every contributor.
2. You warrant that the contribution is yours to give — not owned by
   an employer who hasn't agreed, not derived from non-compatible
   code.

The signed CLA is a standing agreement covering all current and future
contributions by that contributor across every SAE Books repository.

## Trademark

"SAE Books" is a trademark of SAE Engineering. Forking the code under
AGPL-3.0 is fine; calling your fork "SAE Books" is not. See
`TRADEMARK.md` in the `saebooks` repo for the full policy.

## Code of Conduct

- Be kind. Be technical. Disagreements are about code, not people.
- Harassment, discrimination, or personal attacks get a single
  warning, then a ban.
- If you're unsure whether something is OK, ask privately first.

## How to contribute

1. Open an issue describing the bug, feature, or change.
2. Wait for a reply confirming it's in scope and not already claimed.
3. Fork, branch, implement, test.
4. Sign the CLA (one-time, per contributor, covers all SAE Books
   repos).
5. Open a pull request referencing the issue.
6. Address review feedback.
7. Once merged, your contribution is in.

## Commit style

- One logical change per commit.
- Imperative subject line, ≤72 characters.
- Body explains *why*, not *what* (the diff shows what).
- Co-author trailers welcome for paired work.

## Tests

- Every bug fix includes a regression test.
- Every feature includes user-facing docs + tests.
- CI must be green before merge.

## Security

Do not open public issues for security vulnerabilities. Email
`security@saee.com.au` with:

- Description of the issue
- Reproduction steps
- Impact assessment
- Suggested fix (optional)

We will respond within 72 hours and coordinate a disclosure timeline.

## Questions

For anything not covered here, open a discussion on the repo or email
the maintainer.
