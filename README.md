# TrueStore Operations

## Setup (local development)
```
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000

A local SQLite file (`data/app.db`) is created automatically on first run for
party settings, coordinators, groups, and paid/unpaid overrides. Persists across restarts.

`python app.py` uses Flask's built-in development server — fine for this, not for real
traffic. See "Deploying to production" below before hosting this for real.

## This pass: 3 real bugs found and fixed from your screenshot + report

**1. "Convert Deliveries to Invoice" showing nothing — working as designed,
but with zero explanation.** Newly-created deliveries default to `pending`
until admin approves them, and only approved receipts are eligible for
invoicing (by design — admin should confirm a delivery before committing it
to a formal invoice). The receipts *do* show up in `/deliveries` regardless
of status, which is exactly why this looked broken rather than "not ready
yet." Fixed: the tool now explicitly tells you when a customer has pending
receipts awaiting approval, with a direct link to go approve them, instead
of just showing an empty table. Verified: `pending_count: 1` correctly
reported before approval, resolved to a real eligible line immediately
after.

**2. Category filter genuinely wasn't matching for one vendor category —
and it was a bug I introduced, not a filtering-logic bug.** Traced this
properly with real JS execution (not just re-reading code): both the
customer and vendor filters mechanically work correctly. The actual problem
was a **data mismatch I caused myself** — I seeded the canonical vendor
category as "Century" (corrected spelling) while your real vendor data has
"Cetntury" (the original typo, preserved verbatim from the Excel import).
They never matched, so that one filter silently returned zero results.
Fixed the seed value, and added a one-time migration that automatically
corrects any database that already has the wrong "Century" entry — verified
both the fresh-install case and the already-affected-database case.

**3. Delivery receipt PDFs showed a split (US/DU) item as two duplicate-
looking rows.** The web cart UI already merges these into one row with a
combined badge (built two rounds ago) — the PDF generator was never updated
to match, so it still rendered straight from the two separate database rows
per item that the approval logic genuinely needs internally. Fixed: PDFs
now merge by item description before rendering. Customer Copy shows one
row with the combined quantity and no US/DU mention at all. Office Copy
shows one row with the combined quantity plus a compact "US 6 / DU 2"
annotation instead of two rows. Verified by generating real PDFs and
extracting their actual text content to confirm no duplication.

## Field-staff scoping, vendor-product mapping, customer-specific price memory

Three admin-configurable systems, all tested with real numbers.

**1. Admin assigns specific customers/vendors to a field-staff user.** New
page at `/admin/users/<username>/assignments`. Default behavior: a
field_staff user with no assignments yet sees *everything* (so nobody gets
locked out just by having the feature exist) — the restriction only
activates once admin assigns at least one. Verified: before assignment, saw
all 82 customers; after assigning 2, saw exactly those 2; admin's own view
stayed unrestricted throughout; removing one assignment correctly dropped
it back to 1.

**2. Admin maps which products belong to which vendor**, so New Purchase
only offers a deliberate, relevant list instead of the full catalog. New
page at `/parties/vendors/<id>/products`. Once a vendor is selected in New
Purchase, the product search automatically narrows to that vendor's mapped
products, **plus anything a field-staff member has personally added**
(pending or approved) even if it hasn't been formally mapped yet — since
they know firsthand it came from that vendor. Verified: before mapping, a
vendor's product search returned 0 items; after mapping 2, returned exactly
those 2; a separate, unmapped vendor still correctly returned 0.

**3. Customer-specific price memory** on New Bill, matching how the
original Excel-based system worked: the same product can be sold to two
customers at two different prices, and next time either of them buys it
again, the system suggests their own last price as a recommendation —
accept it or override it, and whatever's actually used gets saved as the
new last price. A product a customer has never bought before always shows
the plain catalog price, never a price inferred from someone else's
history (no bias toward first-time buyers).

Verified with real execution (not just code review — actually ran the
browser-side JS in a headless DOM against the live server):
- First-time item for any customer: empty price history, shows plain
  catalog price.
- Customer A bought at catalog price (₹135) → correctly remembered as
  ₹135.00 for them specifically.
- Customer B negotiated ₹120 for the *same product* → remembered as ₹120.00
  for them, completely independent of customer A's price — proving this is
  genuinely per-customer, not a shared override.
- The actual cart-building code, not just the API: selecting customer B
  again and adding that same product **automatically pre-filled the rate
  field with their remembered ₹120**, correctly flagged as "recommended,"
  not silently applied — the person creating the bill still sees it and can
  accept or type something else, exactly as specified ("up to admin to
  accept it or not").
- The rate field in the New Bill cart is now genuinely editable (it was
  previously read-only, just displaying the catalog price) — a real,
  necessary capability that didn't exist before this feature.

## Convert Deliveries to Invoice — new professional workflow

A complete new feature (admin-only) at `/deliveries/invoice`: review
everything delivered to a customer across possibly several delivery
receipts, group it however makes sense, and turn each group into a real
GST invoice with its own invoice number.

**How it works:**
1. Pick a customer (same search/category-filter/browse pattern used
   everywhere else in the app).
2. Every uninvoiced line from that customer's *approved* delivery receipts
   loads into one combined table — regardless of which receipt it came
   from, and regardless of US/DU tag (that flag only governs inventory
   bookkeeping, not whether the customer owes for the item).
3. **Group the lines** — "Auto-group by GST%" (one group per tax rate),
   "Auto-group by amount" (give it a ₹ cap, it bin-packs items into groups
   under that cap), or freely reassign any line's group by hand — the three
   ways you described (GST-wise, amount-wise, or mixed) are really the same
   per-line group assignment underneath, just with two auto-fill shortcuts
   on top of full manual control.
4. Each group gets its own **live invoice-number suggestion and
   availability check** (reusing the same "last + one" logic and
   availability API built for New Bill), editable before confirming.
5. **Create Invoice** builds a real bill using the exact same pricing engine
   New Bill uses (not a re-derived approximation — literally the same
   `billing_engine.build_bill_lines()` call), generates the real PDF, and
   the converted lines vanish from the working table since they're no
   longer eligible.

**Verified end-to-end, including the trickiest cases:**
- Two items delivered across two *separate* receipts, converted together
  into one invoice — total landed at exactly ₹327.53, matching hand
  calculation to the paisa.
- The *same product* delivered across two separate receipts correctly
  **merged into one bill line with the summed quantity** (5, not two
  duplicate lines of 3 and 2).
- **Stock is never touched again at invoice-creation time** — a delivery's
  US-tagged items already moved stock back at approval; converting those
  same items to an invoice later does not double-decrement. Verified: stock
  read identical before and after invoice creation.
- **Visibility for both roles, as required**: `/deliveries` now shows an
  Invoiced column — "not invoiced," "3/5 invoiced," or "fully invoiced"
  with direct download links to the resulting invoice(s) — and this is
  visible to *both* the admin and the field-staff member who created the
  original delivery, confirmed on both role's views.

## This pass: Invoice # auto-populate/availability check + admin-managed category lists

**Invoice # auto-population.** The New Bill page now pre-fills the Invoice #
field with the real suggested next number (same "last + one" logic the
backend already used at creation time, now exposed as a preview via
`/bills/api/next-invoice-number`) and shows a live "✓ Available" /
"✗ Already used" indicator as you type or edit it, debounced against
`/bills/api/invoice-availability/<no>`. Verified live: suggested the correct
next number, correctly flagged a real existing invoice as taken and a fresh
one as available.

**Customer/Vendor category lists are now admin-managed, not just derived
from whatever's in the data.** New `customer_categories` /
`vendor_categories` tables, seeded exactly with the requested customer list
(SBI, BOB, PNB, BOI, UBI, IB, Canara, Gramin, Agency, Office, Gem) and the
real vendor brands already in the catalog. Settings now has an Add/Remove
UI for both lists (mirroring the existing Money Request Reasons pattern) —
verified live: added "HDFC," confirmed it appeared in the filter API,
removed it, confirmed it disappeared.

- Every category filter dropdown (New Bill, New Purchase, New Delivery,
  Wallet lookup, the Customers/Vendors management list, and every "⊞ Browse"
  popup) now pulls from these canonical lists instead of deriving from
  whatever category strings happen to exist in the data.
- The Add/Edit Customer and Add/Edit Vendor forms now use a dropdown for
  category instead of free text — but **existing records with a category
  outside the canonical list (e.g. a customer whose bank was recorded as
  "indian" or "cbi" during the original import) are preserved, not hidden**:
  the dropdown shows their actual value with a "(not in the standard list)"
  label rather than silently dropping it. Verified with a real customer on
  the "indian" category.
- Filtering matches **case-insensitively** throughout, since the original
  imported data is lowercase (`sbi`) while the canonical list displays in
  the requested casing (`SBI`) — verified: filtering by `SBI` (uppercase)
  correctly returned all 22 customers actually stored as `sbi` (lowercase).
  The shared `card_picker.js` component was fixed the same way, since it's
  reused by every "⊞ Browse" popup.

### A real pre-existing bug found and fixed while testing this
`/parties/customers/new` was returning a 500 error — tracing it back, a
route decorator for `/parties/customers/new` had been mistakenly stacked
onto the `customer_ledger` function (which requires a `customer_id`) during
an earlier round when the Ledger feature was added, rather than staying on
its own `parties_customer_form` function. This meant creating a new
customer has been broken since the Ledger pages were introduced two rounds
ago, and nothing in this project's test suite had exercised that exact URL
since. Fixed, and confirmed the full regression catches it going forward
(added to the standard route sweep).

## This pass: 3 delivery UX fixes

**1. PDF copy visibility is now role-scoped.** Field staff only see "Customer
Copy" (both on the creation success screen and the saved-deliveries list);
admin only sees "Office Copy". Verified on both surfaces for both roles.

**2 & 3. Bifurcation redesign — these turned out to be the same underlying
fix.** The original implementation split a bifurcated item into two separate
cart entries immediately, which caused both problems at once: no live
syncing between the US/DU fields (so incrementing DU didn't decrement US),
and the same product visually appearing twice in the cart, looking like a
duplicate-add mistake.

Rebuilt the cart data model so a bifurcated item is **one entry carrying
both `us_qty` and `du_qty`**, not two entries:
- The split popup's US and DU fields are now **live two-way bound** — typing
  or incrementing either one immediately recalculates the other so they
  always sum to exactly the item's total quantity, with no way to get them
  out of sync. Verified by executing the actual JS: live-typing DU=2
  immediately flipped the US field to 2 *before* clicking Apply, and typing
  US=1 immediately flipped DU to 3.
- The cart table now shows **one row per item** regardless of whether it's
  split, with a compact "US 2 · DU 2" badge when it is. Verified: after a
  4-unit item was split, `cart.length` stayed at 1, not 2.
- The backend still needs one row per stock-type to do its job correctly
  (approval only touches US rows) — that conversion now happens invisibly
  at submit time (`buildSubmitLines()`), not as something the user sees
  while building the cart. Verified end-to-end through actual submission
  and approval: a split 1 US / 3 DU delivery correctly stored as 2 DB rows,
  and approval correctly moved stock for only the 1-unit US portion.
- Applied identically to both the New Delivery and Edit Delivery pages —
  the edit page additionally merges existing DB rows back into single cart
  entries on load, so editing a previously-split delivery shows the same
  single-row-with-badge view instead of exposing the storage-level split.

## This pass: 5 fixes/features, prioritized as requested

**1. Qty spinner "1.01" bug — real root cause found and fixed.** `min="0.01"` paired with `step="1"` makes HTML5's native stepper snap to `min + N×step` (0.01, 1.01, 2.01...) instead of clean integers. Fixed across all 6 affected inputs.

**2. "Service Manager" label — actual bug found this time.** Every page's top-nav badge was rendering `{{ logged_in_user.role }}` directly (the raw internal key `field_staff`), never routed through the label mapping — a global `role_labels` context processor didn't exist, so the earlier ROLE_LABELS rename never reached the UI. Fixed at the source and corrected all 24 affected pages. Verified live: zero raw role-key leaks anywhere in that role's portal.

**3a. US/DU bifurcation popup.** Clicking the US/DU toggle now opens a modal asking for a quantity split (e.g. 5 total → 3 US + 2 DU), splitting the single cart line into two. Verified by actually executing the JS in a headless DOM (not just reading the code): a 5-qty line correctly split into US qty 3 + DU qty 2, and an invalid split (quantities that don't sum correctly) was correctly rejected with the cart left unchanged.

**3b. Multiple purchases per money request + partial vendor payments.** This was the big one:
- **Multi-purchase linkage**: a money request can now have *any number* of purchases against it (previously hard-capped at one). Verified: two separate purchases (₹236.00 + ₹590.00) both succeeded against the same approved ₹1,000 request, correctly summed to ₹826.00 spent, and field-staff cash balance landed at exactly ₹174.00. A third purchase pushing the total over budget correctly triggers a warning without blocking the purchase.
- **Real partial-payment tracking**, not a binary toggle: `purchase_bills` now has `amount_paid` and a `status` that's genuinely derived (`unpaid` → `partial` → `paid`), backed by a full `purchase_payments` history table. Verified a 3-payment sequence on one ₹1,180.00 purchase (₹400 cash → partial, ₹300 from vendor wallet → partial, correctly debiting the vendor wallet to exactly ₹700 remaining, ₹480 cash → paid) landed exactly right, with over-payment correctly rejected at each step.
- **Removed another `CHECK` constraint** (`status IN ('paid','unpaid')`) that would have hit the exact same bug class as the `users` table did earlier — this one specifically tested with a real migration: simulated a pre-existing database on the old schema, ran the migration, and confirmed both a previously-paid and previously-unpaid row backfilled correctly (`amount_paid` set appropriately) and immediately worked with the new partial-payment system afterward.
- The old one-click "mark fully paid" button still exists but now routes through the same payment-recording path for consistency — marking a purchase back to *unpaid* is no longer supported through it, since that would mean silently erasing real payment history rather than recording a correction.

**4. Category filters — re-verified with actual proof.** Set up a real headless-browser JS execution environment (curl can't run JavaScript, so earlier "verification" only proved the markup existed, not that it worked) and confirmed all four category dropdowns genuinely populate with live data. If this still isn't showing up on your end, it's very likely something environment-specific (browser cache, a different page than I tested) — let me know the exact URL and I'll dig further rather than guessing.

## Field Staff renamed, category filters everywhere, delivery approval workflow, vendor wallet, salary system (this pass)

**1-2. "Service Manager" label** — role display renamed everywhere (internal key stays `field_staff`, no data migration needed, nothing else changed).

**3. Category filters on the fast inline search, not just the "⊞ Browse" popup** — the popup already had them; the faster-to-use autocomplete search box didn't. Added a category `<select>` next to every customer/vendor search field (New Bill, New Purchase, New Delivery, Wallet lookup).

**8. Pending Products can be edited by admin before approving** — turned out the capability already existed (`update_product_by_id` never touches the `approved` flag); just needed the visible Edit link on the pending queue.

**5. Cash ledger filters** — type (credit/debit) + date range on the Money Requests page's cash history.

**2 & 4. Delivery receipts: US/DU stock toggle, approval workflow, dual-copy PDFs, full Edit/Delete.** This was the biggest change:
- Every delivery line now gets a **US (Update Stock) / DU (Don't Update)** toggle at creation — DU is for the government-office "extra/informal supply" case you described, where an item is physically delivered but shouldn't count as a real inventory movement.
- Deliveries now go through **admin approval** before touching stock at all — verified: creating one with a US item and a DU item left stock completely untouched; only after admin approval did the US item's stock actually decrement (DU stayed untouched throughout).
- **Two PDF copies generated automatically**: Customer Copy (clean) and Office Copy (shows each item's US/DU status plus an explanatory note about what DU means) — exactly the "office copy may clearly show which is extra" behavior you asked for.
- **Edit/Delete now exist** for deliveries, mirroring the Purchase Edit/Delete pattern — verified the trickiest case (editing an *already-approved* delivery): old US-item stock correctly reversed, new US-item stock correctly reapplied, in one step. Delete correctly reverses stock too if the delivery was approved, and is a no-op on stock if it was still pending (nothing had moved yet).

**6. Salary expense system**, built on top of the existing field-staff cash ledger rather than a parallel system, as you asked:
- Admin sets a **joining date + monthly salary** on any user from Manage Users.
- `/salary` shows every employee with a joining date, flags who's **Due** for the current period (no payment record yet), lets admin **Approve** an amount (doesn't touch their balance yet — this is the "review and decide" step), then **Mark Paid** once cash actually changes hands, which is the moment their cash balance actually updates.
- Verified the full state machine end-to-end: Due → Approved (balance stays 0, correctly deferred) → Paid (balance became exactly the approved amount).
- Since this reuses `cash_ledger` (originally built for field_staff money requests), **every role can now see their own cash balance** on `/money-requests` (renamed "My Cash Balance" for non-field_staff), not just field_staff — verified a sales-role employee could see their salary credit there, while still correctly blocked from the money-*request*-creation flow (that stays field_staff-only).

**7. Customer and Vendor ledger pages, plus a new Vendor Wallet.**
- New "Ledger" page per customer/vendor (`/parties/customers/<id>/ledger`, `/parties/vendors/<id>/ledger`): wallet balance, total billed/purchased, currently-unpaid total, and a combined chronological timeline of every bill/purchase plus every wallet transaction in one place.
- **Vendor Wallet is a new, fully separate system** mirroring the customer one (advances/credits you've given a vendor). Found and fixed a real gap while building this: **there was no way to mark a Purchase as paid anywhere in the UI at all** — `db.set_purchase_bill_status` existed but nothing called it. Added the same clickable-status-pill + payment modal pattern the dashboard uses for bills, including **"Pay from vendor wallet balance."**
- Verified end-to-end: insufficient vendor-wallet balance correctly rejected; ₹500 credited → ₹236.00 purchase (2×₹100 + 18% GST) paid from wallet → balance landed at exactly ₹264.00; both the purchase and both wallet entries show correctly on the vendor's ledger page.

## Real item-level Split, Wallet-as-payment, Accountant read access, favicon
- **Split now does real per-item math, not estimation.** Turns out
  `mtsBills.py`'s `get_bucket_store` was never an optimizer — it's a manual
  per-line letter assignment a human typed into an Excel column, and the
  "algorithm" (`get_bill_num`) just suggests a part *count*
  (`ceil(total/cap)`). Built exactly that as a real web UI: for any bill with
  real line items (created in this app), you assign each item to a part via
  a dropdown, and each part's tax/margin is computed for real from its
  actual assigned items using the same engine New Bill uses — not divided
  proportionally. Hand-verified: a 2-item bill split into Part A (₹270.00,
  margin ₹60.00) + Part B (₹670.00, margin ₹71.50) — margins sum exactly to
  the original. Legacy bills without full line-item data still fall back to
  the previous proportional-amount split, since there's no real per-item
  data to split.
- **Wallet can now pay an invoice directly.** The Mark Paid modal offers
  "Pay from wallet balance" whenever the customer has enough credit —
  debits the wallet by the exact invoice amount and marks it paid in one
  step. Verified: ₹10,000 balance → paid a ₹7,600.93 invoice → balance
  landed at exactly ₹2,399.07. Insufficient balance is rejected cleanly
  (no partial payment state). Known limitation: flipping a wallet-paid
  invoice back to unpaid doesn't auto-reverse the wallet debit — use the
  Wallet page to manually credit it back if that happens by mistake.
- **Accountant can now view (read-only) Saved Bills and Saved Purchases** —
  Edit/Delete/Split actions correctly hidden for that role in both the UI
  and the underlying routes (not just hidden buttons — the routes
  themselves stayed locked to admin/sales or admin/purchase/field_staff).
- **Favicon** — dark ink background, cream "TS" monogram, matching the
  app's palette. SVG primary + ICO/PNG fallbacks for broader browser/OS
  support, added to all 22 pages.
- **Deployment configs updated for a multi-app droplet**: `deploy/` files
  now default to port 8010 (not the commonly-taken 8000) and include an
  explicit pre-flight checklist (check free ports, check existing nginx
  `server_name`s) so this doesn't collide with anything already running.

## Field Staff can now use their own pending products immediately
Previously, a field-staff-submitted product was invisible everywhere
(including to the person who added it) until admin approval — which meant
they couldn't actually record the purchase or delivery they were adding it
*for*. Fixed:

- `db.list_products_for_user(username)` returns every approved product,
  **plus that same user's own still-pending submissions** — so New Purchase
  and New Delivery's product search shows it to them right away, tagged
  "pending admin approval" (in both the autocomplete dropdown and the
  card-picker popup), while it stays invisible to everyone else, including
  other field-staff users' own pending items.
- Nothing about **using** a pending product needed fixing — Purchase/Delivery
  creation already looked products up by ID with no approval check; the gate
  only ever existed at the *search* layer. Once search access was fixed,
  everything else already worked.
- **Verified the full lifecycle end-to-end**: field_staff adds a product →
  immediately visible in their own Purchase and Delivery search (tagged
  pending) → invisible to a separate sales-role user in the meantime →
  successfully created a Purchase against it (stock correctly went 20→25) →
  successfully created a Delivery against it → admin approves → now visible
  to the sales user too → the purchase/delivery created while it was pending
  are completely untouched (same totals, same line items, same stock) —
  nothing needed re-linking, since they always pointed at the real product
  row directly.
- Also cleaned up a latent bug found while in here: `db.py` had two
  definitions of `list_products()` (one stale, silently shadowed by the
  real one later in the file, since Python just keeps the last definition)
  — removed the dead one.

## Logout, infinite scroll, and a proper Field Staff home (this pass)

**Logout audit** — found a real gap: only 2 of 22 pages had a visible logout
link (`index.html`, `bills.html`). Every other page required navigating back
to one of those two first. Fixed across all 20 remaining pages (`settings.html`
handled separately, different header structure) with a consistent
username/role + logout element, right-aligned via a normalized `.top-nav`
flex rule. Verified: logout link present on every page except `login`/`setup`
(which correctly don't need one), and functionally confirmed — logging out
clears the session and blocks the dashboard afterward.

**Infinite scroll + scroll-jump buttons** — one shared component
(`static/infinite_scroll.js`/`.css`), included on every page:
- Floating ↑/↓ buttons appear automatically on any page tall enough to
  scroll (bottom-right, smooth-scroll) — zero setup per page, just including
  the script is enough.
- `InfiniteScroll.attach('#tbodyId')` progressively reveals a long table's
  rows in chunks of 40 as you scroll near the bottom (via
  `IntersectionObserver`), instead of the browser laying out hundreds of
  rows at once. Wired into the four biggest lists: Products (803+ rows),
  Saved Bills, Customers/Vendors, Saved Purchases. Works against tables the
  server already renders in full — no new pagination endpoints needed.

**Field Staff now has a proper home, not just one page doing double duty**:
- A consistent tool-nav bar (Money Requests / New Purchase / Saved Purchases
  / New Delivery / Saved Deliveries / Add Product) now appears on all five
  of their pages, not just the one you happened to land on.
- **Cash Ledger History was built in `db.py` two passes ago but never
  actually shown anywhere** — fixed. `/money-requests` now shows the full
  credit/debit history underneath the requests table, not just the current
  balance. Verified end-to-end: a ₹500 "Purchase" request → approved (credit)
  → linked purchase created (debit) → both entries correctly show in the
  history table.

## Custom field values now visible everywhere, not just the edit form
- **Products list, Customers/Vendors list**: each defined custom field gets
  its own column, populated via one bulk query per page load (not N+1 —
  `get_custom_field_values_bulk`), verified: a "Warranty Months" value of 24
  set through the real edit form shows up correctly in its own column on
  `/products/manage`, with other rows correctly blank.
- **Card-picker popups** (product/customer/vendor browsing everywhere it's
  used: New Bill, New Purchase, both Edit pages, New Delivery): custom field
  values now appear as extra lines on each card, via a shared
  `customFieldLines()` helper in `card_picker.js` so it's one implementation
  reused across all 7 call sites, not duplicated per page.
- Cache-busting bumped to `?v=3` for this change.

## Role-scenario verification (this pass)
- **Purchase role**, logged in as an actual `purchase`-role user (not admin):
  created a purchase, opened its Edit page (200), changed a quantity, and
  confirmed the recompute is correct (3×₹100 + 18% GST = exactly ₹354.00).
- **Field Staff**: manual purchase-number override confirmed working from
  this role specifically — `MANUALFIELDPO1` used verbatim instead of
  auto-generating a `PB...` number, `created_by` correctly recorded.

## Deploying to production

### 0. On a droplet that already runs other apps (do this first)
You mentioned 5-6 other apps are already running on this droplet — two things
matter before anything else, or you risk taking down something else or
colliding with it:

- **Check which ports are already in use**:
  ```
  sudo ss -tlnp | grep LISTEN
  ```
  Every `127.0.0.1:XXXX` or `0.0.0.0:XXXX` line is taken by something already
  running. `deploy/truestore.service` and `deploy/nginx_truestore.conf`
  default to **8010** — if that's free, use it as-is; if not, pick another
  free port and change it in **both** files together (they must match).
- **Check which domains/subdomains nginx already serves**:
  ```
  grep -r server_name /etc/nginx/sites-enabled/
  ```
  Don't point TrueStore at a domain another app's config already claims.
  Use a dedicated subdomain (e.g. `truestore.your-domain.example.com`) —
  `deploy/nginx_truestore.conf` is written as a **new, independent server
  block** meant to sit alongside your existing ones, not replace anything.
  `sudo nginx -t` (before reloading) will immediately flag any conflict.
- The systemd service (`deploy/truestore.service`) runs as its own isolated
  process under its own service name — starting/stopping/restarting it never
  touches your other apps.

### 1. Install (production dependencies included automatically)
`requirements.txt` installs `gunicorn` on Linux/Mac or `waitress` on Windows
automatically (via `pip`'s environment markers — no manual choice needed):
```
pip install -r requirements.txt
```

### 2. Set environment variables
```
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export FLASK_ENV=production
```
`FLASK_ENV=production` turns on the `Secure` flag on session cookies (only sent over
HTTPS) — leave it unset for local `http://` testing, or the browser will silently
refuse to store the session cookie. Setting `SECRET_KEY` explicitly (rather than
relying on the auto-generated `data/secret_key.txt`) means sessions survive a
redeploy/container rebuild instead of invalidating everyone's login.

