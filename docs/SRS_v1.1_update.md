# Additional Software Requirements: Requirements Update, Version 1.1

Small Shop Inventory Management System (AG Inventory). This update adds to the existing SRS
(`Shop_Inventory_SRS_v1.1.docx`). Requirements are grouped as functional requirements, business rules
and reporting requirements.

## Implementation status

All sections are built (sections 3 and 7 first, the rest in the next update).

| Section | Topic | Status |
|---|---|---|
| 1 | Product and category management (Excel import, product detail and sales history) | Done |
| 2 | Purchase management (confirmation adds stock, separate purchase returns) | Done |
| 3 | Inventory: FIFO and batch tracking, selling before expiry | Done |
| 4 | Serial number management | Done |
| 5 | Flexible selling price management | Done |
| 6 | Sales management (TT Number) and manual sales recording | Done |
| 7 | Payment management: due and partial payments | Done |
| 8 | Daily transaction reporting | Done |
| 9 | Number formatting and visual indicators | Done |
| 10 | Additional business rules | Done |
| 11 | Additional acceptance criteria | Done, see `docs/ACCEPTANCE.md` |

### How the remaining sections were built

**1. Excel import and product details**
- Products → Import from Excel (also on Categories). Templates download as .xlsx; .xlsx and .csv files are read
  without any extra program. The file is checked first and every row is shown as OK, Duplicate (code or
  barcode already used, or twice in the file) or Error with the reason, with a summary. Only correct rows are
  saved. The rows not imported download as an Excel file to correct and upload again.
- The product page has tabs: sales history (date, receipt, qty, price sold and difference, seller, customer,
  TT No., batches used, serial numbers), purchase history, stock movements, serial numbers and price changes.

**2. Purchases**
- "Save and confirm" ends the purchase: all lines go into stock at once as FIFO batches, with the expiry
  date and serial numbers typed on the line. There is no receiving step. Purchases confirmed before this
  update can still be received the old way.
- A return to the supplier is its own transaction (number PR-…), for one or more lines, each with a reason,
  linked to the original purchase. It can't exceed what was received, not yet returned and still in stock
  from that purchase, and it takes the goods out of that purchase's batches.

**4. Serial numbers**
- Switched on per product ("Track serial numbers"). Serial numbers are typed or scanned on the purchase
  (one per item, required to confirm), unique in the business, and linked to the product, batch and purchase.
- At the POS a scanned serial number adds its product; each serial-numbered line needs one valid serial
  per item. A serial can't be sold twice. Returns, cancellations, supplier returns, stock adjustments and
  transfers move the serial numbers too. Each serial number has a history page (purchased, sold, returned,
  current status). Stock already on hand can be given serial numbers on the product page.

**5. Flexible selling price**
- New permission "Change the selling price during a sale" (administrators always; give it to other roles in
  Admin → Roles). The cart's price box can then be changed. Each sale line keeps the default price, the
  price sold and the user who changed it. Reports: "Sold above / below the default price" and "Price
  variation by product" (total variation and its effect on sales in %).

**6. Manual sales**
- New permission "Record manual sales". Selling → Manual sale records the real date, the actual seller,
  customer, products, prices, payment and TT No. The person recording it is kept separately. It gets an
  M receipt number and a "Manual" label everywhere. The user must say whether the stock is deducted now
  (FIFO, with stock check) or was already taken out (no second deduction).
- The TT No. column is shown in sales history and is searchable.

**8. Daily report**
- Money & reports → Daily report: sales (count, total, cash, bank and mobile, fully / partly / unpaid,
  POS / manual, by seller, price variations, list of sales), purchases (count, total, products, returns,
  paid, supplier balances), inventory (received, sold, returned, damaged / expired / lost, adjustments,
  stock now, per product), expenses and the financial summary (revenue, cost of goods sold, gross and net
  profit, customer and supplier balances). Filters: date or date range, seller, POS / manual, payment
  method. Excel, Print / PDF, and every receipt or purchase number links to the transaction.

