#!/usr/bin/env python3
"""Fetch FantasyPros consensus projections → players.csv (current season).

FantasyPros publishes a free, no-login projection table — ~266 players,
consensus per-game averages across their expert sources, updated regularly.
The sorter reads the resulting players.csv unchanged. ESPN's projections
API is not used: it bounces anonymous requests (needs an ESPN account's
cookies).

The FantasyPros table has no FGA/FTA columns, so FG%/FT% z-scores fall
back to unweighted (the sorter prints a warning). Rows are sorted by the
computed total z to build the projected seed order.

Usage:
    python3 fetch_data.py                 # live fetch -> players.csv
    python3 fetch_data.py --parse-file page.html
    python3 fetch_data.py --selftest
"""

import argparse
import csv
import html
import re
import sys
import urllib.request
from datetime import datetime, timezone

URL = "https://www.fantasypros.com/nba/projections/avg-overall.php"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Same canonical columns the sorter reads; rank is the seed-order primary key.
FIELDS = ["rank", "name", "team", "pos", "inj", "inj_note", "g", "mpg",
          "pts", "three", "reb", "ast", "stl", "blk", "fg_pct", "ft_pct",
          "to"]

# Stat cell order (verified 2026-09-25 against the page header):
# Player, PTS, REB, AST, BLK, STL, FG%, FT%, 3PM, GP, MIN, TO
# Cell 0 carries "Name   (TEAM - POS)   [OUT]?" — name cells count as 1.
NUMERIC_CELLS = {
    1: "pts", 2: "reb", 3: "ast", 4: "blk", 5: "stl",
    6: "fg_pct", 7: "ft_pct", 8: "three", 9: "g", 10: "mpg", 11: "to",
}


def fetch(url=URL, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def text_of(td_body):
    return html.unescape(re.sub(r"<[^>]+>", " ", td_body)).strip()


def parse(html_text):
    """Parse player rows. Returns (players, skipped)."""
    rows = re.findall(r'<tr[^>]*class="[^"]*mpb-player[^"]*"[^>]*>'
                      r".*?</tr>", html_text, re.S)
    players, skipped = [], 0
    for row in rows:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) != 12:                # 1 name cell + 11 stat cells
            skipped += 1
            continue
        m = re.match(r"^(.*?)\s*\(([A-Z]{3}) - ([^)]+)\)\s*(.*)$",
                     text_of(tds[0]))
        if m:
            name, team, pos, inj = (m.group(1), m.group(2), m.group(3),
                                    m.group(4))
        else:
            name, team, pos, inj = text_of(tds[0]), "", "", ""
        raw = [text_of(c).replace(",", "") for c in tds[1:]]

        def num(i):
            try:
                return float(raw[i])
            except (ValueError, IndexError):
                return 0.0

        p = {
            "name": name, "team": team, "pos": pos,
            "inj": inj, "inj_note": "",
            "pts": num(0), "reb": num(1), "ast": num(2), "blk": num(3),
            "stl": num(4), "fg_pct": num(5), "ft_pct": num(6),
            "three": num(7), "g": int(num(8)), "mpg": num(9), "to": num(10),
            # FantasyPros publishes no attempts; sorter falls back to
            # unweighted percentage z for these two categories.
            "fga": 0.0, "fta": 0.0,
        }
        players.append(p)
    return players, skipped


def assign_ranks(players):
    """Sort by computed total z and assign seed ranks 1..N."""
    from draft_sort import CATS, compute_z, total_z
    compute_z(players)
    players.sort(key=lambda p: -total_z(p, set()))
    for i, p in enumerate(players, 1):
        p["rank"] = i
    return players


def page_title(html_text):
    t = re.search(r"<title>([^<]*)</title>", html_text)
    return html.unescape(t.group(1)).strip() if t else "unknown page"


def write_csv(players, out_path, source_url, title):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        f.write(f"# source: {source_url}\n")
        f.write(f"# fetched: {stamp}\n")
        f.write(f"# page: {title}\n")
        f.write(f"# note: FantasyPros consensus per-game projections, "
                f"{len(players)} players;\n")
        f.write("#       no FGA/FTA published, so FG%/FT% z-scores are "
                "unweighted.\n")
        w = csv.writer(f)
        w.writerow(FIELDS)
        for p in players:
            w.writerow([p.get(k, "") for k in FIELDS])