### 3. Run behind a real WSGI server, not `python app.py`
```
# Linux/Mac
gunicorn -w 4 --capture-output -b 127.0.0.1:8010 wsgi:app

# Windows
waitress-serve --host=127.0.0.1 --port=8010 wsgi:app
```
Port `8010` matches the `deploy/` example configs — change it (in all three places:
here, `deploy/truestore.service`, `deploy/nginx_truestore.conf`) if step 0 found it
already taken.

`--capture-output` matters for gunicorn specifically — without it, the app's own
`print()`/logging output (including the auto-migration message on first boot) is
silently swallowed rather than appearing in your logs.

Bind to `127.0.0.1`, not `0.0.0.0` — put a reverse proxy in front (next step) rather
than exposing the WSGI server directly to the internet.

An example systemd service (Linux, runs gunicorn as a managed, auto-restarting
background service) is at `deploy/truestore.service`.

### 4. Put a reverse proxy in front for HTTPS
The app itself doesn't terminate TLS — that's nginx/Caddy's job, sitting in front and
forwarding plain HTTP to the WSGI server on localhost. An example nginx config is at
`deploy/nginx_truestore.conf`. Quick version with [certbot](https://certbot.eff.org/):
```
sudo apt install nginx certbot python3-certbot-nginx
# copy deploy/nginx_truestore.conf to /etc/nginx/sites-available/, edit server_name,
# symlink into sites-enabled, then:
sudo certbot --nginx -d your-domain.example.com
```
Certbot edits the nginx config in place to add the certificate paths and sets up
auto-renewal.

### 5. Back up the database regularly
`data/app.db` is now your live database (bills, stock, customers, users — everything),
not just a cache. Back it up:
```
python backup_db.py              # snapshot now, keep the last 14
python backup_db.py --keep 30    # keep more history instead
```
Uses SQLite's online backup API (safe to run while the app is live — a plain file copy
can grab a half-written page mid-transaction and produce a corrupt backup).

