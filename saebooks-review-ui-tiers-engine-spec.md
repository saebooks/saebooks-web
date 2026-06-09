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

## Sandbox Critics
After Tier 3 lands on main (dev visible), full critic re-run (all personas + structured) will be executed on dedicated sandbox tenant. Results will be in separate reports. Any new engine gaps flagged there will be added to this spec or new handover.

**Action for engine lane:** Prioritize the Tier 2 items for the next engine cycle so web can deliver the big UX wins (recon, allocations, reports) without workarounds. File in buildboard if needed.

Changes are isolated in app lane worktree. Handoff log updated.
