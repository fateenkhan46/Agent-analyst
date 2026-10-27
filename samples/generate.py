"""Generate the sample project: three inconsistent sources with *known*,
documented defects, plus a brief and a stakeholder transcript.

The defects are deliberate and enumerated in EXPECTED.md so the pipeline's
findings can be checked against ground truth rather than eyeballed.

Run: python samples/generate.py [output_dir]
"""
from __future__ import annotations

import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

random.seed(20261027)  # reproducible output

# DEFECT 13: "Tom  Okafor" carries a double space, so it does not group with
# "Tom Okafor" until whitespace is normalised.
PROJECTS = [
    ("PRJ-1001", "Harbour Bridge Retrofit",      "Infrastructure", "Priya Raman",   "In Flight", 1_250_000, "2025-01-06", "2026-06-30"),
    ("PRJ-1002", "Northgate Depot Automation",   "Logistics",      "Tom  Okafor",   "In Flight",   890_000, "2025-03-17", "2026-02-27"),
    ("PRJ-1003", "Customer Portal Replatform",   "Technology",     "Sara Lindqvist","Complete",    640_000, "2024-09-02", "2025-11-14"),
    ("PRJ-1004", "Fleet Electrification Pilot",  "Operations",     "Tom Okafor",    "On Hold",     410_000, "2025-05-12", "2026-09-30"),
    ("PRJ-1005", "Regulatory Reporting Uplift",  "Finance",        "Priya Raman",   "In Flight",   275_000, "2025-07-01", "2026-03-31"),
    ("PRJ-1006", "Warehouse Safety Programme",   "Operations",     "Nadia Haddad",  "Complete",    198_000, "2024-11-04", "2025-08-29"),
    ("PRJ-1007", "Data Platform Migration",      "Technology",     "Sara Lindqvist","In Flight",   755_000, "2025-02-10", "2026-12-18"),
    ("PRJ-1008", "Port Access Road",             "Infrastructure", "Nadia Haddad",  "Cancelled",   520_000, "2025-04-21", "2026-01-30"),
]

COST_CATEGORIES = ["Labour", "Materials", "Subcontract", "Equipment", "Travel", "Overhead"]
EMPLOYEES = [
    ("E-204", "A. Whitfield", "Engineering", 92.50),
    ("E-211", "B. Nakamura",  "Engineering", 88.00),
    ("E-233", "C. Mensah",    "Delivery",    76.25),
    ("E-241", "D. Fischer",   "Delivery",    74.00),
    ("E-258", "E. Rossi",     "Safety",      69.50),
    ("E-262", "F. Dubois",    "Engineering", 95.00),
    ("E-270", "G. Patel",     "Analytics",   81.75),
]


def _mixed_date(d: date, i: int) -> str:
    """Deliberately inconsistent date formats -- DEFECT 1."""
    if i % 5 == 0:
        return d.strftime("%d/%m/%Y")        # ambiguous order
    if i % 5 == 1:
        return d.strftime("%Y-%m-%d")        # ISO
    if i % 5 == 2:
        return d.strftime("%d-%b-%Y")        # 15-Mar-2025
    if i % 5 == 3:
        return d.strftime("%m/%d/%Y")        # US order, same column
    return d.strftime("%B %d, %Y")           # March 15, 2025


def _mixed_money(v: float, i: int) -> str:
    """Deliberately inconsistent currency formats -- DEFECT 2."""
    if i % 6 == 0:
        return f"${v:,.2f}"
    if i % 6 == 1:
        return f"{v:,.2f}"
    if i % 6 == 2:
        return f"{v:.2f}"
    if i % 6 == 3:
        return f"({abs(v):,.2f})" if v < 0 else f"{v:,.2f}"
    if i % 6 == 4:
        return f"{v:,.2f} USD"
    return f"{v:.2f}".replace(".", ",")      # European decimal comma