Linux/Mac cron (daily at 2am, keep 30 days):
```
0 2 * * * cd /opt/truestore/receivables_dashboard && /opt/truestore/receivables_dashboard/.venv/bin/python backup_db.py --keep 30
```
Windows: Task Scheduler → Create Task → Trigger "Daily" → Action "Start a program",
program `python.exe`, arguments `backup_db.py --keep 30`, start-in the project folder.

**Also back up `data/backups/` itself somewhere off the same machine** (cloud storage,
another server) — a local-only backup doesn't help if the disk itself fails.

### What's already handled for you
- **SQLite WAL mode** — enabled automatically on first `init_db()` call. Readers and
  writers don't block each other, which matters once there's real concurrent traffic
  from multiple gunicorn/waitress workers.
- **Login rate limiting** — 5 failed attempts per IP locks that IP out for 5 minutes
  (in-memory, so it resets on restart and isn't shared across multiple gunicorn
  workers; fine as a first line of defense, upgrade to Flask-Limiter + Redis if you
  need it enforced globally across workers).
- **Secure session cookies** — `HttpOnly` always; `Secure` when `FLASK_ENV=production`.
- **Auto-migration race safety** — with multiple workers, only one claims the startup
  migration (via an atomic lock file); verified under `gunicorn -w 4` that migration
  runs exactly once and the resulting data is correct, not duplicated or partial.