def selftest():
    def td(body):
        return f"<td>{body}</td>"

    def make_row(name_cell, stats, n_cells=None):
        cells = [name_cell] + list(stats)
        if n_cells is not None:
            cells = cells[:n_cells]
        else:
            assert len(cells) == 12, len(cells)
        return ('<tr class="mpb-player"><td>'
                + "</td><td>".join(cells) + "</td></tr>")

    sga = ["31.1", "4.5", "6.3", "0.8", "1.6", ".528", ".887", "1.7",
           "77", "34.3", "2.3"]
    kyrie = ["24.0", "4.5", "4.8", "1.2", "0.4", ".478", ".915", "2.7",
             "70", "33.9", "2.8"]
    fixture = (
        "<html><head><title>NBA Projections - Per Game</title></head><body>"
        "<table>"
        + make_row("Shai Gilgeous-Alexander   (OKC - PG)", sga)
        + make_row("Kyrie Irving   (DAL - PG)   OUT", kyrie)
        + make_row("Short Row", sga[:5], n_cells=6)
        + "</table></body></html>"
    )
    players, skipped = parse(fixture)
    ok = True

    def check(cond, label):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        ok = ok and cond

    check(len(players) == 2 and skipped == 1,
          f"2 valid rows parsed, malformed skipped (got {len(players)})")
    s, k = players[0], players[1]
    check(s["name"] == "Shai Gilgeous-Alexander" and s["team"] == "OKC"
          and s["pos"] == "PG", "name/team/pos parsed from name cell")
    check(s["pts"] == 31.1 and s["blk"] == 0.8 and s["stl"] == 1.6
          and s["fg_pct"] == 0.528 and s["three"] == 1.7 and s["g"] == 77
          and s["to"] == 2.3, "stat cells mapped in header order")
    check(k["inj"] == "OUT" and k["name"] == "Kyrie Irving",
          "OUT tag captured as injury flag")
    ranked = assign_ranks([dict(p) for p in players])
    check(ranked[0]["name"] == "Shai Gilgeous-Alexander",
          "seed ranks assigned by computed total z")
    check(page_title(fixture) == "NBA Projections - Per Game",
          "page title detected")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=URL, help="page URL (default: "
                    "FantasyPros avg projections)")
    ap.add_argument("--out", default="players.csv")
    ap.add_argument("--parse-file", metavar="HTML",
                    help="parse a saved copy of the page instead of fetching")
    ap.add_argument("--selftest", action="store_true",
                    help="run the embedded parser test")
    args = ap.parse_args(argv)

    if args.selftest:
        print("fetch_data.py selftest:")
        sys.exit(0 if selftest() else 1)

    if args.parse_file:
        with open(args.parse_file, encoding="utf-8", errors="replace") as f:
            html_text = f.read()
        source = f"file:{args.parse_file}"
    else:
        print(f"Fetching {args.url} ...")
        html_text = fetch(args.url)
        source = args.url

    players, skipped = parse(html_text)
    if not players:
        print("ERROR: parsed 0 player rows. FantasyPros markup likely "
              "changed; the parser expected <tr class='mpb-player'> rows "
              "with 12 cells.", file=sys.stderr)
        print("Options: save the page and try --parse-file, or supply your "
              "own players.csv.", file=sys.stderr)
        return 2
    if skipped:
        print(f"Warning: {skipped} player-looking rows skipped "
              "(wrong cell count).")
    if len(players) < 200:
        print(f"Warning: only {len(players)} players parsed "
              "(expected >= 200).")

    assign_ranks(players)
    write_csv(players, args.out, source, page_title(html_text))
    print(f"Page: {page_title(html_text)}")
    print(f"Wrote {len(players)} players to {args.out} "
          "(seed order = computed total z)")
    for p in players[:3]:
        inj = f" [{p['inj']}]" if p["inj"] else ""
        print(f"  # {p['rank']:>3}  {p['name']:<24} {p['team']} {p['pos']}"
              f"{inj}  g={p['g']} pts={p['pts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