def write_costs(out: Path) -> None:
    """Workbook with a title row, two sheets, inconsistent headers."""
    wb = Workbook()
    ws = wb.active
    ws.title = "FY25 Costs"
    ws.append(["Acme Infrastructure Group — Project Cost Ledger (FY25)"])
    ws.append([])
    ws.append(["Proj ID", "Cost Date", "Cost Category", "Cost Amount", "Vendor", "Invoice No"])

    i = 0
    rows = []
    for pid, _name, _pf, _pm, _st, _budget, start_iso, _finish in PROJECTS:
        start = date.fromisoformat(start_iso)
        for _ in range(random.randint(14, 26)):
            d = start + timedelta(days=random.randint(0, 420))
            cat = random.choice(COST_CATEGORIES)
            amount = round(random.uniform(1_200, 48_000), 2)
            rows.append([pid, d, cat, amount, f"V-{random.randint(100, 180)}",
                         f"INV-{random.randint(20000, 29999)}"])
    random.shuffle(rows)

    for pid, d, cat, amount, vendor, inv in rows:
        ws.append([pid, _mixed_date(d, i), cat, _mixed_money(amount, i), vendor, inv])
        i += 1

    # DEFECT 3: orphan project id present in costs but absent from the register
    ws.append(["PRJ-1099", "2025-08-14", "Materials", "$18,400.00", "V-144", "INV-29001"])
    ws.append(["PRJ-1099", "2025-09-02", "Labour",    "$22,100.00", "V-151", "INV-29002"])
    # DEFECT 4: an exact duplicate cost row (double-count risk)
    dup = rows[3]
    ws.append([dup[0], _mixed_date(dup[1], 3), dup[2], _mixed_money(dup[3], 3), dup[4], dup[5]])
    # DEFECT 5: a negative cost (credit note) and a blank amount
    ws.append(["PRJ-1001", "2025-06-18", "Materials", "(4,250.00)", "V-133", "INV-28810"])
    ws.append(["PRJ-1002", "2025-07-09", "Travel", "", "V-160", "INV-28899"])
    # DEFECT 6: a row whose amount is in a different currency
    ws.append(["PRJ-1007", "2025-10-01", "Subcontract", "EUR 31,500.00", "V-177", "INV-29140"])

    # Second sheet: same concept, *different* header names -- DEFECT 7
    ws2 = wb.create_sheet("FY24 Costs")
    ws2.append(["project_id", "transaction_date", "category", "amount_usd", "supplier", "invoice"])
    for pid, _name, _pf, _pm, _st, _budget, start_iso, _finish in PROJECTS[:4]:
        start = date.fromisoformat(start_iso)
        for _ in range(random.randint(6, 12)):
            d = start - timedelta(days=random.randint(1, 300))
            ws2.append([pid, d.isoformat(), random.choice(COST_CATEGORIES),
                        round(random.uniform(900, 30_000), 2),
                        f"V-{random.randint(100, 180)}", f"INV-{random.randint(10000, 19999)}"])

    hidden = wb.create_sheet("Pivot Scratch")
    hidden.sheet_state = "hidden"
    hidden.append(["ignore me"])
    wb.save(out / "project_costs.xlsx")


def write_labour(out: Path) -> None:
    """Timesheets as CSV, with mixed hour units -- DEFECT 8."""
    path = out / "labour_hours.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timesheet_id", "project", "employee_id", "employee",
                    "week_ending", "hours_worked", "hourly_rate", "dept"])
        tid = 5000
        for pid, _name, _pf, _pm, _st, _budget, start_iso, _finish in PROJECTS:
            start = date.fromisoformat(start_iso)
            for _ in range(random.randint(10, 20)):
                emp = random.choice(EMPLOYEES)
                wk = start + timedelta(weeks=random.randint(0, 50))
                hours = round(random.uniform(4, 44), 2)
                roll = random.random()
                if roll < 0.12:
                    hours_text = f"{hours:.2f}h"           # unit suffix
                elif roll < 0.20:
                    hours_text = f"{int(hours * 60)}min"    # minutes!
                elif roll < 0.26:
                    hh = int(hours); mm = int((hours - hh) * 60)
                    hours_text = f"{hh:02d}:{mm:02d}"       # hh:mm
                else:
                    hours_text = f"{hours:.2f}"
                w.writerow([f"TS-{tid}", pid, emp[0], emp[1],
                            wk.strftime("%d/%m/%Y"), hours_text, f"${emp[3]:.2f}", emp[2]])
                tid += 1
        # DEFECT 9: duplicate timesheet_id with different hours (conflicting record)
        w.writerow(["TS-5000", "PRJ-1001", "E-204", "A. Whitfield", "13/01/2025", "41.00", "$92.50", "Engineering"])
        # DEFECT 10: a timesheet with no project (missing foreign key)
        w.writerow([f"TS-{tid}", "", "E-233", "C. Mensah", "20/01/2025", "12.50", "$76.25", "Delivery"])
        # DEFECT 11: project id written in a different style (prj 1003)
        w.writerow([f"TS-{tid+1}", "prj 1003", "E-270", "G. Patel", "03/02/2025", "16.00", "$81.75", "Analytics"])


