# Changelog

Each version here becomes a release on GitHub. The notes under a version are
what admins see in Settings → Software Updates before they click Update Now,
so write them for the people using the app.

## 5.2.23

**A second backup copy in Cloudflare R2**
- Settings → Backups has a new **Add a second copy in Cloudflare R2** section,
  with the steps. Paste the bucket's S3 API address and an R2 API token, then
  click **Connect R2**. R2 is free up to 10 GB; this app uses well under 1 GB.
- After that, every backup goes to Backblaze **and** Cloudflare R2, encrypted
  with the same recovery key, so there is nothing new to save.
- If one of them fails (like Backblaze's trouble this week), the backup still
  counts as long as the other copy was saved, and the page shows each copy's
  last backup and any problem. A failed copy is tried again within 10 minutes.
- **Show backups** lets you choose which copy to list, download or restore from.
  **Test restore** checks both.

## 5.2.22

**Cloud backup no longer gets in the way of updates**
- Updates now keep the cloud backup's settings on the server. Before, each
  update forgot them: cloud backup said "another server does the backups" until
  someone clicked **Use this server**, and the backup tool had to read every old
  backup again from Backblaze.
- If Backblaze can't serve a file, the cloud backup now stops within about a
  minute and says which file and that it's Backblaze's problem (see
  status.backblaze.com). Before, it waited 5 minutes and only said "Backblaze
  did not respond".
- If only the cloud backup fails during an update, the update now goes on with
  the checked local backup it always makes (the one it rolls back to). Settings
  → Cloud Backup shows the reason, and the next cloud backup catches up. Before,
  a Backblaze problem stopped every update.
- When Backblaze refuses because your account's daily free limit is used up
  (Caps & Alerts in Backblaze), the message now says so and that backups resume
  after Backblaze's daily reset. Before, it wrongly said Backblaze refused the key.

## 5.2.21

**Grouped clients count as one in "Clients owe you"**
- On the dashboard, **You hold for clients** and **Clients owe you** now treat a
  group (a client and the clients grouped under it) as one customer. The group's
  balances are added up first, and the group lands on one side by its total.
- Before, each client in a group was counted on its own, so a client could show
  as owing you even when its group holds more money than that overall.
- The totals at the top and Company position don't change.

## 5.2.20

**Your own exchange rate**
- The rate box on the dashboard now has **My rate**. An admin presses the pencil,
  types 1 USD = ? RMB and saves. Every ≈ RMB figure then uses it: Company
  position, the client totals, each client's Approx Total and the phone view.
- It shows the date it was set, so an old rate is easy to spot. The live rate and
  your latest exchange are still shown next to it for reference.
- The average of every exchange since the start is gone. Settings → Exchange rate
  lets you choose **My rate** or **Live rate**; if the one you choose isn't set
  yet, the other one is used.
- This only changes how totals are shown. Saved entries never change.

**Clients: who owes you and whose money you hold**
- The Clients card on the dashboard now shows **You hold for clients** and
  **Clients owe you** separately, each in ≈ RMB with the USD and RMB parts.
  Before, both were added into one net number, so a big deposit could hide a
  big debt. The net total and Company position are the same as before.

**Auto commission counts as company profit**
- The Auto commission form now has **Add to company profit**, ticked, with your
  company account already chosen. Untick it for a commission that shouldn't
  count. Commissions made before this update don't change.
- If that account doesn't hold the commission's currency, the commission is
  still made and a message says it wasn't added to company profit.

## 5.2.19

**Orders inside a statement**
- A client's statement can now be split into orders. Everything already in a
  statement is "Order 1", so nothing changes until you close an order.
- **Close order** (on the statement page) ends the current order and starts the
  next one. New entries go into the new order, and whatever balance is left
  carries over as its starting balance ("Brought forward").
- Pick which order to look at from the **Order** list above the filters, or
  choose **All orders** to see everything together. The dashboard still shows
  each client's full balance.
- The Export menu lets you choose **This order** or **Entire statement** (PDF,
  Excel or CSV). An order's PDF and Excel start with the balance brought forward.
- Admins can rename an order, change its closing date (entries dated between
  the old and new date move with it), reopen it, or delete an empty one.
- A closed order can only be changed by an admin. Other users can still see it.

## 5.2.18

**≈ RMB for each client on the phone**
- On a phone, the client list now shows each client's approximate total in
  RMB under their name, next to the USD and CNY balances. Before, it was only
  shown on a computer.

## 5.2.17

**Only real categories, and no more "Bad Request" when editing**
- Some entries added by bots had categories that aren't in the Category list
  ("accommodation", "transport", "travel", "deposit"). Editing them, for example
  changing the date, showed a "Bad Request" page.
- When you update, those entries are moved once into proper categories: hotels,
  taxis, trains and flights become **Travel**; the "deposit" becomes **Client
  Receipt**. Anything else unknown would become Uncategorized.
- The app now only accepts categories from the list, whether an entry comes from
  the form, a file import or the API. A bot that sends another category gets a
  clear error listing the allowed ones (see docs/API.md).
- If an entry's category is ever missing, the edit form shows "Choose a category"
  instead of failing.

## 5.2.16

**Redesigned dashboard and statement page** (same colours, cleaner layout)
- New, plainer look everywhere: the app now uses your device's own font (the
  old one only existed on Windows, so iPhones showed a fallback), with
  calmer corners and shadows.
