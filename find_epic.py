#!/usr/bin/env python3
"""
Resolve Capital.com epics by name — run before adding an instrument.

GER40 was configured by analogy with US30 and US100. It is not an epic on this
account: every request 404s, the run reports "received 0 bars", and the
instrument contributes nothing while looking configured. Guessing is the bug;
this is the fix.

    python find_epic.py dax germany

Credentials come from CAPITAL_API_KEY / CAPITAL_IDENTIFIER / CAPITAL_PASSWORD,
which live only as Actions secrets — so this runs inside a workflow, like the
backtest does.
"""
import os
import sys

from strategy.capital_feed import CapitalComFeed


def main(terms: list) -> int:
    if not terms:
        print(__doc__)
        return 2
    missing = [v for v in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_PASSWORD")
               if not os.getenv(v)]
    if missing:
        print(f"missing credentials: {', '.join(missing)}", file=sys.stderr)
        return 2

    feed = CapitalComFeed(os.environ["CAPITAL_API_KEY"],
                          os.environ["CAPITAL_IDENTIFIER"],
                          os.environ["CAPITAL_PASSWORD"],
                          epic="US500",
                          demo=os.getenv("CAPITAL_DEMO", "true").lower() != "false")

    found = 0
    for term in terms:
        markets = feed.search_markets(term)
        print(f"\n{term!r}: {len(markets)} match(es)")
        for m in markets:
            print(f"  {m['epic']:<22} {m['name'][:44]:<46} {m['type']:<14} {m['status']}")
        found += len(markets)

    # Nothing found is a real answer, not a successful run: the caller is
    # looking for a name to paste into the config and has none.
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