def write_register(out: Path) -> None:
    """The project register -- the dimension. Header on row 1, clean-ish."""
    path = out / "project_register.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Project Code", "Project Name", "Portfolio", "Project Manager",
                    "Status", "Approved Budget", "Start Date", "Planned Finish",
                    "Percent Complete"])
        for pid, name, portfolio, pm, status, budget, start, finish in PROJECTS:
            pct = {"Complete": "100%", "Cancelled": "0%", "On Hold": "35%"}.get(
                status, f"{random.randint(20, 85)}%"
            )
            w.writerow([pid, name, portfolio, pm, status,
                        f"${budget:,.0f}", start, finish, pct])
        # DEFECT 12: a project in the register with no costs and no labour at all
        w.writerow(["PRJ-1009", "Siding Rehabilitation", "Infrastructure",
                    "Nadia Haddad", "In Flight", "$150,000", "2026-01-05",
                    "2026-10-30", "5%"])


BRIEF = """# Reporting brief — Project Portfolio Cost & Performance

**Requested by:** Priya Raman, Finance Director
**Audience:** Executive committee (monthly), portfolio managers (weekly)
**Reporting period:** FY2025 (2025-01-01 to 2025-12-31), with FY2024 available for comparison
**Currency:** USD. All amounts are to be reported in USD.
**Units:** Labour is reported in hours.

## Business objective

We cannot currently answer, in one place, whether our capital projects are
running over budget and why. Cost data lives in a finance workbook, labour in
timesheet exports, and project status in a register maintained by the PMO. The
three disagree about project identifiers and about what "complete" means.

We need a report the executive committee can open monthly to see which
projects are overspending, by how much, and how much of that is labour.

## KPIs required

1. **Total Cost** — all cost ledger amounts, in USD.
2. **Total Budget** — the approved budget from the project register.
3. **Budget Variance** — Budget minus Total Cost. Positive means under budget.
4. **Budget Variance %** — Variance as a percentage of Budget.
5. **Total Labour Hours** — hours from the timesheets, normalised to hours.
6. **Average Cost per Project** — Total Cost divided by the number of projects
   that actually have cost.
7. **Project Completion %** — from the register.
8. **Month-over-Month Cost Change** — change in Total Cost vs the prior month.
9. **Year-to-Date Cost** — cumulative cost within the reporting year.

## Reporting questions

- Which projects are over budget, and by how much?
- How does cost split by category and by portfolio?
- What is the monthly cost trend, and is it accelerating?
- Which portfolios consume the most labour hours?
- Which projects have no recent cost activity?

## Required dimensions and filters

Portfolio, Project Manager, Status, Cost Category, Month. Status and Portfolio
must be available as slicers.

## Known problems we are aware of

- Project identifiers are written differently in each system.
- The cost workbook has two sheets with different column names.
- Some timesheet rows have no project.
- There is at least one project id in the cost ledger that the PMO does not
  recognise.
"""