- **Dashboard:** one "Company position" panel with the total in RMB, the USD
  and CNY figures and the exchange rates. Bank accounts, Suppliers and Expense
  accounts are now three equal cards that list every account with its balance,
  so nothing is hidden behind a click. Add or edit an account right in its
  card with the **+** and pencil buttons.
- **Clients** is a proper list: name, number of entries, last entry date and
  the USD, CNY and RMB balances in neat columns. Click a row to open it.
  Rename, delete and "take out of group" are in the **⋯** menu on each row.
  Groups show their total with the statements underneath.
- **New client** is one button: press it, type the name, press Enter. A name
  that already exists is refused with a message instead of an error page.
- **Search inside one client:** type the client's name first, then what you
  are looking for, for example `Golden Gate freight`. The start of a name is
  enough when only one client begins that way (`cairo samples`). You can also
  pick a client from the suggestions; it then shows as a small tag in the
  search box and every search stays inside that client until you press its ×.
- **Statement page:** the client name, tools and balances are one header card;
  the filters fit on one line; on phones each entry shows the description and
  amount on one line, with the date and buttons under it.
- Negative amounts now always read -$1,234.50 (never $-1,234.50), on screen
  and in the PDF.

## 5.2.15

**New app icon**
- The app now has its own icon in browser tabs and bookmarks: dark teal with
  three statement lines and a small glowing firefly. On iPhone, **Add to Home
  Screen** uses it too.

## 5.2.14

**Tidier statement page**
- Commission entries no longer offer **Auto commission** (a commission can't
  have its own commission). Instead a small company icon shows whether the
  commission is counted in the company profit: green means added, grey means
  not added. Hover it (or read the word under it on a phone) for details.
- An entry whose commission was already made shows a green check.
- The Type column shows one short label: the category (for example
  Commission, Shipping) or the exchange rate. The extra words "Regular",
  "Has Commission" and "Commission entry … already exists" are gone.
- **Export**, **History** and **Undo** are now icon buttons at the top right.
- **Undo now asks first.** It shows what the last change was (the entry, and
  for an edit the old and new values) and what undoing it will do. Nothing
  changes until you press **Yes, undo it**. If someone else made a newer
  change in the meantime, nothing is undone and the page shows you the newer
  change instead.
- The big **Back** button is gone. On a computer, use the **Dashboard** link
  above the client name. On a phone, a round **←** button sits at the top left
  of the statement, next to Export, History and Undo, and the client name gets
  the full width below them.
- The calculator and **Newest** buttons are the same size and have a softer
  shadow. On iPhone they now stay clear of the bottom edge.

**New phone menu**
- The side menu has icons, a clear highlight on the page you're on, a round
  close button, and **Log out** at the bottom. It slides in when you open it
  and slides back out smoothly when you close it.

## 5.2.13

**Newest entries on top, older ones load as you scroll**
- Client statements and expense accounts now open with the newest entry at
  the top. The balance on the top row is the current balance.
- Scroll down and older entries load by themselves, 30 at a time. No more
  pages to click through. **Load older entries** and **Show all** are at the
  bottom too.
- At the bottom you see how many entries are showing, and a note when you
  reach the very first entry.
- An **↑ Newest** button takes you back to the top.
- After you save, delete or undo, the page comes back with the same entries
  still loaded, and refreshing keeps your place.