- **Rotating file logs** at `logs/app.log` (5MB × 5 files) once `app.debug` is off.

## Custom field attributes
- `/settings/custom-fields` — admin defines extra attributes per entity type
  (Product / Customer / Vendor), each with a label and type (text or number).
  Storage key is auto-generated from the label (slugified), shown alongside
  the label so it's clear what's actually stored.
- Defined fields **automatically appear** on the corresponding Add/Edit form
  (`/products/new`, `/products/edit/<id>`, `/parties/customers/...`,
  `/parties/vendors/...`) — no extra wiring needed per field, they render
  from whatever's currently defined in Settings.
- Deleting a field removes its stored values too (no orphaned rows) —
  verified the edit page for an entity that had a value under a since-deleted
  field reloads cleanly rather than erroring.
- **Deliberately no DB-level `CHECK` constraint** on `entity_type`/`field_type`
  — validated in Python instead. This is a direct lesson from the `users`-table
  bug found earlier in this project: baking an enum into a `CHECK` constraint
  at table-creation time breaks the moment you need to add a new value later,
  and SQLite reports that failure as the same error type as a genuine
  uniqueness violation, which is exactly what caused the confusing
  "already exists" bug when `field_staff` was added as a role.
- Verified end-to-end: defined a product field (Warranty Months, number) and
  a customer field (Preferred Delivery Day, text), confirmed each renders
  only on its own entity's form (not the other's), saved real values through
  the actual edit forms, confirmed the values persist and reload correctly,
  and confirmed vendor fields work identically.