TRANSCRIPT = """Project Portfolio Reporting — Requirements Workshop
Date: 2026-10-12
Duration: approximately 50 minutes
Attendees: Priya Raman (Finance Director), Tom Okafor (PMO Lead), Sara Lindqvist (Data Engineering), Nadia Haddad (Operations Manager)

Priya Raman: Thanks everyone. The reason we're here is that the exec committee asked the same question three months in a row and we gave three different answers. I want one number for project cost and one number for variance, and I want to know where they come from.

Tom Okafor: Agreed. Part of the problem is that my register and Finance's ledger don't use the same project codes. Mine are PRJ-1001 style, the ledger sometimes has spaces or lowercase.

Sara Lindqvist: That's fixable, we can normalise the identifiers. The bigger issue is the ledger has two sheets with different column names for the same thing. FY24 calls it project_id and amount_usd, FY25 calls it Proj ID and Cost Amount.

Priya Raman: Can we just use FY25?

Tom Okafor: The exec wants a year-on-year comparison, so no, we need both.

Sara Lindqvist: Then we append them and map the columns to one set of names. That's straightforward.

Priya Raman: Right. Now, the definition I care most about. Budget Variance is Budget minus Actual Cost. Positive variance means we are under budget. I have seen it done the other way round in a previous report and it caused a genuine argument in the committee.

Tom Okafor: Noted. Budget minus actual, positive is good.

Priya Raman: And the variance percentage is variance divided by budget, not divided by actual.

Sara Lindqvist: Understood. What should happen when budget is zero? The cancelled project has no budget left.

Priya Raman: Show it as blank rather than infinity. Don't show a huge percentage.

Nadia Haddad: On labour — my timesheets are not consistent. Some rows are in hours, some in minutes, and I think some are in hh:mm because of how the old system exported.

Sara Lindqvist: We can detect and normalise those. Everything becomes hours.

Nadia Haddad: Please do, and please tell me which rows you changed. I don't want hours silently altered.

Priya Raman: That's a good point generally. I want to see what was transformed.

Tom Okafor: There's something else. There are timesheet rows with no project code at all. Maybe twenty or thirty.

Nadia Haddad: Those are usually general site work that wasn't charged to a project yet.

Priya Raman: So they're real hours but not project hours. They should not disappear. Put them under an 'Unassigned' project so the total hours still ties out.

Sara Lindqvist: Good, that's an explicit Unknown member. I'll do the same for cost rows whose project isn't in the register.

Tom Okafor: Hold on, what cost rows aren't in my register?

Sara Lindqvist: There's at least one code, I think PRJ-1099, that's in the ledger but not the register.

Tom Okafor: That shouldn't exist. I'll check whether it's a typo or a project someone opened without telling the PMO. Leave it visible, don't drop it.

Priya Raman: Agreed, never drop cost.

Nadia Haddad: What about the duplicate timesheet? I saw TS-5000 twice with different hours.

Sara Lindqvist: Two rows with the same timesheet id but different hours is a conflict, not a duplicate. I'll flag it rather than pick one.

Priya Raman: Flag it and I'll have Finance adjudicate.

Tom Okafor: On status — my register says 'Complete', 'In Flight', 'On Hold', 'Cancelled'. The ledger has a status column too and it says 'Active' and 'Closed'. Those are not the same vocabulary.

Priya Raman: The register is the system of record for status. Use the register's values.

Tom Okafor: Thank you.

Sara Lindqvist: One question on Average Cost per Project. Do you divide by all projects in the register, or only the ones with cost? Because there's a project with no cost at all, PRJ-1009, and including it drags the average down.

Priya Raman: Only projects that have actual cost. If it has no cost it isn't spending yet.

Sara Lindqvist: That's a distinct-count of projects in the cost data, not a row count of the register. Fine.

Nadia Haddad: Do we need labour cost as well as labour hours? I have an hourly rate in the timesheet.

Priya Raman: Not for this version. Hours only. Labour cost is already in the ledger as a category and I don't want it counted twice.

Sara Lindqvist: That's important — if I joined timesheets to costs naively I would double count labour. I'll keep them as separate fact tables sharing the project dimension.

Priya Raman: Please do.

Tom Okafor: What about the reporting period? The brief says FY25.

Priya Raman: FY25 is the default view, but let me filter back to FY24. Don't hard-code the filter into the measures.

Sara Lindqvist: Then I need a proper date table. The dates in the ledger are in about five different formats, by the way.

Priya Raman: I know. That's why we're here.

Nadia Haddad: Mine are all dd/mm/yyyy at least.

Sara Lindqvist: Yours are consistent, yes. The ledger has dd/mm and mm/dd in the same column, which is genuinely ambiguous for the first twelve days of a month. I'll need someone to confirm the intent.

Tom Okafor: The ledger is exported from the UK finance system, so day first.

Sara Lindqvist: That helps but I'd still like written confirmation from Finance before I parse it that way, because the mm/dd-looking ones might be hand-entered.

Priya Raman: I'll confirm in writing. Put it on the action list.

Tom Okafor: Also one of the cost rows is in euros. PRJ-1007 has a subcontract line in EUR.

Priya Raman: We report in USD only. I don't want a mixed-currency total. Convert it or exclude it, but tell me which, and don't just add it to the dollars.

Sara Lindqvist: I'll flag it and leave it out of the USD total until Finance gives me a rate.

Priya Raman: Fine. What date should the FX rate be?

Sara Lindqvist: That's an open question for Finance.

Priya Raman: Noted, put it on the list too.

Nadia Haddad: For the report itself, what do you actually want to look at?

Priya Raman: An executive page with the headline numbers — total cost, total budget, variance, variance percent. Then a page that breaks cost down by portfolio and category, with a monthly trend line. Then something where a portfolio manager can click into a single project.

Tom Okafor: Drill-through to project detail, then.

Priya Raman: Yes. And keep it readable — no more than four or five pages.

Sara Lindqvist: Do you want month-over-month on the executive page?

Priya Raman: Yes, and year-to-date cost. Both monthly.

Nadia Haddad: Who signs off on the report?

Priya Raman: I do, for the definitions. Tom signs off that the project register is right.

Tom Okafor: Understood.

Priya Raman: Last thing. Nothing gets published to the workspace until I've seen the variance number reconcile against my own spreadsheet. I'll give you the control total.

Sara Lindqvist: Then I'll build a reconciliation check against your control total before anything is published.

Priya Raman: Good. Let's wrap up.
"""


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "sample_project"
    out.mkdir(parents=True, exist_ok=True)
    write_costs(out)
    write_labour(out)
    write_register(out)
    (out / "reporting_brief.md").write_text(BRIEF, encoding="utf-8")
    (out / "requirements_workshop_transcript.txt").write_text(TRANSCRIPT, encoding="utf-8")
    print(f"Wrote sample project to {out}")
    for f in sorted(out.iterdir()):
        print(f"  {f.name:46} {f.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()