**9. Numbers and colours**
- All amounts and quantities use comma thousand separators (also in the POS and purchase screens, whatever
  the computer's language). Positive values such as profit are green with "+", negative values red with "−",
  zero grey, on the dashboard, reports, sale pages and the daily report.

### How sections 3 and 7 were built

**Section 3, FIFO batches**
- Every delivery received on a purchase becomes a stock batch with its purchase reference, date received,
  quantity received, quantity remaining and unit cost (after the purchase discount). An optional expiry
  date can be entered when receiving, or set on the batch later.
- Order of use: batches with an expiry date go first, the earliest expiry first; then the oldest batch
  received first (FIFO). This is how the shop sells before goods expire.
- A sale that needs more than one batch takes what it needs from the first batch, then the next.
- The cost of goods sold is the cost of the batches actually used. The product's average cost and the
  stock value are now the value of the remaining batches.
- Customer returns and sale cancellations go back into the batches the goods came from, at their cost.
- Returns to the supplier come out of the batches that purchase line brought in. If those goods were
  already sold, the return is refused with the quantity still in stock.
- Stock adjustments (damaged, expired, lost, correction) can name a batch; without one they use FIFO.
  Stocktake differences use FIFO; extra stock found becomes a new batch at the current cost.
- Branch transfers carry the batches with them (same cost, received date and expiry date).
- Every batch has its own history page (batch-level stock movement and costing history).
- Upgrade: the stock each branch already had became one opening batch per product at its average cost.
  A copy of the database was saved in `backups/` first (`shop-before-batches-<date>.db`).

**Section 7, due and partial payments**
- A sale can be fully paid, partly paid or unpaid. The amount paid at the POS is the first payment;
  more payments are added on the sale page until the balance is 0. Payments never create a new sale or
  touch stock.
- Each payment records its date, amount, method, TT Number or other reference, and the user.
- The sale page shows its payment status and full payment history. Sales history can be filtered by
  payment status and searched by TT Number.
- Stock leaves when the sale is confirmed, whatever was paid. Selling on credit (less than the full
  amount) needs a registered customer and the new **Sell on credit** permission (given to the roles that
  could already sell; the administrator can take it away).
- Supplier payments can also be partial, with method and TT Number / reference.
- Reports: outstanding customer balances, sales by payment status, customer payments, outstanding
  supplier balances, supplier payments.

---

## 1. Product and Product Category Management

### 1.1 Excel Import
- The system shall support importing products and product categories from Excel files.
- The system shall provide downloadable Excel templates for product and category imports.
- The system shall validate imported data before saving.
- The system shall identify duplicate product codes and invalid records.
- The system shall display import errors and allow users to correct and re-upload invalid records.
- The system shall provide an import summary showing successful, failed, and duplicate records.

### 1.2 Product Details and Sales History
- Each product shall have a dedicated detail view.
- The product detail view shall display product information, current stock, purchase history, sales history, and stock movement history.
- The system shall provide detailed information about every item sold, including sale date, quantity, selling price, seller, transaction reference, and applicable batch information.
- The system shall allow users to view all transactions associated with a particular product.

## 2. Purchase Management

### 2.1 Purchase Confirmation
- The purchase process shall end with saving and confirming the purchase transaction.
- The system shall not require a separate receiving confirmation step after purchase confirmation.
- Once a purchase is confirmed, the purchased quantities shall automatically be added to inventory and assigned to the appropriate stock batch.
- Each purchase batch shall maintain its purchase date, quantity, remaining quantity, and unit purchase cost.

### 2.2 Purchase Returns
- Returning purchased items shall be handled as a separate transaction after the original purchase has been confirmed.
- The system shall allow users to initiate a purchase return by referencing the original purchase transaction.
- Users shall specify the products, quantities, and reasons for returning items.
- The system shall ensure that returned quantities do not exceed the remaining eligible quantity from the original purchase.
- Confirmed purchase returns shall automatically reduce the corresponding inventory batch quantity.
- All purchase returns shall maintain a traceable relationship with the original purchase.

## 3. Inventory Management: FIFO and Batch Tracking for selling before expiry  *(in progress)*
- The system shall implement the First-In, First-Out (FIFO) inventory costing and stock consumption method.
- Products received at different times or with different purchase costs shall be maintained as separate inventory batches.
- Each batch shall maintain its purchase reference, purchase date, quantity received, remaining quantity, and unit purchase cost.
- During sales, the system shall automatically deduct stock from the oldest available batch first.
- When a sale requires quantities from multiple batches, the system shall automatically consume the required quantities from the oldest batches before moving to newer batches.
- The system shall maintain batch-level stock movement and costing history.
- The system shall calculate the cost of goods sold based on the actual batches consumed.
- The system shall support batch-level purchase returns and stock adjustments.

## 4. Serial Number Management
- The system shall support optional serial number tracking for products that require individual item identification.
- Serial number tracking shall be configurable at the product level.
- For serial-number-controlled products, users shall register individual serial numbers when receiving purchased items.
- Each serial number shall be unique within the system.
- The system shall associate each serial number with its product, purchase batch, and purchase transaction.
- During sales, the user shall select or enter the serial number of the specific item being sold.
- The system shall prevent selling the same serial number more than once.
- The system shall maintain the complete lifecycle of each serial-number-controlled item, including purchase, sale, return, and current status.

## 5. Flexible Selling Price Management
- The system shall allow authorized users to change the selling price during a sales transaction.
- The selling price shall not be restricted to the default product selling price.
- The system shall maintain the default selling price separately from the actual transaction selling price.
- The system shall calculate and record the difference between the default selling price and the actual selling price.
- The system shall record the user responsible for changing the selling price.
- The system shall support configurable permissions for users allowed to change selling prices.
- The system shall provide reports showing products sold above or below their default selling prices.
- The system shall calculate the total price variation and its effect on sales performance.

## 6. Sales Management and Manual Sales Recording

### 6.1 Sales Transaction
- The system shall allow users to record sales through the POS interface.
- Each sales transaction shall have a unique transaction reference number.
- The system shall provide a field for entering the TT Number (Telebirr Transaction Number or other transfer reference).
- The TT Number shall be stored with the corresponding sales payment transaction.
- The TT Number shall be searchable and retrievable from sales history.
- The system shall support cash and electronic payment references.

### 6.2 Manual Sales Recording
- The system shall support recording sales that were conducted outside the application.
- Authorized users shall be able to enter manual sales records for transactions that were not processed through the POS interface.
- Each manual sales record shall capture the actual sales date, product, quantity, selling price, payment information, and seller.
- The system shall identify such transactions as manually recorded sales.
- The system shall record the user who entered the manual transaction separately from the actual seller.
- Manual sales shall update inventory after confirmation, subject to stock availability and the applicable FIFO rules.
- The system shall maintain a clear distinction between system-generated POS sales and manually recorded sales.

## 7. Payment Management: Due and Partial Payments  *(in progress)*
- The system shall support fully paid, partially paid, and unpaid sales transactions.
- Users shall be able to record an initial payment and additional payments against an outstanding sales balance.
- The system shall automatically calculate the remaining amount due.
- The system shall maintain payment history for each sales transaction.
- Each payment record shall include payment date, amount, payment method, TT Number or payment reference where applicable, and responsible user.
- The system shall provide a report of outstanding customer balances.
- The system shall support partial payments against purchases and supplier balances as well.
- The system shall distinguish between transaction confirmation and full payment settlement.
- Inventory shall be deducted when the sale is confirmed, regardless of whether the sale is fully paid, partially paid, or unpaid, subject to authorized credit-sale rules.

## 8. Daily Transaction Reporting
The system shall provide comprehensive daily transaction reports covering all business activities. The daily report shall include:

### 8.1 Sales Transactions
- Total number of sales.
- Total sales amount.
- Cash sales.
- Bank and mobile banking sales.
- Fully paid sales.
- Partially paid sales.
- Unpaid sales.
- Manually recorded sales.
- Sales by individual seller.
- Selling price variations.

### 8.2 Purchase Transactions
- Total purchase transactions.
- Total purchase amount.
- Products purchased.
- Purchase returns.
- Amount paid and outstanding supplier balances.

### 8.3 Inventory Transactions
- Stock received.
- Stock sold.
- Stock returned.
- Damaged and expired products.
- Stock adjustments.
- Remaining stock quantities.

### 8.4 Expenses and Financial Summary
- Total daily expenses.
- Total sales revenue.
- Cost of goods sold.
- Gross profit.
- Estimated net operating profit.
- Outstanding customer balances.
- Outstanding supplier balances.

### 8.5 Report Filtering and Export
- Users shall be able to filter daily reports by date, seller, transaction type, and payment method.
- The system shall support printing and exporting reports to Excel and PDF.
- Users shall be able to view transaction-level details from the daily summary.

## 9. Number Formatting and Visual Indicators

### 9.1 Thousand Separator
- The system shall display monetary amounts and quantities using comma separators for thousands.
- Example: 1,000.00 instead of 1000.00.
- The formatting shall be consistent across forms, tables, dashboards, and reports.
- Calculations shall use numeric values internally, not formatted text.

### 9.2 Positive and Negative Value Indicators
- The system shall use visually distinct colors to represent positive and negative financial values.
- Positive values, such as profit and favorable price differences, shall use green.
- Negative values, such as losses and unfavorable price differences, shall use red.
- Neutral values shall use a neutral color.
- Color indicators shall be applied consistently across dashboards, transaction lists, and reports.
- Text labels or positive/negative signs shall accompany color indicators to ensure accessibility.

## 10. Additional Business Rules
1. All confirmed purchases shall automatically update inventory.
2. Purchase returns shall only be processed against previously confirmed purchases.
3. FIFO shall determine which stock batches are consumed during sales.
4. Products requiring serial number tracking shall not be sold without selecting a valid available serial number.
5. Actual selling prices shall be recorded independently of default product selling prices.
6. Every price variation shall be traceable to the responsible user.
7. Manual sales shall be distinguishable from POS-generated sales.
8. The actual seller and the user recording a manual sale shall be recorded separately.
9. Each sales transaction shall support an optional TT Number or payment reference.
10. Partial payments shall not create duplicate sales or inventory deductions.
11. All additional payments shall be linked to their original sales or purchase transactions.
12. Daily reports shall include system-generated and manually recorded transactions.
13. All inventory and financial adjustments shall be auditable.
14. Monetary values shall be stored using appropriate decimal precision and displayed with comma thousand separators.

## 11. Additional Acceptance Criteria
- AC-016: Products and categories can be imported from Excel with validation.
- AC-017: Confirming a purchase automatically updates inventory.
- AC-018: Purchase returns can be processed separately after purchase confirmation.
- AC-019: FIFO correctly consumes the oldest stock batches first.
- AC-020: Batch-level purchase costs are correctly reflected in cost of goods sold.
- AC-021: Serial-number-controlled products require valid serial numbers during sales.
- AC-022: Authorized users can change selling prices during sales.
- AC-023: Price differences are correctly recorded and reported.
- AC-024: Manual sales can be recorded with the actual seller's information.
- AC-025: Manual sales and POS sales are distinguishable in reports.
- AC-026: TT Numbers can be recorded and retrieved from sales transactions.
- AC-027: Product details display individual sales and purchase histories.
- AC-028: Daily reports correctly summarize sales, purchases, returns, expenses, and payments.
- AC-029: The system correctly handles fully paid, partially paid, and unpaid transactions.
- AC-030: All monetary amounts display comma thousand separators.
- AC-031: Positive and negative financial values are visually distinguishable.

Note: AC-016 and AC-017 above reuse two numbers from the first acceptance list (`docs/ACCEPTANCE.md`).
In the checklist they are listed as *U-AC-016* and *U-AC-017* to keep them apart.

## Important implementation clarifications
These three design decisions are kept:
1. **FIFO by batch:** every purchase creates a stock batch. Sales consume the oldest available batch first, preserving the actual cost of each sale.
2. **Flexible selling price:** keep the default price, actual selling price, and price difference as separate values. This makes price-performance reporting possible.
3. **Manual sales accountability:** record both the actual seller and the user who entered the sale. This avoids confusing transaction responsibility.

One important distinction: for manual sales that occurred outside the application, the system should
require the user to specify whether the physical stock was already deducted or whether the system must
deduct it upon recording. This prevents double deduction.
