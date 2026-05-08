# Changelog

All notable changes to the SAE Books web frontend will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-05-08

### Added

- **Purchase orders UI.** `/purchase_orders` list with status, date and contact
  filters and HTMX-aware pagination; `/purchase_orders/{id}` detail with
  status-conditional Send / Cancel / Close / Convert-to-bill actions and an
  inline per-line conversion form; `/purchase_orders/new` create form with an
  HTMX-driven add-line button.
- **Prorate calculator.** `/proration` three-tab interactive calculator
  (per-line, first-period sign-up, plan-change). Each tab calls its preview
  endpoint and swaps a result fragment into the page — no DB writes.
- Sidebar: Vendors → Purchase Orders link added above Bills; Assets & Setup →
  Prorate Calculator link added.

### Fixed

- ATO/SBR admin UI now wires to the wizard endpoints correctly.

## [0.1.0] - 2026-05-08

Initial public alpha.
