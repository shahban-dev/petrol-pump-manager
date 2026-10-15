# Petrol Pump Manager — User Guide

**Version 1.0.0**

Petrol Pump Manager is an offline Windows program for a petrol station.
It keeps your daily register, nozzle readings, fuel stock, customer credit,
expenses, and reports — all on your computer. No internet is needed.

- Station: Zeishan Petroleum Service (PSO)
- Currency: Pakistani Rupees (PKR)
- Time zone: Asia/Karachi
- Language: English and Urdu. You can switch in Settings.

---

## 1. The sidebar pages

The left sidebar has these pages. Click a page to open it.

1. **Dashboard** — a quick summary of the current day.
2. **Daily Register** — the full daily register, like your paper register.
3. **Nozzle Readings** — enter meter readings for each nozzle.
4. **Fuel Stock** — how much fuel is in the tanks right now.
5. **Fuel Deliveries** — fuel you received from the supplier.
6. **Customer Credit** — credit tickets given to customers.
7. **Customer Payments** — payments customers made against their credit.
8. **Suppliers** — your fuel suppliers.
9. **Other Materials** — engine oil, filters, and other items you sell or use.
10. **Expenses** — daily running costs (electricity, wages, etc.).
11. **Reports** — daily, weekly, and monthly reports.
12. **Backups** — save and restore copies of your data.
13. **Settings** — station name, language, prices, nozzles, backup folder.

---

## 2. First-time setup (manager only)

The first time you open the program, enter these once:

1. Open **Settings**.
2. Enter the station name: **Zeishan Petroleum Service**.
3. Check the nozzles. The station has 4 nozzles:
   - Nozzle 1 — PG+ (petrol)
   - Nozzle 2 — PG+ (petrol)
   - Nozzle 3 — HSD (diesel)
   - Nozzle 4 — HSD (diesel)
4. Enter today's fuel prices (petrol and diesel).
5. Enter the **opening cash** in the drawer.
6. Enter the **opening stock** of each fuel in liters.
7. Enter the **opening meter readings** for each nozzle.
8. Set the backup folder (see Section 7).

Do this carefully. These numbers are the base for everything else.

---

## 3. The daily routine

Every business day follows the same steps.

### Step 1 — Open the day

1. Go to **Daily Register**.
2. Click **Open Day**.
3. Yesterday's closing cash and stock are carried forward automatically.
   You do not type them again.

Note: the day does not close at midnight by itself. A sale made at 00:20 at
night still belongs to the open day. The manager decides when the day closes.

### Step 2 — Enter nozzle readings

1. Go to **Nozzle Readings**.
2. For each nozzle, type the new meter reading.
3. The program checks that the new reading is higher than the old one and
   calculates the liters sold automatically.

Enter readings at least once a day, before closing.

### Step 3 — Record fuel deliveries

1. Go to **Fuel Deliveries**.
2. Click **Add Delivery**.
3. Choose the product (petrol or diesel), enter liters and total cost.
4. Save.

Stock goes up as soon as you save the delivery.

### Step 4 — Issue credit tickets

1. Go to **Customer Credit**.
2. Click **New Ticket**.
3. Choose the customer, the product, and the liters (or amount).
4. If the customer pays part of the amount right now, enter that too.
5. Save.

The remaining unpaid amount stays as the customer's balance.

### Step 5 — Record customer payments

1. Go to **Customer Payments**.
2. Click **New Payment**.
3. Choose the customer and the amount.
4. Save.

The payment reduces what the customer owes. Old payments are never changed.

### Step 6 — Record expenses

1. Go to **Expenses**.
2. Click **Add Expense**.
3. Enter what it was for (electricity, wages, repairs, etc.) and the amount.
4. Save.

### Step 7 — Close the day

1. Go to **Daily Register**.
2. Click **Close Day**.
3. A checklist appears. Read each item and confirm:
   - All nozzle readings entered
   - All deliveries recorded
   - Credit tickets and payments entered
   - Expenses entered
4. When you confirm everything, the day closes. The program then makes an
   **automatic backup** by itself.

---

## 4. Corrections — what to do when you made a mistake

**A saved record is never edited or deleted.** This protects your accounts.