## Core logic
- Reads `data/sales_log.xlsx` (columns C, D, F, G, H, I, J, K, L, O — Date, Invoice #,
  Customer, Total Amount, Margin, Payment Status, Payment Breakup, Taxable Amount,
  Taxable Amount Breakup, Candidate).
- **Base dataset**: rows where column O ("Candidate") = "Candidate". If column O has
  no data anywhere in the sheet (an older-format file), falls back to the original rule
  — rows where column I ("Payment Status") = "unpaid".
- Payment Breakup (column J) values like `560|560|560|560|620` split the invoice into
  that many sub-invoices named with the original invoice number + A, B, C... Each
  sub-invoice's Amount = its breakup value; Margin is split proportionally. The
  original unsplit line is dropped.
- Taxable Amount (column K) is split the same way, using column L's own breakup when
  it has a matching number of legs, otherwise split proportionally like Margin.

## Settings (⚙ button)
- **Coordinators** — managed list, seeded with Anil Kumar, Binay Kumar. Add more anytime.
- **Groups** — managed list, same pattern as coordinators (add via the "Add group" form).
- **Party directory** — every customer from the sheet is auto-listed. Set Contact number,
  Group by (dropdown, sourced from the Groups list), Location, and Coordinator (dropdown,
  sourced from the Coordinators list) per party — saved to SQLite, not the Excel file.

## Margin / Taxable toggle (top of dashboard)
- Switches the second metric column (in both views, plus totals) between Margin
  (default) and Taxable Amount. The "Amount" column always stays the same
  (from column G/J) regardless of this toggle.

