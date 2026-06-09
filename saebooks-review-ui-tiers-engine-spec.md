# SAE Books Web UI Tiers - Engine Spec Handover (June 2026)

**From:** Grok (app lane)
**To:** Claude (engine lane)
**Context:** Full UI audit tiers implemented in saebooks-web (Tier 1 quick wins landed, Tier 2 in progress, Tier 3 planned). Changes are in feat/ui-tiers-2026-06 , merged to main for dev.books.sauer.com.au visibility after each tier. Critics to follow on dedicated sandbox.

## Tier 1 (landed) - mostly web, minor data needs
- BSL account filter: currently text; ideally populated select from /bank_accounts or recon accounts (name + id).
- TB/P&L drills: links now use account id; ensure ledger endpoints support date_from/to and return account.id reliably.
- Payments unmatch per-alloc: UI added; needs POST /payments/{id}/unallocate or similar tool with allocation granularity and impact preview (payment-on-account note).
- Employee contact name: web now prefers contact_name; ensure employee list API enriches with contact.name.
- General: more .num-display usage; no new API.

## Tier 2 (in progress) - key UX lifts, data contract changes needed
- Recon one-row / inline: current is multi-page (lines -> suggest). Need enriched suggest payload from engine:
  - Per suggestion: amount, gst, confidence (0-1 or %), why (["amount", "ref", "contact"]), rule_id if rule-matched.
  - BSL side: statement_balance, books_balance at account level for strip.
  - Support for "apply" : create expense/bill/je/transfer from BSL + auto-match in one.
  - Split and multi-match support (one BSL to N targets).
  - Rule creation from recon context (POST bank_rules with prefill from line desc).
- Payments allocation: search-as-you-type for targets (need /search or suggest endpoint for invoices/bills by number/contact/amount).
- Reports: comparatives (prior_period, ytd, py columns) in P&L/BS/TB/budget responses.
- GL ledger: additional filters (contact_id, description ilike, source_type in [INVOICE,BILL,...]) and contact_name in line items.
- Exports: consistent CSV endpoints for reports, GL, BSL, depr, etc. with tabular fidelity.
- Project financials: API for project rollup (time + expenses + revenue + profit).

## Tier 3 (planned) - strategic, heavy engine
- Reclass tool: dedicated endpoint or tooled-JE for posted line reclass with reason, audit link back to source (expense/bill etc.).
- Contact balances: unpaid, overdue, total, last_txn in contact responses and list filters.
- Full payroll data: YTD, leave balances, award info in employee and pay run surfaces.
- Forecast: cashflow forecast primitives (next 30d AR+AP+recurring +/- what-if).
- Period locks UI: endpoints to create/ list/ enforce quarter locks; history.
- Audit surface: rich change_log + snapshot query with filters, CSV.
- Dup detection, ML rules confidence, etc.

## Sandbox Critics (real automation round 2026-06-09)
After Tier 3 parity batch landed (real timed: 5 files, 101 lines net, 117s @21:56-21:58 AEST in worktree), full 4-persona critics re-run on the dedicated sandbox worktree `/home/richard/projects/saebooks-web-ui-tiers-2026-06` (feat/ui-tiers-2026-06, commit 1be5c71 pushed). 

**Real batch this round:** budgets (list + _table: h1 font-display/ink-3, sae-table, saebooks-input, btn-primary, sae checkboxes), expenses/new (labels ink-3, selects saebooks-input, notes/lines/add-line, save btn-primary; camera stub attempted), recon/lines (thead ink-3, All Accounts btn-secondary), pay_run (labels ink-3). Prior cumulative in branch: TB/PL exports, owing on contacts, overviews KPI, recon balance strip + sae-table, unallocate per-alloc, many UUID→plain labels, DS pockets.

