"""Pass B design preview — static mocks, no data wiring, no auth.

Public routes used to iterate on visual design before committing to a
data-bound implementation.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/preview/home", response_class=HTMLResponse, response_model=None)
async def preview_home(request: Request) -> HTMLResponse:
    """Static mock of the redesigned home dashboard."""
    return _TEMPLATES.TemplateResponse(request, "preview/home.html", {})


@router.get("/preview/invoices", response_class=HTMLResponse, response_model=None)
async def preview_invoices(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/invoices.html", {})


@router.get("/preview/bills", response_class=HTMLResponse, response_model=None)
async def preview_bills(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/bills.html", {})


@router.get("/preview/quotes", response_class=HTMLResponse, response_model=None)
async def preview_quotes(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/quotes.html", {})


@router.get("/preview/contacts", response_class=HTMLResponse, response_model=None)
async def preview_contacts(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/contacts.html", {})


@router.get("/preview/reports", response_class=HTMLResponse, response_model=None)
async def preview_reports(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/reports.html", {})


@router.get("/preview/projects", response_class=HTMLResponse, response_model=None)
async def preview_projects(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/projects.html", {})


@router.get("/preview/compliance", response_class=HTMLResponse, response_model=None)
async def preview_compliance(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/compliance.html", {})


@router.get("/preview/cashbook", response_class=HTMLResponse, response_model=None)
async def preview_cashbook(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/cashbook.html", {})


@router.get("/preview/reconcile", response_class=HTMLResponse, response_model=None)
async def preview_reconcile(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/reconcile.html", {})


@router.get("/preview/invoices/inv-0028", response_class=HTMLResponse, response_model=None)
async def preview_invoice_detail(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/invoice_detail.html", {})


@router.get("/preview/contacts/acme", response_class=HTMLResponse, response_model=None)
async def preview_contact_detail(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/contact_detail.html", {})


@router.get("/preview/reports/profit-loss", response_class=HTMLResponse, response_model=None)
async def preview_report_pl(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/pl.html", {})


@router.get("/preview/quotes/q-0032/convert", response_class=HTMLResponse, response_model=None)
async def preview_quote_convert(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/quote_convert.html", {})


@router.get("/preview/settings", response_class=HTMLResponse, response_model=None)
async def preview_settings(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/settings.html", {})


@router.get("/preview/compliance/bas-prep", response_class=HTMLResponse, response_model=None)
async def preview_bas_prep(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/bas_prep.html", {})


@router.get("/preview/payroll/pay-run", response_class=HTMLResponse, response_model=None)
async def preview_pay_run(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/pay_run.html", {})


@router.get("/preview/compliance/tpar", response_class=HTMLResponse, response_model=None)
async def preview_tpar(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/tpar.html", {})


@router.get("/preview/bills/bill-1842", response_class=HTMLResponse, response_model=None)
async def preview_bill_detail(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/bill_detail.html", {})


@router.get("/preview/invoices/new", response_class=HTMLResponse, response_model=None)
async def preview_invoice_new(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/invoice_new.html", {})


@router.get("/preview/inbox", response_class=HTMLResponse, response_model=None)
async def preview_inbox(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/inbox.html", {})


@router.get("/preview/cashbook/capture", response_class=HTMLResponse, response_model=None)
async def preview_cashbook_capture(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/cashbook_capture.html", {})


@router.get("/preview/cashbook/tax-time", response_class=HTMLResponse, response_model=None)
async def preview_cashbook_tax_time(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/cashbook_tax_time.html", {})


@router.get("/preview/activity", response_class=HTMLResponse, response_model=None)
async def preview_activity(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/activity.html", {})


@router.get("/preview/super", response_class=HTMLResponse, response_model=None)
async def preview_super(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/super.html", {})


@router.get("/preview/reports/aged-receivables", response_class=HTMLResponse, response_model=None)
async def preview_aged_receivables(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/aged_receivables.html", {})


@router.get("/preview/reports/aged-payables", response_class=HTMLResponse, response_model=None)
async def preview_aged_payables(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/aged_payables.html", {})


@router.get("/preview/reports/cash-flow", response_class=HTMLResponse, response_model=None)
async def preview_cash_flow(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/cash_flow.html", {})


@router.get("/preview/reports/balance-sheet", response_class=HTMLResponse, response_model=None)
async def preview_balance_sheet(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/balance_sheet.html", {})


@router.get("/preview/projects/j-2603", response_class=HTMLResponse, response_model=None)
async def preview_job_detail(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/job_detail.html", {})


@router.get("/preview/settings/banking/cba", response_class=HTMLResponse, response_model=None)
async def preview_settings_banking_cba(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/settings_banking_cba.html", {})


@router.get("/preview/settings/integrations/ato", response_class=HTMLResponse, response_model=None)
async def preview_settings_ato(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/settings_ato.html", {})


@router.get("/preview/reports/profit-loss/sales-engineering", response_class=HTMLResponse, response_model=None)
async def preview_pl_account(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/pl_account.html", {})


@router.get("/preview/settings/integrations/peppol", response_class=HTMLResponse, response_model=None)
async def preview_settings_peppol(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/settings_peppol.html", {})


@router.get("/preview/payroll/employees/mia-chen", response_class=HTMLResponse, response_model=None)
async def preview_employee_detail(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/employee_detail.html", {})


@router.get("/preview/reports/aged-receivables/bowen-crane-hire", response_class=HTMLResponse, response_model=None)
async def preview_debtor_bowen(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/debtor_drilldown.html", {})


@router.get("/preview/settings/chart-of-accounts", response_class=HTMLResponse, response_model=None)
async def preview_chart_of_accounts(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/chart_of_accounts.html", {})


@router.get("/preview/super/batches/ssp-0014", response_class=HTMLResponse, response_model=None)
async def preview_super_batch(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/super_batch.html", {})


@router.get("/preview/settings/bank-feed-rules", response_class=HTMLResponse, response_model=None)
async def preview_bank_feed_rules(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/bank_feed_rules.html", {})


@router.get("/preview/settings/integrations/stripe-au", response_class=HTMLResponse, response_model=None)
async def preview_settings_stripe_au(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/settings_stripe_au.html", {})


@router.get("/preview/contacts/dale-maxwell", response_class=HTMLResponse, response_model=None)
async def preview_contact_dale_maxwell(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/supplier_dale_maxwell.html", {})


@router.get("/preview/settings/users", response_class=HTMLResponse, response_model=None)
async def preview_settings_users(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/settings_users.html", {})


@router.get("/preview/settings/tax-rates", response_class=HTMLResponse, response_model=None)
async def preview_settings_tax_rates(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/settings_tax_rates.html", {})


@router.get("/preview/settings/numbering", response_class=HTMLResponse, response_model=None)
async def preview_settings_numbering(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/settings_numbering.html", {})


@router.get("/preview/cashbook/receipts/rct-00184", response_class=HTMLResponse, response_model=None)
async def preview_receipt_detail(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/receipt_detail.html", {})


@router.get("/preview/settings/integrations/square-au", response_class=HTMLResponse, response_model=None)
async def preview_settings_square_au(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/settings_square_au.html", {})


@router.get("/preview/payroll/stp-events/26-051104", response_class=HTMLResponse, response_model=None)
async def preview_stp_event(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/stp_event.html", {})


@router.get("/preview/inbox/inv-bs-20611", response_class=HTMLResponse, response_model=None)
async def preview_inbox_peppol_detail(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/inbox_peppol_detail.html", {})


@router.get("/preview/migration", response_class=HTMLResponse, response_model=None)
async def preview_migration(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/migration.html", {})


@router.get("/preview/compliance/bas/26-q3", response_class=HTMLResponse, response_model=None)
async def preview_bas_lodgement(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/bas_lodgement.html", {})


@router.get("/preview/banking/trust/j-2603", response_class=HTMLResponse, response_model=None)
async def preview_trust_account(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/trust_account.html", {})


@router.get("/preview/jobs/j-2603/financials", response_class=HTMLResponse, response_model=None)
async def preview_job_financials(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/job_financials.html", {})


@router.get("/preview/super/batch/26q4-0184", response_class=HTMLResponse, response_model=None)
async def preview_super_batch_q4(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/super_batch_q4.html", {})


@router.get("/preview/bills/bill-1843", response_class=HTMLResponse, response_model=None)
async def preview_bill_1843(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/bill_1843.html", {})


@router.get("/preview/contracts/j-2603/payment-schedule-09", response_class=HTMLResponse, response_model=None)
async def preview_payment_schedule_09(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/payment_schedule.html", {})


@router.get("/preview/compliance/fbt/fy26-workpaper", response_class=HTMLResponse, response_model=None)
async def preview_fbt_fy26_workpaper(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/fbt_workpaper.html", {})


@router.get("/preview/compliance/div7a/ln-2603", response_class=HTMLResponse, response_model=None)
async def preview_div7a_ln_2603(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/div7a_loan.html", {})


@router.get("/preview/payroll/pay-event/26w19", response_class=HTMLResponse, response_model=None)
async def preview_stp_pe_26w19(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/stp_pay_run.html", {})


@router.get("/preview/cashbook/april-2026", response_class=HTMLResponse, response_model=None)
async def preview_cashbook_april(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/cashbook_april.html", {})


@router.get("/preview/compliance/depreciation/sbe-pool-fy26", response_class=HTMLResponse, response_model=None)
async def preview_sbe_pool_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/sbe_pool.html", {})


@router.get("/preview/compliance/ftc/q4-fy26", response_class=HTMLResponse, response_model=None)
async def preview_ftc_q4_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/ftc_q4_fy26.html", {})


@router.get("/preview/compliance/payg-i/q4-fy26", response_class=HTMLResponse, response_model=None)
async def preview_payg_i_q4_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/payg_instalment_q4.html", {})


@router.get("/preview/quotes/qte-2618", response_class=HTMLResponse, response_model=None)
async def preview_quote_qte2618(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/quote_qte2618.html", {})


@router.get("/preview/compliance/rdti/fy26", response_class=HTMLResponse, response_model=None)
async def preview_rdti_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/rdti_fy26.html", {})


@router.get("/preview/compliance/payroll-tax/qld-q4-fy26", response_class=HTMLResponse, response_model=None)
async def preview_payroll_tax_qld_q4(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/payroll_tax_qld_q4.html", {})


@router.get("/preview/compliance/workcover/qld-fy26", response_class=HTMLResponse, response_model=None)
async def preview_workcover_qld_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/workcover_qld_fy26.html", {})


@router.get("/preview/compliance/cgt/sbc-fy26", response_class=HTMLResponse, response_model=None)
async def preview_cgt_sbc_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/cgt_sbc_fy26.html", {})


@router.get("/preview/compliance/land-tax/qld-fy26", response_class=HTMLResponse, response_model=None)
async def preview_land_tax_qld_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/land_tax_qld_fy26.html", {})


@router.get("/preview/compliance/tpar/fy26", response_class=HTMLResponse, response_model=None)
async def preview_tpar_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/tpar_fy26.html", {})


@router.get("/preview/compliance/director-id/fy26", response_class=HTMLResponse, response_model=None)
async def preview_director_id_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/director_id_fy26.html", {})


@router.get("/preview/compliance/fbt/fy26", response_class=HTMLResponse, response_model=None)
async def preview_fbt_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/fbt_fy26.html", {})


@router.get("/preview/compliance/payday-super/fy26", response_class=HTMLResponse, response_model=None)
async def preview_payday_super_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/payday_super_fy26.html", {})


@router.get("/preview/compliance/div7a/fy26", response_class=HTMLResponse, response_model=None)
async def preview_div7a_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/div7a_fy26.html", {})


@router.get("/preview/compliance/smsf-audit/fy26", response_class=HTMLResponse, response_model=None)
async def preview_smsf_audit_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/smsf_audit_fy26.html", {})


@router.get("/preview/compliance/fte-distribution/fy26", response_class=HTMLResponse, response_model=None)
async def preview_fte_distribution_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/fte_distribution_fy26.html", {})


@router.get("/preview/compliance/asic-annual-review/fy26", response_class=HTMLResponse, response_model=None)
async def preview_asic_annual_review_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/asic_annual_review_fy26.html", {})


@router.get("/preview/compliance/payroll-tax-multistate/fy26", response_class=HTMLResponse, response_model=None)
async def preview_payroll_tax_multistate_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/payroll_tax_multistate_fy26.html", {})


@router.get("/preview/compliance/gst-annual-recon/fy26", response_class=HTMLResponse, response_model=None)
async def preview_gst_annual_recon_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/gst_annual_recon_fy26.html", {})


@router.get("/preview/compliance/stp-finalisation/fy26", response_class=HTMLResponse, response_model=None)
async def preview_stp_finalisation_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/stp_finalisation_fy26.html", {})


@router.get("/preview/compliance/ecpi-actuarial/fy26", response_class=HTMLResponse, response_model=None)
async def preview_ecpi_actuarial_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/ecpi_actuarial_fy26.html", {})


@router.get("/preview/compliance/ato-data-matching/fy26", response_class=HTMLResponse, response_model=None)
async def preview_ato_data_matching_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/ato_data_matching_fy26.html", {})


@router.get("/preview/compliance/psi-attribution/fy26", response_class=HTMLResponse, response_model=None)
async def preview_psi_attribution_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/psi_attribution_fy26.html", {})


@router.get("/preview/compliance/gic-sic-deductibility/fy26", response_class=HTMLResponse, response_model=None)
async def preview_gic_sic_deductibility_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/gic_sic_deductibility_fy26.html", {})


@router.get("/preview/compliance/div7a-upe-bendel/fy26", response_class=HTMLResponse, response_model=None)
async def preview_div7a_upe_bendel_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/div7a_upe_bendel_fy26.html", {})


@router.get("/preview/compliance/land-tax-multistate/fy26", response_class=HTMLResponse, response_model=None)
async def preview_land_tax_multistate_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/land_tax_multistate_fy26.html", {})


@router.get("/preview/compliance/stage3-brackets/fy26", response_class=HTMLResponse, response_model=None)
async def preview_stage3_brackets_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/stage3_brackets_fy26.html", {})


@router.get("/preview/compliance/div296-super/fy26", response_class=HTMLResponse, response_model=None)
async def preview_div296_super_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/div296_super_fy26.html", {})


@router.get("/preview/compliance/aasb16-leases/fy26", response_class=HTMLResponse, response_model=None)
async def preview_aasb16_leases_fy26(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "preview/aasb16_leases_fy26.html", {})
