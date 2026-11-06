# English copy review sheet

Scope: the English object in `site/content/site.ts` and English text rendered by
`site/components/pages/*`. This sheet is for a human reviewer; the copy was written
by the implementation model and has not been reviewed by a native speaker.

## Terms (keep as-is)

| Term | Rule |
|---|---|
| `cf_clearance` | Keep the cookie name verbatim; never translate or title-case. |
| `Cloudflare` | One word, capital C; do not write "CloudFlare". |
| `Turnstile` | Capital T; product name, not "turnstile" in prose. |
| `jsd` | Lowercase; it is the internal challenge name. |
| `managed challenge` | Lowercase except at sentence start. |
| `route=auto`, `want=turnstile`, `--pure-only` | Keep code formatting; do not add spaces around `=`. |
| `cfsolve`, `pydoll`, `Camoufox`, `FlareSolverr` | Backend names; keep spelling. |
| `egress IP` | Preferred over "exit IP" in English copy. |
| `replay` | Use for the act of reusing a clearance; not "replay attack". |
| `clearance` | Short form of `cf_clearance` only after the first mention. |

## Claims policy

- Every performance number must match `site/data/benchmarks.ts` and carry its date and conditions.
- Do not add "100%", "always works", "unlimited", "undetectable", or "guaranteed".
- Keep the known limits section: managed challenge replay can fail; the 2026-11-03 script has a stage2 blocker; clearance is bound to the egress IP.
- "Bypass" is in the H1 and title by an explicit product decision. Do not add it to claims about authorization; the `/security/` page must stay adjacent.

## Checklist

1. Articles: `a/an/the` around "egress IP", "managed challenge", "clearance".
2. Prepositions: "bound to", "replay for 30 minutes", "fall back to", "on the hot path".
3. Tense: present simple for behavior, past simple for a measured run.
4. Sentence length: split anything over ~28 words.
5. Voice: active; avoid "is being solved by".
6. Numbers: `18.43s` (no space), `2.93ms`, `0.55s`, `463B`, `72MB`; keep the unit style in `benchmarks.ts`.
7. Dates: `2026-11-01` in tables; "on 2026-11-04" in prose.
8. Headings: sentence case; no Title Case.
9. Trademark: "Cloudflare is a trademark of Cloudflare, Inc." appears in the footer.
10. Consistency: "challenge solver" (not "solver service" / "bypass service" mixed); "clearance" after first mention.

## Known uncertain phrases (review first)

| Location | Phrase | Question |
|---|---|---|
| `home.lede` | "Pure-protocol cfsolve first, then pydoll / Camoufox / FlareSolverr." | Is the comma splice acceptable? |
| `home.capabilities[0].body` | "the hot path has zero Python" | Is "zero Python" clear to a non-Python reader? |
| `how.sections[2].body` | "UA is not part of the cache key, but is used for replay." | Check comma and antecedent. |
| `benchmarks.lede` | "No search volume, no success-rate promises." | Tone: blunt is intentional. |
| `security.sections[0].body` | "no tutorial for bypassing paywalls or access controls" | Confirm this matches the intended policy. |

## Sign-off

| Reviewer | Date | Result |
|---|---|---|
| (pending) | — | — |