- When you edit an amount, the balances on the rows above update straight
  away.
- PDF, Excel, CSV and print still list entries oldest first, like a normal
  statement.

**Smaller row buttons**
- Edit, Delete and Auto Commission are now small icons. Hover over one to
  see what it does.
- On phones each icon has a small word under it, and the buttons sit on the
  same line as the entry type, so each card takes much less space.

## 5.2.12

**Expenses look like the rest of the app**
- Expense accounts now use the same layout as client statements: a header
  with Back, Export and Account buttons, balance cards, the same filters, and
  a ledger with Edit and Del buttons on every row.
- **Add Entry** opens a pop-up with a clear "Money out / Money in" choice,
  like on client statements.
- Rename, currencies and Delete account are tucked into the **Account** menu.
- The balance cards update straight away when you add, edit or delete.
- Entries are shown 10 at a time with the newest first, like client
  statements. Pick 10, 25, 50 or All under **Lines**.
- The Expenses page shows totals across all accounts and a **New Account**
  button.
- On phones, entries show as easy-to-tap cards. The calculator is available
  on expense accounts too.

**One colour scheme**
- Expenses, Settings, the login page and the password page now use the
  app's teal colours instead of bright blue.

## 5.2.11

**Your company name everywhere**
- The company name from Settings → General → Login screen now also shows in
  page titles, page headings and on statement and expense PDFs, instead of
  "Firefly".
- PDFs use the logo you uploaded for the login screen. A white logo gets a
  dark backdrop so it stays visible.
- The default account for commission profit entries is the expense account
  with your company name.

**New: time zone**
- Super admins can pick the time zone in Settings → General. It is used for
  today's date on new entries and for every time shown in the app (backups,
  cloud backups, activity log, sign-ins, updates).
- Until you change it, the time zone stays Asia/Shanghai, as before.

## 5.2.10

**Backups on this server**
- Each backup now has a **Delete** button, so you can remove backups you no
  longer need one by one. You're asked to confirm first, and the deletion is
  recorded in the activity log.

## 5.2.9

**Only one server does cloud backups**
- If you restore a backup onto another machine, that machine no longer starts
  backing up to the cloud by itself. Its Backups tab says cloud backup is paused.
- To move cloud backups to that machine, a super admin clicks
  **Use this server for cloud backups**. The old server notices at its next
  backup and stops.
- Servers that already back up keep doing so after this update.

**Installing**
- `setup.sh` now also installs the updater, so Update Now works right after a
  fresh install.

**Password reset**
- `./setup.sh reset-admin-password` works again. It changes the password inside the
  running app, makes the account a super admin, lifts the wrong-password pause,
  and needs no restart.

## 5.2.8

**New: super admin**
- Everyone who was an admin becomes a **super admin** and can still do everything.
- **Admin** now means: login screen, cloud backup (back up and restore only),
  backups on this server, users, activity log and password.
- **User**: password only in Settings.
- Only a super admin can create or change super admins, and there is always
  at least one active super admin.

**Settings**
- Users, API tokens, the activity log and your password are now tabs in
  Settings. The top menu is just Dashboard, Expenses and Settings.

## 5.2.7

**Removed**
- GitHub backup is gone: the Settings card, the automatic schedule, its
  buttons and scripts. Cloud Backup (Backblaze) replaces it.
- When this version starts, it deletes the saved GitHub backup settings,
  including the GitHub access token, from the app.

Backups already in your GitHub repository are not touched; delete that
repository on github.com yourself if you no longer want it.

## 5.2.6

**Settings, redesigned**
- Settings is split into four tabs: Updates, General, AI and Backups. Only one
  shows at a time, so the page no longer grows into a long scroll.
- Updates shows your version and a Check button on one line, and only the
  last 3 updates (with "Show all" for the rest).