## Filters (top of dashboard, all combinable)
- Search by party name
- Status: Unpaid / Paid / All (Paid = invoices you've manually marked paid)
- Period: date range on invoice date
- Coordinator, Group by — both dropdowns pull live from Settings
- Amount range: <1,000 / 1,000–2,000 / 2,000–5,000 / 5,000–10,000 / Above 10,000
  (applied per invoice line, after splitting)

## Two views
- **Grouped by customer** — collapsible ledger view, real `<table>` markup (fixed-width
  columns via `<colgroup>`) so it renders identically and predictably in print/PDF.
- **Invoice-wise** — every invoice line shown flat, each with a checkbox; a live bar
  above the table shows the checked count/amount/margin as you check/uncheck (client-side).

## Roles
| Role | Can do | Cannot do |
|---|---|---|
| `admin` | Everything | — |
| `sales` | Dashboard, New/Edit/Delete/Split bills, add/edit customers, browse/add/edit products, Wallet | Purchases, vendors, users, catalog upload |
| `purchase` | New/Edit/Delete purchases, add/edit vendors, browse/add/edit products | Dashboard, sales bills, customers, Wallet, users |
| `accountant` | Dashboard (read + mark paid/unpaid), Wallet | Creating/editing/deleting/splitting bills, Bills/Purchases pages, master data, users |
| `field_staff` | Money requests, purchases (GST or non-GST), delivery receipts, add products (pending approval) | Dashboard, Bills, editing existing products, managing customers/vendors/users |

## Field Staff role + 3 workflows (this pass)
A new `field_staff` role, built for staff who request cash, buy on the
business's behalf, and deliver goods — separate from `sales`/`purchase`.

- **Money requests** (`/money-requests`): field staff request an amount +
  reason (admin-configurable list at Settings → Money Request Reasons,
  seeded with Purchase/Fuel/Delivery expense/Repair/Miscellaneous) + note.
  Admin approves/rejects. **Approval credits the requester's personal cash
  ledger** — a new running balance per staff member, same pattern as the
  customer Wallet but for cash-in-hand instead of receivables.
- When the reason is "Purchase" and the request is approved, a **"Create
  Purchase" button appears** linking straight to New Purchase with that
  request attached. Creating the purchase **debits the same amount back out**
  of their cash balance and links the two records together — verified
  end-to-end: ₹1000 requested → approved (balance +1000) → ₹590 purchase
  created against it (balance → 410, exactly 1000−590).
- **GST / non-GST purchases**: New Purchase now has a Bill Type toggle.
  Non-GST skips tax entirely — verified: 5×₹100 → exactly ₹500.00, no CGST/SGST.
- **Delivery receipts** (`/deliveries`): pick a customer + products (quantity
  only, no pricing — this documents what was physically handed over, not a
  bill), generates a PDF, stored separately from Sales/Purchase.
- **Product creation pending approval**: field staff can add new products,
  but they land as `approved=0` and are **invisible to New Bill/New
  Purchase's product search** until an admin approves them at
  `/products/pending` — verified both states (hidden before, visible after).

### Two real bugs found and fixed while building this
1. **A schema bug that would have broken every future role addition**: the
   `users` table had a `CHECK(role IN (...))` constraint written at table-
   creation time. Adding `field_staff` to the Python role list did nothing
   for existing/fresh databases, since SQLite enforces the constraint
   independently — every attempt to create a `field_staff` user failed with
   a misleading "username already exists" error (SQLite reports CHECK and
   UNIQUE violations as the same exception type, and the error handling
   conflated them). Fixed by removing the constraint (role validity is
   enforced in Python instead, so no schema change is needed for future
   roles) and added a migration that safely rebuilds the table for
   already-existing databases without losing any users.
2. **A redirect loop**: `field_staff` (and `purchase`) logging in would have
   bounced forever, since the default post-login destination and every
   access-denial redirect both pointed at the receivables dashboard, which
   those roles can't see. Added a role-aware `/home` landing route and
   repointed every "back to home" nav link at it.

## Multi-select product browsing + cache-busting (this pass)
- The card-picker popup now supports `multiSelect` mode (used for products
  everywhere it's browsed — New Bill, Purchase, both Edit pages, Delivery):
  clicking an item adds it and keeps the picker open (with a brief green
  flash + a running "Done (N added)" count) instead of closing after one
  pick. Customer/vendor pickers are unchanged (still close on select, since
  you only pick one).
- All static assets now carry a `?v=2` cache-busting query param — if a
  change to `card_picker.js`/`mobile.css` doesn't seem to show up after a
  redeploy, this is exactly the class of bug that causes it (stale cached
  JS in the browser), now addressed at the root.

## Party & Product CRUD, category filters (this pass)
- `/parties` — Customers/Vendors list (tabbed), Add/Edit forms. IDs are
  immutable once created (bills reference them directly); everything else
  is editable.
- `/products/manage` — product catalog list, Add/Edit forms.
- **Category filters use real data from your billdesk.xlsx**, not something
  invented: column A ("Super Search") holds bank/group tags for customers
  (`sbi`, `bob`, `pnb`, `indian`, `agency`, `canara`, `gem`, `boi`, `ubi`,
  `gramin`, `cbi`, `office`, `other`) and supplier-brand tags for vendors
  (`Domes`, `Infinity`, `Kangaro`, `CobraFiles`, `Cetntury`) — now captured
  during migration and filterable both on the management list pages and in
  the Browse-as-cards picker everywhere it's used. Products filter by
  `sheet`, same as before.
- Found and fixed a real bug while wiring this up: the VENDOR-sheet migration
  loader was matching header names (`customerID`/`Customer_Name`) that don't
  exist in that sheet (it's actually `vendorID`/`Vendor_Name`) — it happened
  to still work by column-position coincidence, but was one column-order
  change away from silently pulling the wrong data. Now matches by the
  correct header names.
- Verified end-to-end: add/edit/duplicate-rejection for customers, vendors,
  and products, plus category filtering on all three.

## Qty/amount spinner step
- Every quantity and amount/cost-rate input across New Bill, Edit, New
  Purchase, Purchase Edit, Split, and Wallet now steps by **1** on the
  browser's up/down arrows (was 0.01). Typed decimal values are still
  accepted where it makes sense (e.g. fractional quantities) — only the
  spinner increment changed.

## Zero-setup data ownership
- The app **auto-migrates on startup** if the database is empty and your real
  `data/billdesk.xlsx` + `data/sales_log.xlsx` are present (they are, in this
  delivery) — 812 products, 85 customers, 5 vendors, 827 bills load
  automatically the moment the server starts, before anyone even logs in.
  The only manual step left is creating the first admin account at `/setup`.
- The manual "Run migration from Excel" button in Settings still exists —
  that's for *re-syncing* after you later upload updated files, not a
  required first step anymore.

## Database migration (Excel → SQLite)
- **The database is now the live store** for bills, stock/products, and
  customers/vendors — Excel files (`sales_log.xlsx`, `billdesk.xlsx`) are no
  longer read at request time; they're import sources and human-readable
  backups only. This closes the concurrent-write-corruption risk that came
  with using `.xlsx` files as a live datastore for a multi-user hosted app.
- **Run it**: Settings page → "Run migration from Excel". Idempotent — safe
  to re-run after uploading an updated sheet; products/customers/vendors are
  upserted by natural key, bills are matched by file name and only new ones
  are added.
- Split bills are stored one row per leg (matching how `record_room` already
  stores them) rather than one row with pipe-delimited breakup columns —
  cleaner and removes a whole class of parsing at read time.
- Paid/unpaid status and payment date now live directly on the bill row —
  the old `invoice_status` override table is no longer needed for anything
  migrated through this path.
- 15 pre-existing duplicate invoice-number+date rows were found in the real
  `sales_log.xlsx` (a legacy data-quality issue, not introduced by this
  migration) — the migration keeps the latest occurrence of each and reports
  the rest under `duplicates_skipped` so they can be reviewed in the source
  sheet.
- Verified: migrated totals reconcile exactly against the original
  Excel-based dashboard (₹303,750 total / ₹105,605 margin / ₹258,397
  taxable / 102 candidate lines / 29 customers, both ways).

## Mobile-responsive pass (Milestone 6 — final item)
- Fixed a genuine gap: `bills.html` was missing its viewport meta tag entirely
  (every other page had it) — without it, mobile browsers render the page at
  desktop width and let the user pinch-zoom, defeating the point.
- One shared `static/mobile.css`, linked on every page, covering the patterns
  that repeat everywhere rather than fixing each page ad hoc:
  - **Tables** get horizontal scroll instead of breaking the page layout at
    narrow widths (a `display:block; overflow-x:auto` trick on the `<table>`
    itself — no markup changes needed across the 8 templates with tables).
  - **Buttons/links** get a minimum 40px tap target below 720px.
  - **Nav/filter/field rows** get `flex-wrap` + row-gap backstops.
  - **Modals** (payment date, PDF viewer, card picker) fill more of a small
    screen instead of floating a fixed-width box that forces zooming.
  - Base font/heading/padding scale down slightly below 480px.
- Plus targeted fixes on the main dashboard specifically (highest-traffic,
  most complex page): the filter bar's search field had a hardcoded 190px
  width that would overflow a narrow phone screen (now flexes to 100%), and
  the selection bar (Invoice-wise view's "N selected · Download" bar) wasn't
  wrapping at all (now wraps cleanly with the download controls dropping to
  their own row).
- Verified: every page still returns 200 and correctly references
  `mobile.css` after the change; both new static assets serve correctly.

## Card view for products/customers/vendors (Milestone 5)
- Every place you'd previously only get a text-search dropdown (New Bill's
  customer + item fields, New Purchase's vendor + item fields, Edit's item
  field, Wallet's customer lookup) now also has a **⊞ Browse** button next
  to it, opening a searchable card grid (24 per page, with its own search
  box) as an alternative way to find what you're looking for.
- Built as one shared, reusable component (`static/card_picker.js` +
  `static/card_picker.css`) rather than five separate implementations —
  each page just calls `CardPicker.open({items, renderCard, onSelect, ...})`
  with its own dataset and card layout.
- Verified: static assets serve correctly, and the picker is wired into
  all five surfaces (checked via markup presence in each rendered page).
- Also fixed while here: `/wallet`'s customer list was still reading from
  the old `billdesk.xlsx`-based loader (stale since the DB migration) —
  now reads from the database like everywhere else.

## Purchase Edit (closes the gap flagged in Milestone 4)
- `/purchases/edit/<purchase_no>` — mirrors the Sales Edit pattern exactly:
  full line-item recompute, correct stock delta (reverses the old purchase's
  stock addition before applying the new one — tested: qty 5→8 moved stock
  8→11, not 8→16), regenerates the receipt PDF/JSON, date stays fixed for
  the same reason as Sales Edit (avoids relocating `purchase_room` files).
- Hand-verified: 5×₹90 → ₹531.00 total, edited to 8×₹95 → ₹896.80 total,
  both exact.
- Edit link added to `/purchases/list`.

## Purchase portal (Milestone 4)
- **No source code existed for this side** — `orderSummary.py`/`mtsBills.py` are
  entirely sales-side; this is designed fresh, reusing the same tax-calc engine
  (`billing_engine.compute_tax`) and UI patterns as Sales for consistency.
- `/purchases` — pick a vendor, add items with quantity and cost rate (per-unit,
  excl. GST — pre-filled from the product's last known cost, editable), same
  live GST calc as New Bill. On submit: **increases** stock (opposite of a
  sale), updates each product's `cost_price` to the latest purchase cost
  (simple latest-cost model, not weighted-average), and writes a receipt
  PDF + JSON to `purchase_room/<mm_dd_yyyy>/` (separate from `record_room`,
  which stays sales-only).
- `/purchases/list` — search/filter saved purchases, with Delete (reverses the
  stock this purchase added — tested: 13→3 exactly. Does **not** revert
  `cost_price`, since it reflects the latest known cost regardless of which
  purchase set it — noted in the delete confirmation).
- Manual purchase-number override works the same way as Sales' manual invoice
  number; auto-assigned as `PB{year}{seq}` otherwise.
- Gated to `admin`/`purchase` roles — verified a `sales`-role user is
  correctly blocked (302) from every `/purchases/*` route.
- Not yet built: Edit (mirrors the Sales Edit pattern closely, deferred to
  keep this pass focused and tested).

## Sales portal (Saved Bills — Milestone 3)
- `/bills/list` — search/filter saved bills (invoice #, customer, status), with
  PDF/Edit/Split/Delete actions per row.
- **Edit**: full line-item recompute (same tax/margin engine as New Bill) for
  bills created in this app (they have product-linked `bill_lines`). For bills
  migrated from your original `sales_log.xlsx` (no line-item detail available),
  the page shows a clear notice and their existing line items read-only —
  status/date can still be changed from the main dashboard. Editing correctly
  reverses the old stock impact before applying the new one (tested: qty 1→3
  on a known item moved stock 2→0, not 2→-1).
- **Delete**: soft-delete (bill stays in the DB with `deleted=1`, just hidden)
  and restores stock for any linked line items (tested: restored 2→3 exactly).
- **Split**: proportional split by user-specified amounts (must sum to the
  bill's total) — creates one new bill per part (invoice # + lowercase letter,
  matching the real convention), each with its own `record_room` JSON/PDF,
  margin and taxable amount allocated by each part's share of the total, then
  soft-deletes the original. **This is a scoped-down version of your real
  bucket-balancing logic** (`get_bucket_store` in `orderSummary.py`, which I
  haven't ported) — it splits the bill's totals proportionally rather than
  recommending an optimal split count or dividing individual line items. Good
  enough for "split this ₹9,000 bill into three ₹3,000 pieces," not a
  replacement for the original algorithm if you need that specifically.
- Manual invoice-number override (mentioned under New Bill) applies here too
  implicitly — split legs are always named `{invoice}{letter}`, matching your
  real system.

## Bills page (New Bill — Milestone 1 of 4)
- `/bills` — pick a customer and add items (both searched live from `data/billdesk.xlsx`,
  with real stock/price/GST/cost pulled per row), see the tax/margin calc update live,
  then submit.
- On submit, this reproduces `orderSummary.py`'s math exactly (verified against your
  real `TS2026AA0111` sample down to the cent): `rateWithout_gst`, GST bucketing with
  CGST always == SGST (half-tax rounded `ROUND_HALF_UP`), margin = `qty * (rate - cost)`,
  amount-in-words via `num2words(lang='en_IN')`. It then:
  - writes `record_room/<mm_dd_yyyy>/<Invoice>_<ddmmyyyy>.json` (same lettered-field
    format your real invoices use) + a sibling `_CostReport.json`,
  - decrements stock (`Quantity` column) in `billdesk.xlsx` per line sold — does *not*
    touch `CurrentQTY`, since that field is your desktop tool's own cart-entry
    scratchpad and this web form replaces that mechanism entirely,
  - appends a full row to `sales_log.xlsx` (all columns A–O, including K taxable /
    O "Candidate"), so the bill shows up on the main dashboard immediately,
  - generates a PDF and serves it at `/bills/pdf/<fileName>`.
- **Manual invoice number**: optional field on New Bill — leave blank for
  auto-assignment (scans the DB for the last invoice number and increments
  its last 4 digits), or type your own; rejected with a clear error if it
  already exists.
- Reads/writes products, customers, and stock decrement directly against
  the database (`db.list_products()` / `db.get_customer()` /
  `db.decrement_product_stock()`) — no longer touches `billdesk.xlsx` at
  request time. Uploading a new `billdesk.xlsx` via New Bill's catalog
  upload re-runs the product/customer/vendor import automatically.
- **PDF layout is a placeholder** (`bill_pdf.py`, built with fpdf2) — not a copy of your
  real "True Store" template, since those `.docx` files (`templateBook/taxInv_new.docx`
  etc.) weren't provided. `build_final_data()`'s output already uses your exact
  docxtpl field names, so wiring in the real templates + LibreOffice-headless
  conversion later only touches this one module.
- **Not yet built**: Edit, Split, Delete (planned next, per the agreed build order —
  New has to land solidly first since the others all edit an existing bill).

## Auth & roles (new)
- **First run**: every route redirects to `/setup` until an admin account is
  created — no hardcoded credentials anywhere. Session cookie signing key is
  read from a `SECRET_KEY` env var if set (recommended for real hosting),
  otherwise generated once and persisted to `data/secret_key.txt`.
- **Roles**: `admin` (everything, including `/admin/users` to add/disable
  other accounts), `sales` (dashboard + Bills), `purchase` (the `/purchases`
  stub — real functionality not built yet), `accountant` (dashboard read +
  can toggle paid/unpaid + wallet). Every existing route is now gated by
  `@login_required` + `@role_required(...)` in `app.py` / `auth.py`.
- Passwords hashed with Werkzeug's `generate_password_hash` (already a Flask
  dependency — no new package needed).

## Wallet (customer credit/advance balance)
- `/wallet` — look up a customer, see their running balance, add a credit
  (advance payment) or debit (used against a bill) entry, see full history.
  Stored in SQLite (`wallet_ledger` table) — independent of whether customer
  master data itself has moved off `billdesk.xlsx` yet.
- Not yet wired to bill creation (e.g. auto-debiting wallet balance when
  used to pay a new bill) — that's a natural next step once this is in use.

## Build status: all six original milestones complete
1. ~~Full DB migration for bills/stock/customers/vendors off Excel~~ — done.
2. ~~Sales portal: list/search saved bills, edit, delete, split~~ — done
   (split is a proportional-amount approximation, see above — not the exact
   bucket-balancing algorithm from `orderSummary.py`).
3. ~~Purchase portal: real vendor-bill / stock-in flow~~ — done (New +
   List + Delete; Edit deferred).
4. ~~Card view for products/customers/vendors~~ — done.
5. ~~Mobile-responsive pass across every page~~ — done.

## Known gaps / good next steps
- ~~Purchase Edit~~ — done, see above.
- **Real Split algorithm** — current Split is a proportional-amount divider,
  not the bucket-balancing optimizer from `orderSummary.py`'s
  `get_bucket_store` (never ported — I haven't read that function).
- **PDF layout** — `bill_pdf.py`/`purchase_pdf.py` are original, functional
  layouts, not your real "True Store" `.docx` templates (never provided).
  `build_final_data()` already emits your exact docxtpl field names, so
  swapping in the real templates + LibreOffice-headless conversion is a
  contained change to one module, not a rearchitecture.
- **Multi-tenancy** — explicitly out of scope per your answer early on
  (single business for now). The DB schema has no tenant/org concept, so
  this would need real design work if you ever host it for other businesses.
- **Wallet ↔ bills integration** — wallet balance isn't yet auto-applied
  when creating/paying a bill; it's a standalone ledger today.

## Invoice files (PDF + line items)
- Two lookup strategies, tried in order:
  1. **`record_room/` tree** — matches your real `mtsBills.py`/`orderSummary.py` layout
     exactly: `record_room/<mm_dd_yyyy>/<Invoice>_<ddmmyyyy>.json` for whole invoices,
     `record_room/<mm_dd_yyyy>/<Invoice>_<ddmmyyyy>/<Invoice><letter>_<ddmmyyyy>.json`
     per part for split ones (lowercase `a`, `b`, `c`... matching the real
     invoice-numbering convention — split legs in this dashboard are now named
     the same way, e.g. `TS2025AA0201a`). All path segments are resolved
     **case-insensitively** against what's actually on disk (see
     `record_room_lookup.py`'s `_ci()` / `resolve_case_insensitive`), since
     invoice-number casing can drift between creation and lookup. PDFs are
     expected next to their `.json` sibling with the same stem.
  2. **Flat `data/invoices/{fileName}.json` / `.pdf`** — fallback for setups
     without a full `record_room` tree.
- **Hover** an invoice number to see its line items (parsed from the JSON) in a
  tooltip. Fetched on demand and cached client-side.
- Click the 👁 icon next to an invoice number to **view** its PDF in an in-page
  modal. Shows a friendly message if the file isn't present.
- In Invoice-wise view, select rows and click **Download selected** to get a ZIP
  of each selected line's real PDF (split legs each have their own). Tick
  **Include summary PDF** to add a one-page summary (Invoice #, Customer, Date,
  Amount, Margin/Taxable Amount, Status, with totals) listing exactly the lines
  you selected.
- The JSON parser expects the lettered-field format (`itema`/`hsa`/`qa`/…/`aa` for
  item "a", `itemb`/… for item "b", etc., using the exact letter set from
  `record_room_lookup.ITEM_LETTERS` — skips `m`/`n`/`w`, which are reserved for
  MRP/rate fields) — see `invoice_files.py`.

## Paid/Unpaid toggle
- Every invoice line has a status pill (UNPAID/PAID). Click an UNPAID pill to
  mark it paid — a small modal asks for the payment date (defaults to today,
  editable). Click a PAID pill to flip it back to unpaid (clears the stored
  payment date). Both are stored as overrides in SQLite (source Excel is never
  modified), and immediately respected by the Status filter and totals. The
  payment date shows in small text under the pill whenever a line is paid.

## Print / Save PDF
- "Print / Save PDF" opens the browser's print dialog on exactly what's on screen
  (current filters/view respected). Choose "Save as PDF" as the destination.
  Filter controls, buttons, and checkboxes are hidden automatically; all customer rows
  auto-expand. The ledger table uses fixed percentage-width columns so long/short
  customer names never wrap into single-letter lines on narrower print pages.

## Files
- `app.py` — Flask routes (dashboard, filters, upload, settings, status toggle)
- `db.py` — SQLite storage for parties, coordinators, groups, status overrides
- `data_processor.py` — Excel parsing, split logic, filtering, grouping
- `templates/index.html` — dashboard UI (filters, both views, print-safe table layout)
- `templates/settings.html` — party, coordinator & group management UI
- `data/sales_log.xlsx` — your sample data (swap out or use "Load sheet")