**Critics round results (verbatim persona-driven, post-batch state inspected via tools on sandbox):**
- **Sandra (bookkeeper):** Wins: balance strip (statement/books/to-reconcile cards + total unmatched), per-alloc Unmatch in payments/detail (with confirm), partial DS (sae-table/card/ink/font-display/num in recon lines + payments + budgets table), some UUID softening. P0 blockers still: recon flow = "three pages per transaction nightmare" (suggest.html still legacy full old table, no inline suggested match + big OK per row per "I want one screen..."), no create-from-line buttons on BSL ("If there's no match, give me a 'Create expense' / 'Create bill' button right there"), no confidence/why/amounts on suggestions, no bulk on recon lines, BSL filter still text/UUID-ish in places, suggest + BSL detail leak raw UUIDs and old indigo/gray tables. Suggest.html untouched legacy. Flow score 3/10 vs Xero. "Match-in-one-row reconciliation" and "create in place" unaddressed in live (preview/reconcile.html is the north star). Web fixes limited; rest Tier2 engine spec (enriched suggest payload, apply/create-from, split/multi, rule-from-recon, search pickers, populated account selects).
- **Priya (accountant):** Wins: contact owing/overdue prominent (font-display num sae + overdue_days), TB account names now linked drills + Export CSV on TB, "Coming soon" stubs removed from accounts/detail (now full register with running bal + JE links + date filters), budget-vs-actual parity (Budget/Actual/Variance/% + signed color + totals) live and usable, some YTD in P&L/GST, HTMX partials, DS shell. P0 still exact baseline: no comparative columns (P&L/BS/TB/budget all single-period; "comparative columns are table stakes... SAE Books does not"), no drill from P&L rows (lines inert text, no /accounts or /journal-entries), GL/account filters date-only (no contact_id/source_type/desc ilike). Exports: only TB wired (?export=csv); PL/budget/others have print or promises in index but no links (routes have zero export handling beyond the one TB case). Score 3/10 for quarter-close. "Sub-ledger drill, not promises" only half-true. All map 1:1 to Tier2 in this spec ("Reports: comparatives...", "GL ledger: additional filters...", "Exports: consistent CSV...").
- **Mark (mobile/field tradie, ute 6pm):** Wins: owing/overdue card on contacts (and customer_hub), cashbook invoice/quote new forms are the "plain English" hero (free-text desc, one GST tick, no per-line acq/project/account/tax dropdowns — close to "type 'Mrs Smith — HWS swap — $1,840 inc GST' and hit send"), base.html solid (dark default + toggle + anti-FOUC + localStorage + prefers + Inter/JetBrains tabular + PWA manifest + mobile drawer + responsive cards). P0 dealbreakers: "Acq. cost" + margin jargon still on every full invoice/quote line table (7-8 cols, eats phone width, "never relevant to me"), ZERO camera input (capture=environment) on expenses/new (the Reece docket flow; "Take a photo of my handwritten quote and have it fill in the invoice"); no SMS/"Send via SMS" anywhere (email/Stripe only); save-draft then navigate for send (no bundled save+send); full forms still force account/tax/project per line; sales overviews have copy-paste duplicate AR owing cards (breaks phone); contact txn history still "Posted/DRAFT" not owing-focused; cashflow is pure historical accountant sections (no "next 30 days" or ute what-if per "Can I afford the new ute?"). Camera stub + labels/selects modernized in this batch (good), but not the capture input on the expense screen that needs it. Score 2/10 on "I do everything on my phone" + ute decision flow. Engine gaps: SMS, AI photo extract wired to expenses + job picker, forecast primitives, owing status in history filters. Previews (cashbook_capture etc) are the vision; live lags.
- **Auditor (DS/consistency/a11y/non-neg/branch):** Non-negs pass: NO Fraunces (only ban comment + no usage), dark-by-default + toggle + anti-FOUC + localStorage['saebooks-theme'] + :root[data-theme] + vars + meta in base.html ( "ships dark or it isn't finished" holds for shell), Inter + JetBrains tabular-nums (.font-display/.num-display), .sae-table/.card/.saebooks-input/.btn-* defined and used in core. DS unification ~62/100 this round (batch helped budgets/recon/expenses/pay_run). Remaining systemic: 88 files with bad h1 (`text-2xl font-semibold text-gray-900` + gray p), 135 raw `border-gray-300` inputs, sae-table only ~55 files (vs 167 plain tables), empty_state used in only 6, heavy legacy in accounts/* (full gray/#194291 in list + _table + detail ~60 grays), fixed_assets/*, expenses/list, _status_macros (hard indigo "Open"/blue Partial + gray Voided), many _form_fields/details/cashbook/auth/imports. Hard #194291 + indigo focus leaks persist (checkboxes, links, btns). Previews have localStorage key mismatch (saebooks_theme vs saebooks-theme) + reg-mark visibility diff. This batch (1be5c71) + priors good incremental (sae-table/ink-3/font-display/saebooks-input in targeted high-use + spec updated). Branch hygiene: feat/ui-tiers-2026-06 clean, multiple commits in <24h window with "real automation... per keep going", pushed, handoff/spec note dev visibility on main merge (sacred sauer ritual for prod build). Top files for next micro: accounts/list+_table, fixed_assets, _status_macros, more expenses/recon tables, unify 2-3 empties, fix preview key.

**Cumulative (from prior sim + this real):** ~25-30 issues noted / 12-15 fixed (P0s: unallocate, balance strip, UUID labels, styling/DS in recon+budgets+payments+employees, exports TB, owing, camera stub progress, h1/ink in more). Remaining P0/P1 exactly as Tier2/3 in this spec (recon one-row + create-from + confidence, comparatives + P&L drills + GL filters, full camera+SMS+AI on expenses, exports parity, forecast, reclass, contact columns, etc.). No web hacks; all filed here for engine lane.

**Action:** Land this real batch + critics round to main (dev.books.sauer visible <24h behind per user rule, no hold communicated). Engine lane: action Tier 2 items (recon payload, comparatives, search pickers, exports, contact balances, GL filters) so web can finish the big UX. Re-run full critics on sandbox post-engine. App lane (Grok) continues incremental DS + preview parity + web stubs in this worktree.

Changes isolated in app lane worktree. Handoff log updated. Benchmark in /tmp/ui-tiers-benchmark.log. Per "start doing this in automation now, I need to benchmark this, so keep going, I'll check up on you in a few hours, don't wait on me...". 

(Real run 21:54-22:00 AEST; 4 critics full reports captured above + in subagent transcripts.)

**Action for engine lane:** Prioritize the Tier 2 items for the next engine cycle so web can deliver the big UX wins (recon, allocations, reports) without workarounds. File in buildboard if needed.

Changes are isolated in app lane worktree. Handoff log updated.