- Backups on this server are listed by date and reason ("Before update to
  5.2.5"), newest 5 first, with "Show older" for the rest. Download and
  Restore are always on screen, also on phones.
- Every section has its own Save button in the same place, and all buttons
  are the same size.
- Cloud Backup's connection settings fold away once it's connected; the old
  GitHub backup is folded away too.

**Fixed**
- Password managers no longer try to fill API keys, the company name or the
  Backblaze key ID with your login.

## 5.2.5

**Fixed**
- Backups could fail with "Permission denied" on servers where older versions
  had left receipts with no read permission (the app used to run as root,
  which ignores that). The app now repairs its data's permissions when it
  starts, and restored receipts always get normal permissions.

## 5.2.4

**Fixed**
- A white or very light logo on a see-through background vanished on the
  white login card. The login page now shows such logos on a dark
  background, and Settings > Login Screen says so. Dark logos, and logos with
  their own background, look the same as before.

## 5.2.3

Security update for the building blocks the app is made of. Nothing changes
in how you use it.

- Flask, Requests and WeasyPrint (which makes the PDF statements) updated to
  versions without known security problems.
- PDFs can only load the app's own logo, never web addresses or other files.
- Every package is now locked to an exact version, and GitHub checks them
  every week for new security problems.

## 5.2.2

**Improved**
- The top bar now shows your company name (from Settings > Login Screen) on
  the left and highlights the page you're on. Quick Submit uses the same menu.
- Shorter dashboard headline: "Client Statements" on one line.
- Settings has buttons at the top to jump to each section.
- You can pinch to zoom on phones again.
- Keyboard users get a "Skip to content" link, a clear outline on whatever is
  selected, and the phone menu closes with Escape.

## 5.2.1

Runs the app the way it should run on a server. Nothing changes in how you use it.

- Uses a proper web server (Gunicorn) instead of the built-in test server, so
  several people can work at the same time without slowing each other down.
- The app no longer runs as the server's all-powerful "root" user. If someone
  ever broke into the app, they could do much less.
- Docker now checks every 30 seconds that the app and its database answer,
  and marks it "unhealthy" if not.
- New servers set up with the updater cap the app at 768 MB of memory, so a
  big backup or export can't starve the rest of the server.

## 5.2.0

Security update. Nothing changes in how you use the app, except where noted.

**Safer**
- Passwords, keys and tokens are never shown in the activity log any more,
  and ones already in the log are hidden when this version starts.
- Resetting someone's password logs them out on every device. Changing your
  own password logs you out everywhere except where you are.
- After 5 wrong passwords in a row, logging in as that user from the same
  place is paused for 15 minutes.
- New passwords need at least 8 characters and can't be the username.
  Existing passwords keep working.
- Amounts like "Infinity" or absurdly large numbers are refused with a clear
  message.
- Exchanges made at the same moment for different clients always keep their
  own rate.
- Text that starts with = + - or @ is exported to Excel and CSV as plain text,
  so a spreadsheet can't run it as a formula.
- Fixed a way to delete files outside the receipts folder, and a way to run
  script on the admin activity page.
- Pages tell the browser not to cache financial data and not to be embedded
  in other sites.

API tokens work exactly as before.

## 5.1.2

**Fixed (phones)**
- Add Entry and Edit no longer scroll sideways (Safari's date box made the
  form wider than the screen), and the page behind them stays still.
- Short fields sit in pairs (Date and No., Currency and Direction, Type and
  Category), so the whole form fits on one screen.
- The photo box is a slim row instead of a big square.
- Save and Cancel are the same size, and Amount opens the number keypad.

## 5.1.1

**Fixed**
- Client statements: column headings now sit over their columns, and amounts
  and balances line up on the right so the numbers are easy to compare.
- Dashboard: the exchange-rate boxes line up with the balance boxes above them,
  and the empty grey circle under the USD totals is gone.
- Dashboard: on medium-size screens the totals no longer cover the section
  name (for example "Supplier Balances").
- Dashboard: clients are shown side by side instead of one per row with empty
  space next to them.
- Expense pages now use the same width as the rest of the app and line up with
  the menu.

## 5.1.0

The first numbered release. From now on, updates can be installed from Settings.

**New**
- Settings → Software Updates shows the installed version and any newer
  release with its notes. Click Update Now to install it. The app makes a
  recovery backup first, pauses saving for a few minutes while it restarts, and
  puts the previous version and your data back by itself if the new version
  doesn't work. Every attempt is listed in the update history.
- Admins see a notice at the top of the page when a new version is available.
- Encrypted cloud backups to your own Backblaze B2 bucket, every 30 minutes,
  with Test Restore, Download and Restore buttons.
- Admin switches to turn the AI receipt reading features on and off.
- Settings → Login Screen: change the company name, welcome line and logo
  shown on the sign-in page.

**Fixed**
- Buttons that were broken or hard to reach on phones.
- Backups could catch a receipt halfway through being saved or deleted.
