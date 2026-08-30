# Drop exported statements here

Anything in this folder is gitignored except this file. Export CSVs from each
account and put them here, then:

    finasst import statements/*.csv --account "Amex SimplyCash"

Import one account at a time so each file gets the right account name. The
import is idempotent, so re-running it on a file you have already loaded, or on
a fresh export that overlaps the last one, adds nothing twice.

Nothing here is committed, and nothing here leaves the machine.