- If a reading, ticket, payment, delivery, or expense is wrong, make a
  **reversal or correction** record. The old record stays, and the correction
  cancels its effect. The full history is always visible.
- If a day is already closed and you must fix something in it, the manager
  must **reopen** the day first. Reopening needs a reason, which is recorded.
- Never try to fix numbers by typing over old ones. If something looks wrong
  and you are unsure, ask support.

---

## 5. Customers who buy on credit

1. **Customer Credit** page shows every credit ticket and each customer's
   total unpaid balance.
2. A ticket can be partly paid at the time of the sale. The rest is due later.
3. **Customer Payments** page records money collected later. Money collected
   from an old debt is **not** counted as new sales — it only reduces the debt.
4. If a customer's balance in the program is wrong from the start (an old
   opening balance), fix it with an audited opening-balance adjustment.
   Never type a new balance directly.

---

## 6. Reports

Go to **Reports** to see:

- The **Daily Register report** — same sections and order as the paper
  register.
- Summary reports for a week or a month.
- Profit/loss. If opening stock values or expenses are missing, profit is
  shown as an **estimate** until the missing numbers are entered.

---

## 7. Backups — keeping your data safe

Your data lives on your computer, not on the internet. A backup is a copy
of your data saved somewhere safe.

### Make a manual backup

1. Go to **Backups**.
2. Click **Backup Now**.
3. Wait until it says the backup is validated and finished.

### Google Drive folder

In **Settings**, you can choose a folder inside Google Drive for desktop.
Copies of backups are placed there. Note the honest rule:

- The program only says a backup is **in the Google Drive folder**.
- It does **not** say it is "backed up to the cloud", because it cannot be
  sure Google Drive has synced it. The status will say
  *"in Google Drive folder — sync unconfirmed"*.
- The program works fully offline. If Drive is not running, the local backup
  still happens.

### Restore from a backup

Restoring replaces your current data with the backup. The program protects
you:

1. It makes a **safety backup** of your current data first.
2. It checks the backup file is valid.
3. It asks for **explicit confirmation** before restoring.

### Retention

Old backups are cleaned up automatically after a set number of days. The
newest good backup is never deleted.

---

## 8. What to never do

These rules protect your data. Follow them always.

1. **Never delete the `-wal` or `-shm` files** next to the database.
   They hold recent work that is not yet in the main file. Deleting them
   destroys data.
2. **Never copy the database file while the program is running.**
   The copy will be broken. Use the **Backups** page instead.
3. **Never put the live database inside a synced folder** (like Google Drive
   or OneDrive). Syncing a live database file corrupts it. Only backup
   files go to the Drive folder.
4. Never change the computer clock backwards to "fix" times. If you see a
   clock warning at startup, fix the computer's clock instead (turn on
   automatic time) and contact support if it continues.

---

## 9. Problems and what to do

### The program will not start

1. Read the message on the screen. It is written in plain language.
2. If it says the database is damaged, **restore from your newest backup**
   (Backups page, or ask support).
3. If it warns the **computer clock is behind**, check the date and time on
   the computer and turn on automatic time.
4. The program **never deletes or repairs the database by itself.** If a
   message says the database was not changed, your data is still there —
   restore from a backup or contact support.

### Windows SmartScreen: "Unknown publisher"

On first install or first run, Windows may show:
*"Windows protected your PC"* / *"Unknown publisher"*.

This is because the program is not signed with a commercial certificate.
It is expected for this build. Click **More info**, then **Run anyway**.

### Power cut while the program is running

Nothing to repair. When you start the program again, everything you saved
is there. Anything that was not saved is gone — re-enter it.

---

## 10. Daily checklist for the manager

- [ ] Day opened
- [ ] Nozzle readings entered for all 4 nozzles
- [ ] Deliveries recorded
- [ ] Credit tickets issued today are entered
- [ ] Customer payments collected today are entered
- [ ] Expenses entered
- [ ] Day closed with the checklist confirmed
- [ ] Backup made (automatic after close — check the Backups page if unsure)

---

*Need help? Contact support. Tell them your station name, the date of the
problem, and what the screen said.*
