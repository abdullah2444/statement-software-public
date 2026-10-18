# Changelog

Each version here becomes a release on GitHub. The notes under a version are
what admins see in Settings → Software Updates before they click Update Now,
so write them for the people using the app.

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
