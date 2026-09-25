#!/usr/bin/env python3
"""Interactive pairwise-comparison ranker for NBA 9-cat fantasy drafts.

Shows two players side-by-side with their 9-category stats (better value
highlighted, TO lower-is-better), asks which one you'd draft first, and
repeats until the whole list is sorted into your personal ranking.

The questioning is an adaptive insertion sort seeded by the projected order
in players.csv: when you agree with the seed, each player costs 1 question;
only upsets trigger a binary search. Progress is saved after every answer,
so you can quit and resume anytime. Undo ('u') takes back the last player
placement.

Usage:
    python3 fetch_data.py             # refresh players.csv (optional)
    python3 draft_sort.py             # rank the top 200
    python3 draft_sort.py --n 12      # quick trial run
    python3 draft_sort.py --selftest  # hermetic algorithm tests
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
import tempfile
import termios
import time
import tty
import unicodedata
from datetime import datetime, timezone
from types import SimpleNamespace

# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------

CATS = ["fg_pct", "ft_pct", "three", "pts", "reb", "ast", "stl", "blk", "to"]
PCT_CATS = ("fg_pct", "ft_pct")
CAT_LABEL = {"pts": "PTS", "three": "3PM", "reb": "REB", "ast": "AST",
             "stl": "STL", "blk": "BLK", "fg_pct": "FG%", "ft_pct": "FT%",
             "to": "TO"}

# Names accepted by --punt and by the CSV header normalizer.
CAT_ALIASES = {
    "points": "pts", "pts": "pts", "ppg": "pts",
    "threes": "three", "three": "three", "3pm": "three", "3p": "three",
    "3ptm": "three", "3pt": "three", "3": "three", "3s": "three",
    "rebounds": "reb", "reb": "reb", "rpg": "reb", "trb": "reb",
    "assists": "ast", "ast": "ast", "apg": "ast",
    "steals": "stl", "stl": "stl", "spg": "stl",
    "blocks": "blk", "blk": "blk", "bpg": "blk",
    "fg": "fg_pct", "fgpct": "fg_pct", "fg%": "fg_pct",
    "ft": "ft_pct", "ftpct": "ft_pct", "ft%": "ft_pct",
    "to": "to", "tov": "to", "turnovers": "to", "tpg": "to",
}

HEADER_ALIASES = {
    "player": "name", "playername": "name", "player_name": "name",
    "gp": "g", "games": "g", "gamesplayed": "g", "games_played": "g",
    "min": "mpg", "minutes": "mpg", "mpg": "mpg", "mins": "mpg",
    "position": "pos", "positions": "pos",
    "usage": "usg", "usagepct": "usg", "usage_pct": "usg",
    "value": "bm_value", "bmvalue": "bm_value",
    "id": "bm_id", "playerid": "bm_id",
    **CAT_ALIASES,
}

REQUIRED_COLS = ["name"] + CATS  # fga/fta optional: pct-z falls back to plain

LEFT, TIE, RIGHT = -1, 0, 1
UNDO, SKIP, REDO, SHOW_RANK, HELP = "undo", "skip", "redo", "show_rank", \
    "help"


class Quit(Exception):
    """Raised when the user saves and exits."""


class CsvError(Exception):
    """players.csv is unusable."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# ANSI helpers
# --------------------------------------------------------------------------

GREEN = "\033[1;32m"
RED = "\033[1;31m"
YELLOW = "\033[1;33m"
DIM = "\033[2m"
RESET = "\033[0m"


def safe_text(s):
    """Degrade unicode to ascii if the terminal can't print it."""
    try:
        s.encode(sys.stdout.encoding or "utf-8")
        return s
    except (UnicodeEncodeError, AttributeError):
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore") \
            .decode("ascii")


# --------------------------------------------------------------------------
# CSV loading
# --------------------------------------------------------------------------

def normalize_header(h):
    return re.sub(r"[^a-z0-9]", "", h.lower())


def fnum(s, row_label, field, warns):
    s = (s or "").strip()
    if not s:
        warns.append(f"{row_label}: blank {field} -> 0.0")
        return 0.0
    try:
        return float(s)
    except ValueError:
        warns.append(f"{row_label}: bad {field} value {s!r} -> 0.0")
        return 0.0


def load_players(fileobj, source, min_g=0, quiet=False):
    """Parse players.csv. Returns (players, warnings). Raises CsvError."""
    warns = []
    rows = list(csv.reader(fileobj))
    data = [r for r in rows
            if r and r[0].strip() and not r[0].lstrip().startswith("#")]
    if not data:
        raise CsvError(f"{source}: no data rows (only comments/blank lines?)")
    header = [normalize_header(h) for h in data[0]]
    idx = {}
    for i, h in enumerate(header):
        idx.setdefault(HEADER_ALIASES.get(h, h), i)
    missing = [c for c in REQUIRED_COLS if c not in idx]
    if missing:
        raise CsvError(f"{source}: missing required column(s): "
                       f"{', '.join(missing)}\n  header found: "
                       f"{', '.join(header)}")
    weighted = "fga" in idx and "fta" in idx
    if not weighted:
        warns.append("no FGA/FTA columns: FG%/FT% z-scores will be "
                     "unweighted (edit the CSV to add fga,fta)")

    players = []
    for r in data[1:]:
        if not any(c.strip() for c in r):
            continue

        def get(canon, default=""):
            i = idx.get(canon)
            if i is None or i >= len(r):
                return default
            return r[i].strip()

        name = get("name")
        if not name:
            warns.append("row skipped: empty player name")
            continue
        label = f"row '{name}'"
        p = {
            "name": name,
            "_rank_raw": get("rank", ""),
            "team": get("team", "?"),
            "pos": get("pos", "?"),
            "inj": get("inj", ""),
            "inj_note": get("inj_note", ""),
            "g": fnum(get("g", "82"), label, "g", warns),
            "mpg": fnum(get("mpg"), label, "mpg", warns),
            "usg": fnum(get("usg"), label, "usg", warns),
            "bm_value": fnum(get("bm_value"), label, "bm_value", warns),
            "bm_id": get("bm_id", ""),
        }
        for cat in CATS:
            p[cat] = fnum(get(cat), label, cat, warns)
        if weighted:
            p["fga"] = fnum(get("fga"), label, "fga", warns)
            p["fta"] = fnum(get("fta"), label, "fta", warns)
        else:
            p["fga"] = p["fta"] = 0.0
        players.append(p)

    # percent -> fraction if the file uses 0-100 style
    for cat in PCT_CATS:
        mx = max(p[cat] for p in players)
        if mx > 1.0:
            for p in players:
                p[cat] /= 100.0
            warns.append(f"{cat}: values >1 found, divided by 100")

    # ranks: use the file's rank column if unique, else assign by row order
    ranks = []
    for i, p in enumerate(players, 1):
        raw = p.pop("_rank_raw", "")
        try:
            ranks.append(int(float(raw)))
        except (TypeError, ValueError):
            ranks.append(None)
    if len(set(r for r in ranks if r is not None)) != len(ranks) \
            or any(r is None for r in ranks):
        if "rank" in idx:
            warns.append("rank column missing/duplicated: assigned by row "
                         "order")
        ranks = list(range(1, len(players) + 1))
    for p, rank in zip(players, ranks):
        p["rank"] = rank
        p["id"] = rank
    players.sort(key=lambda p: p["id"])

    # disambiguate duplicate names with (TEAM)
    counts = {}
    for p in players:
        counts[p["name"]] = counts.get(p["name"], 0) + 1
    for p in players:
        p["display"] = f"{p['name']} ({p['team']})" \
            if counts[p["name"]] > 1 else p["name"]

    if min_g > 0:
        kept = [p for p in players if p["g"] >= min_g]
        dropped = len(players) - len(kept)
        if dropped:
            warns.append(f"--min-g {min_g}: dropped {dropped} players with "
                         f"fewer games")
        players = kept

    if not quiet:
        print(f"Loaded {len(players)} players from {source}"
              f" (min games: {min_g})")
        print("  sanity check (min / mean / max):")
        for cat in CATS:
            vals = [p[cat] for p in players]
            print(f"    {CAT_LABEL[cat]:>4}  {min(vals):7.3f}  "
                  f"{statistics.fmean(vals):7.3f}  {max(vals):7.3f}")
        if any(p["g"] < 15 for p in players):
            n_low = sum(1 for p in players if p["g"] < 15)
            print(f"  note: {n_low} players have <15 games — consider "
                  f"--min-g 15 to tame small-sample outliers")
    return players, warns


# --------------------------------------------------------------------------
# Z-scores
# --------------------------------------------------------------------------

def _plain_z(vals):
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    if sd == 0:
        return [0.0] * len(vals)
    return [(v - mean) / sd for v in vals]


def compute_z(players):
    """Set p['z'][cat] for every player, computed over the given pool."""
    for p in players:
        p["z"] = {}
    for cat in CATS:
        vals = [p[cat] for p in players]
        if cat == "to":
            mean, sd = statistics.fmean(vals), statistics.pstdev(vals)
            zs = [0.0] * len(players) if sd == 0 \
                else [(mean - v) / sd for v in vals]      # lower is better
        elif cat in PCT_CATS:
            att = "fga" if cat == "fg_pct" else "fta"
            atts = [p[att] for p in players]
            if sum(atts) > 0:
                # volume-weighted impact: FGM_i - FGA_i * pool_pct
                L = sum(p[cat] * a for p, a in zip(players, atts)) / sum(atts)
                impacts = [p[cat] * a - a * L
                           for p, a in zip(players, atts)]
                zs = _plain_z(impacts)
            else:
                zs = _plain_z(vals)
        else:
            zs = _plain_z(vals)
        for p, z in zip(players, zs):
            p["z"][cat] = z


def total_z(p, punt):
    return sum(p["z"][c] for c in CATS if c not in punt)


def stat_fmt(cat, p):
    return f"{p[cat]:.3f}" if cat in PCT_CATS else f"{p[cat]:.1f}"


# --------------------------------------------------------------------------
# Punt parsing
# --------------------------------------------------------------------------

def parse_punt(spec):
    if not spec.strip():
        return set()
    out = set()
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        cat = CAT_ALIASES.get(normalize_header(tok))
        if cat not in CATS:
            valid = ", ".join(f"{c} ({CAT_LABEL[c]})" for c in CATS)
            raise CsvError(f"unknown category {tok!r} — valid: {valid}")
        out.add(cat)
    if len(out) == len(CATS):
        raise CsvError("all 9 categories punted — nothing left to rank by")
    return out


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def new_state(seed, n, punt, csv_path, csv_md5, csv_rows, pool_size):
    return {
        "version": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "csv_path": csv_path,
        "csv_md5": csv_md5,
        "csv_rows": csv_rows,
        "pool_size": pool_size,
        "n": n,
        "punt": [c for c in CATS if c in punt],
        "seed": list(seed),
        "queue": list(seed),
        "ranked": [],
        "placements": [],
        "comparisons": 0,
        "ties": 0,
        "skips": 0,
        "undos": 0,
        "redos": 0,
        "redo_stack": [],
        "done": False,
    }


def save_state(state, path):
    state["updated_at"] = now_iso()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def validate_state(state, valid_ids):
    """Return a list of problems (empty = OK)."""
    errs = []
    lists = {"queue": state.get("queue"), "ranked": state.get("ranked"),
             "placements": state.get("placements")}
    for name, lst in lists.items():
        if not isinstance(lst, list):
            errs.append(f"{name} is not a list")
            continue
        for x in lst:
            if not isinstance(x, int) or x not in valid_ids:
                errs.append(f"{name} contains unknown player id {x!r}")
    q, r, pl = state.get("queue", []), state.get("ranked", []), \
        state.get("placements", [])
    if set(q) & set(r):
        errs.append("a player is both ranked and queued")
    if set(q) | set(r) != set(state.get("seed", [])):
        errs.append("ranked + queue does not match the seed list")
    if len(r) != len(pl):
        errs.append("ranked/placements length mismatch")
    if state.get("n") != len(state.get("seed", [])):
        errs.append("n != len(seed)")
    return errs


# --------------------------------------------------------------------------
# Core: adaptive insertion sort seeded by the projected order
# --------------------------------------------------------------------------

def commit(state, pos, save):
    """Insert queue[0] into ranked at pos, then persist."""
    p = state["queue"].pop(0)
    state["ranked"].insert(pos, p)
    state["placements"].append(p)
    state["redo_stack"] = []          # a new placement ends redo history
    save(state)


def do_undo(state, save):
    """Pop the last placement; the player goes back to the front of the
    queue and is re-asked. No-op (and no counter bump) with no placements."""
    if not state["placements"]:
        return False
    p = state["placements"].pop()
    pos = state["ranked"].index(p)
    state["ranked"].remove(p)
    state["queue"].insert(0, p)
    state["redo_stack"].append((p, pos))
    state["undos"] += 1
    save(state)
    return True


def do_redo(state, save):
    """Restore the most recently undone placement. No-op with an empty
    redo stack."""
    if not state.get("redo_stack"):
        return False
    p, pos = state["redo_stack"].pop()
    if p in state["ranked"]:
        return False
    if p in state["queue"]:
        state["queue"].remove(p)
    state["ranked"].insert(pos, p)
    state["placements"].append(p)
    state["redos"] += 1
    save(state)
    return True


def do_skip(state, save):
    if len(state["queue"]) > 1:
        state["queue"].append(state["queue"].pop(0))
        state["skips"] += 1
        save(state)
        return True
    return False


def place_one(state, ask, save):
    """Place queue[0]. ask(left, right, phase, probe, slot) -> verdict or
    UNDO/SKIP/SHOW_RANK/HELP, or raises Quit. Returns "committed" or a meta
    action for the caller. The challenger stays at queue[0] until committed,
    so quitting mid-search just restarts that placement next session."""
    p = state["queue"][0]
    ranked = state["ranked"]
    if not ranked:
        commit(state, 0, save)
        return "committed"

    def ask_row(right_id, phase, probe, slot):
        while True:
            ans = ask(p, right_id, phase, probe, slot)
            if ans not in (SHOW_RANK, HELP):
                return ans

    # quick check vs the current worst: usually the only question needed
    ans = ask_row(ranked[-1], "quick", (1, 1), len(ranked))
    if ans in (UNDO, SKIP, REDO):
        return ans
    state["comparisons"] += 1
    if ans == TIE:
        state["ties"] += 1
    if ans != LEFT:                       # incumbent better or tie -> append
        commit(state, len(ranked), save)
        return "committed"

    # challenger beat the worst: binary search the insertion position
    lo, hi = 0, len(ranked) - 1
    n_probes = max(1, math.ceil(math.log2(len(ranked))))
    k = 1
    while lo < hi:
        mid = (lo + hi) // 2
        ans = ask_row(ranked[mid], "binary", (k, n_probes), mid + 1)
        if ans in (UNDO, SKIP, REDO):
            return ans
        state["comparisons"] += 1
        if ans == TIE:
            state["ties"] += 1
        k += 1
        if ans == LEFT:                   # challenger better -> go higher
            hi = mid
        else:                             # incumbent better or tie -> lower
            lo = mid + 1
    commit(state, lo, save)
    return "committed"


def run(state, ask, save):
    """Rank until the queue is empty or Quit is raised."""
    while state["queue"]:
        res = place_one(state, ask, save)
        if res == UNDO:
            do_undo(state, save)
        elif res == SKIP:
            do_skip(state, save)
        elif res == REDO:
            do_redo(state, save)
    state["done"] = True
    save(state)


# --------------------------------------------------------------------------
# Terminal UI
# --------------------------------------------------------------------------

KEYMAP = {
    "1": "left", "l": "left", "left": "left",
    "2": "right", "right": "right",
    "t": "tie", "tie": "tie",
    "u": "undo", "undo": "undo",
    "y": "redo", "redo": "redo",
    "e": "export", "export": "export",
    "s": "skip", "skip": "skip",
    "r": "rankings", "rankings": "rankings",
    "?": "help", "h": "help", "help": "help",
    "q": "quit", "quit": "quit",
}


def read_key(tty_in):
    """One keypress in raw mode, or a line otherwise. Raises Quit on EOF."""
    if tty_in:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            b = sys.stdin.buffer.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if not b:
            raise Quit()
        return b.decode("utf-8", "replace").lower()
    line = sys.stdin.readline()
    if line == "":
        raise Quit()
    return line.strip().lower()


def prompt_choice(prompt, choices, default):
    """Yes/no-ish prompt usable before the UI exists. EOF -> default."""
    sys.stdout.write(prompt + " ")
    sys.stdout.flush()
    try:
        key = read_key(sys.stdin.isatty())
    except Quit:
        key = default
    return key if key in choices else default


class UI:
    """Renders comparisons and collects verdicts."""

    def __init__(self, by_id, state, punt, color, no_clear, export_path):
        self.by_id = by_id
        self.state = state
        self.punt = set(punt)
        self.export_path = export_path
        self.color = color
        self.tty_in = sys.stdin.isatty()
        self.tty_out = sys.stdout.isatty()
        self.clear = self.tty_out and color and not no_clear
        width = shutil.get_terminal_size((80, 40)).columns
        self.width = max(40, min(width, 140))
        self.start = time.monotonic()

    # ----- low-level rendering helpers -----

    def cell(self, text, width, code=None, winner=False):
        pad = f"{text:>{width}}"
        if winner and not self.color and pad:
            pad = "*" + pad[1:]          # mark a pad space, never a digit
        if code and self.color:
            return code + pad + RESET
        return pad

    def vfmt(self, cat, p):
        if cat in PCT_CATS:
            return f"{p[cat]:.3f}"
        return f"{p[cat]:.1f}"

    def zcell(self, p, cat):
        if cat in self.punt:
            return f"{'—':>7}"
        return f"{p['z'][cat]:+7.2f}"

    def cat_label(self, cat):
        return CAT_LABEL[cat]

    def player_block(self, p):
        name = safe_text(p["display"])
        name = name[:15] + "…" if len(name) > 16 else name
        return (f"#{p['id']} {name:<16} {safe_text(p['team'])} "
                f"{safe_text(p['pos'])}·{p['g']:.0f} GP")

    def inj_block(self, p, width):
        if not p["inj"] and not p["inj_note"]:
            return None
        txt = f"[{p['inj']}] {safe_text(p['inj_note'])[:26]}".strip()
        code = RED if p["inj"] in ("INJ", "X") else YELLOW
        pad = f"{txt:<{width}}"
        return (code + pad + RESET) if code and self.color else pad

    def elapsed(self):
        secs = int(time.monotonic() - self.start)
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m{secs % 60:02d}s"

    def verdict(self, d):
        if d > 0.05:
            return "RIGHT is better"
        if d < -0.05:
            return "LEFT is better"
        return "essentially tied"

    # ----- screen views -----

    def render(self, left, right, phase, probe, slot):
        L, R = self.by_id[left], self.by_id[right]
        state = self.state
        placed, n = len(state["ranked"]), state["n"]
        pct = 100 * placed // n if n else 0
        q = state["comparisons"] + 1
        phase_txt = ("[quick check vs current worst]" if phase == "quick"
                     else f"[binary probe {probe[0]} of ~{probe[1]}]")
        lines = self.render_body(L, R, q, placed, n, pct, phase_txt, slot)
        out = ("\033[2J\033[H" if self.clear else "") + "\n".join(lines) + "\n"
        sys.stdout.write(out)
        sys.stdout.flush()

    def render_body(self, L, R, q, placed, n, pct, phase_txt, slot):
        state = self.state
        elapsed = self.elapsed()
        counts = (f"comparisons {state['comparisons']} · ties "
                  f"{state['ties']} · skips {state['skips']} · undos "
                  f"{state['undos']} · redos {state.get('redos', 0)}")
        punt_txt = "punt: " + (", ".join(self.punt) or "none")
        lines = [
            f"  Q {q} · {placed}/{n} placed ({pct}%) · {elapsed}   {phase_txt}",
            f"  {punt_txt}   ·   {counts}",
            "═" * self.width,
        ]
        lines += self.players_header(L, R, slot)
        lines.append("─" * self.width)
        lines += self.stats_body(L, R)
        lines.append("─" * self.width)
        lines += self.total_body(L, R)
        lines.append("─" * self.width)
        lines += self.keys_body()
        return lines

    def players_header(self, L, R, slot):
        placed = len(self.state["ranked"])
        if self.width >= 78:
            lh = "   [1] LEFT · challenger"
            rh = f"[2] RIGHT · #{slot} of {placed} placed"
            line = lh + " " * max(2, self.width - len(lh) - len(rh) - 1) + rh
            out = [line]
            half = (self.width - 6) // 2
            lblock = f"   {self.player_block(L):<{half}}"
            rblock = f"{self.player_block(R):>{half + 2}}"
            out.append(lblock + rblock)
            li = self.inj_block(L, half)
            ri = self.inj_block(R, half)
            if li or ri:
                out.append(f"   {li or '':<{half}}"
                           f"{(ri or ''):>{half + 2}}")
            return out
        # stacked
        out = ["   [1] LEFT · challenger", f"   {self.player_block(L)}"]
        li = self.inj_block(L, self.width - 4)
        if li:
            out.append("   " + li)
        out += [f"   [2] RIGHT · #{slot} of {placed} placed",
                f"   {self.player_block(R)}"]
        ri = self.inj_block(R, self.width - 4)
        if ri:
            out.append("   " + ri)
        return out

    def stats_body(self, L, R):
        if self.width >= 78:
            prefix = " " * max(0, (self.width - 52) // 2)
            out = [prefix + f"{'z':>7} {'LEFT':>10}  │ {'CATEGORY':^8} │  "
                            f"{'RIGHT':>10} {'z':>7}"]
            for cat in CATS:
                out.append(prefix + self.wide_row(cat, L, R))
            return out
        out = [f"  {'CATEGORY':<8} {'z':>7} {'value':>8}"]
        for cat in CATS:
            out.append("  " + self.stack_row(cat, L))
        out.append(f"  {'TOTAL Z':<8} {total_z(L, self.punt):+8.2f}")
        out.append("")
        out.append(f"  {'CATEGORY':<8} {'z':>7} {'value':>8}")
        for cat in CATS:
            out.append("  " + self.stack_row(cat, R))
        out.append(f"  {'TOTAL Z':<8} {total_z(R, self.punt):+8.2f}")
        return out

    def row_parts(self, cat, L, R):
        """(lval, lz, label, rz, rval, code_l, code_r, winner_l, winner_r)"""
        punted = cat in self.punt
        lv, rv = self.vfmt(cat, L), self.vfmt(cat, R)
        if punted:
            return (lv, "—", self.cat_label(cat), "—", rv,
                    DIM, DIM, False, False)
        if cat == "to":
            wl = L[cat] < R[cat]
            wr = R[cat] < L[cat]
        else:
            wl = L[cat] > R[cat]
            wr = R[cat] > L[cat]
        return (lv, self.zcell(L, cat), self.cat_label(cat),
                self.zcell(R, cat), rv,
                GREEN if wl else None, GREEN if wr else None, wl, wr)

    def wide_row(self, cat, L, R):
        # z on the outside, averages in the middle next to the category
        lv, lz, label, rz, rv, cl, cr, wl, wr = self.row_parts(cat, L, R)
        return (f"{self.cell(lz, 7, cl, wl)} {self.cell(lv, 10, cl, wl)}  │ "
                f"{label:^8} │  {self.cell(rv, 10, cr, wr)} "
                f"{self.cell(rz, 7, cr, wr)}")

    def stack_row(self, cat, p):
        # same player on both sides -> no winner highlight, just values
        lv, lz, label, rz, rv, cl, cr, wl, wr = self.row_parts(cat, p, p)
        return (f"{label:<8} {self.cell(lz, 7, cl, wl)} "
                f"{self.cell(lv, 8, cl, wl)}")

    def total_body(self, L, R):
        lt = total_z(L, self.punt)
        rt = total_z(R, self.punt)
        d = rt - lt
        ver = self.verdict(d)
        if self.width >= 78:
            # mirror the stats rows: bars at columns 20 and 31 of a 52-wide
            # table (7 z + 10 val + 2 + bar + 8 cat + bar + 2 + 10 val + 7 z)
            prefix = " " * max(0, (self.width - 52) // 2)
            left = f"TOTAL Z {lt:+6.2f}".ljust(20)
            right = f"{rt:+6.2f} TOTAL Z".rjust(20)
            mid = f"Δ {d:+.2f}"
            return [
                prefix + f"{left}│ {mid:^8} │{right}",
                prefix + f"{ver:^52}",
            ]
        return [f"  Δ {d:+6.2f} → {ver}"]

    def keys_body(self):
        redo = bool(self.state.get("redo_stack"))
        if self.width >= 78:
            slots = ["[1] left", "[2] right", "[t] tie", "[u] undo",
                     "[s] skip", "[r] rankings", "[?] help", "[q] save & quit"]
            w = max(len(s) for s in slots) + 1      # even column grid
            line1 = "   " + "".join(s.ljust(w) for s in slots[:4]).rstrip()
            if redo:
                line1 += "  [y] redo"
            line2 = "   " + "".join(s.ljust(w) for s in slots[4:]).rstrip()
            line2 += "  [e] export"
            note = "   t: tie — RIGHT (already ranked) stays above"
            if not self.color:
                note += "   * = better · TO: lower is better"
            return [line1, line2, "", note]
        # stacked: compact three-per-line layout
        out = ["   [1] left   [2] right   [t] tie",
               "   [u] undo   [s] skip   [r] rankings"]
        if redo:
            out[1] += "   [y] redo"
        out.append("   [?] help   [q] save & quit   [e] export")
        return out

    # ----- interaction -----

    def ask(self, left, right, phase, probe, slot):
        while True:
            self.render(left, right, phase, probe, slot)
            act = KEYMAP.get(read_key(self.tty_in))
            if act is None:
                continue
            if act == "quit":
                raise Quit()
            if act == "rankings":
                self.show_rankings()
                return SHOW_RANK
            if act == "help":
                self.show_help()
                return HELP
            if act == "export":
                self.export_current()
                return SHOW_RANK
            if act == "undo":
                return UNDO
            if act == "redo":
                return REDO
            if act == "skip":
                return SKIP
            if act == "left":
                return LEFT
            if act == "right":
                return RIGHT
            return TIE

    def export_current(self):
        """Write the current ranking to the export file (key 'e')."""
        write_export(self.state, self.by_id, self.punt, self.export_path)
        if self.tty_in:
            print(f"  Exported {len(self.state['ranked'])} players to "
                  f"{self.export_path} — any key to return")
            if KEYMAP.get(read_key(self.tty_in)) == "quit":
                raise Quit()
        else:
            print(f"  Exported {len(self.state['ranked'])} players to "
                  f"{self.export_path}")

    def show_rankings(self):
        ranked = self.state["ranked"]
        queue = self.state["queue"]
        cols = 3 if self.width >= 96 else (2 if self.width >= 66 else 1)
        entries = []
        for i, pid in enumerate(ranked, 1):
            p = self.by_id[pid]
            entries.append(f"{i:>3}. {safe_text(p['display'])[:14]:<14} "
                           f"{safe_text(p['team']):>3} "
                           f"{total_z(p, self.punt):+5.1f}")
        rows = math.ceil(len(entries) / cols)
        print()
        print("  YOUR RANKING SO FAR (best first)")
        print("  " + "─" * min(self.width - 4, 90))
        for r in range(rows):
            line = "   ".join(entries[c * rows + r]
                              for c in range(cols)
                              if c * rows + r < len(entries))
            print("  " + line)
        if self.tty_in:
            print(f"  ({len(ranked)} placed · {len(queue)} to go · any key "
                  f"to return)")
            if KEYMAP.get(read_key(self.tty_in)) == "quit":
                raise Quit()
        else:
            print(f"  ({len(ranked)} placed · {len(queue)} to go)")

    def show_help(self):
        print()
        print("  KEYS")
        print("    1 / l   left player is better")
        print("    2       right player is better")
        print("    t       about equal — the right (already-ranked) player")
        print("            stays above, which preserves the seed order")
        print("    u       undo the last player placement (they get re-asked)")
        print("    y       redo the last undo (shown only when available)")
        print("    s       skip the left player to the end of the queue")
        print("    e       export the current ranking to export.txt")
        print("            (name, position, z, then the 9 stats)")
        print("    r       show your ranking so far")
        print("    q       save progress and exit (resume any time)")
        print()
        print("  The LEFT side is always the challenger being placed; the")
        print("  RIGHT side is a player already in your ranking. Green/bold")
        print("  (or '*') marks the better raw value; TO is lower-is-better.")
        print("  z columns put each category on the same scale (0 = pool")
        print("  average) and feed TOTAL Z; punted categories show '—'.")
        print()
        print("  The header shows the phase: 'quick check vs current worst'")
        print("  = challenger vs the worst player ranked so far (one quick")
        print("  question); 'binary probe' = challenger beat the worst, so a")
        print("  binary search is finding their exact spot in your ranking.")
        if self.tty_in:
            print("  any key to return")
            if KEYMAP.get(read_key(self.tty_in)) == "quit":
                raise Quit()


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_export(state, by_id, punt, path):
    """Snapshot the current ranking: name, position, total z, then the
    9-cat stats. Usable mid-run (press 'e')."""
    ranked = state["ranked"]
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# NBA 9-cat draft ranking — export snapshot "
                f"({len(ranked)} of {state['n']} placed)\n")
        f.write(f"# exported: {now_iso()} · {state['comparisons']} comparisons"
                f" · {state['ties']} ties · punt: "
                f"{', '.join(state['punt']) or 'none'}\n")
        f.write(f"{'':6}{'NAME':<26} {'POS':<4} {'Z':>7} "
                + " ".join(f"{CAT_LABEL[c]:>6}" for c in CATS) + "\n")
        for i, pid in enumerate(ranked, 1):
            p = by_id[pid]
            z = total_z(p, set(state["punt"]))
            stats = " ".join(f"{stat_fmt(c, p):>6}" for c in CATS)
            f.write(f"{i:>4}. {p['display']:<26} {p['pos']:<4} "
                    f"{z:>+7.2f} {stats}\n")


def write_draft_list(state, by_id, punt, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# NBA 9-cat draft ranking — {state['n']} players\n")
        f.write(f"# generated: {now_iso()} · {state['comparisons']} "
                f"comparisons · {state['ties']} ties · {state['undos']} "
                f"undos\n")
        f.write(f"# punt: {', '.join(state['punt']) or 'none'}\n")
        f.write(f"# seed: {state['csv_path']} ({state['csv_rows']} rows)\n")
        for i, pid in enumerate(state["ranked"], 1):
            p = by_id[pid]
            z = total_z(p, set(state["punt"]))
            seed_no = state["seed"].index(pid) + 1
            f.write(f"{i:>4}. {p['display']:<26} {p['team']:>4} "
                    f"{p['pos']:>4}  z {z:+6.2f}  (seed #{seed_no})\n")
        moves = [(state["seed"].index(pid) + 1 - i, by_id[pid]["display"])
                 for i, pid in enumerate(state["ranked"], 1)]
        risers = [m for m in sorted(moves, reverse=True)[:5] if m[0] > 0]
        fallers = [m for m in sorted(moves)[:5] if m[0] < 0]
        f.write("#\n# vs seed — biggest risers:\n")
        if risers:
            for d, name in risers:
                f.write(f"#   {d:+3d}  {name}\n")
        else:
            f.write("#   (none)\n")
        f.write("# vs seed — biggest fallers:\n")
        if fallers:
            for d, name in fallers:
                f.write(f"#   {d:+3d}  {name}\n")
        else:
            f.write("#   (none)\n")


# --------------------------------------------------------------------------
# Selftest
# --------------------------------------------------------------------------

def make_synthetic_pool(n=60, seed=42):
    rng = random.Random(seed)
    players = []
    for i in range(1, n + 1):
        p = {
            "id": i, "rank": i, "name": f"Player{i:03d}", "display": f"Player{i:03d}",
            "team": f"T{i % 30:02d}", "pos": ("G", "F", "C")[i % 3],
            "inj": "", "inj_note": "", "g": rng.randint(30, 82),
            "mpg": round(rng.uniform(12, 36), 1),
            "pts": round(rng.uniform(4, 32), 1),
            "three": round(rng.uniform(0, 4), 1),
            "reb": round(rng.uniform(1, 13), 1),
            "ast": round(rng.uniform(0.5, 10), 1),
            "stl": round(rng.uniform(0.2, 2), 1),
            "blk": round(rng.uniform(0, 3), 1),
            "fg_pct": round(rng.uniform(0.40, 0.62), 3),
            "fga": round(rng.uniform(4, 22), 1),
            "ft_pct": round(rng.uniform(0.60, 0.93), 3),
            "fta": round(rng.uniform(1, 9), 1),
            "to": round(rng.uniform(0.5, 4), 1),
            "usg": round(rng.uniform(10, 34), 1),
            "bm_value": 0.0, "bm_id": str(i),
        }
        players.append(p)
    return players


class Auto:
    """Selftest stand-in for the human: answers by total z."""

    def __init__(self, by_id, punt, tol=0.0, state=None):
        self.by_id, self.punt, self.tol = by_id, punt, tol
        self.count = 0
        self.actions = []          # (at_comparison, action) fire before answer
        self.quit_at = None        # raise Quit before answering ask #quit_at+1
        self.state = state
        self.snapshots = []

    def on(self, at, action):
        self.actions.append((at, action))

    def ask(self, left, right, phase=None, probe=None, slot=None):
        self.count += 1
        if self.state is not None:
            self.snapshots.append((self.state["queue"][0],
                                   len(self.state["ranked"]),
                                   len(self.state["placements"])))
        for at, act in self.actions:
            if at == self.count:
                if act == "quit":
                    raise Quit()
                return act
        if self.quit_at is not None and self.count > self.quit_at:
            raise Quit()
        d = total_z(self.by_id[left], self.punt) \
            - total_z(self.by_id[right], self.punt)
        if abs(d) < self.tol:
            return TIE
        return LEFT if d > 0 else RIGHT


def selftest():
    ok = True

    def check(cond, label):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        ok = ok and cond

    def scenario(seed, tol=0.0, actions=None, quit_at=None):
        state = new_state(list(seed), len(seed), [], "<selftest>", "md5",
                          len(pool), len(pool))
        a = Auto(by_id, PUNT0, tol=tol, state=state)
        for at, act in (actions or []):
            a.on(at, act)
        a.quit_at = quit_at
        try:
            run(state, a.ask, lambda st: None)
        except Quit:
            pass
        return state, a

    pool = make_synthetic_pool(60)
    compute_z(pool)
    by_id = {p["id"]: p for p in pool}
    PUNT0 = set()
    base = [p["id"] for p in pool]
    expected = sorted(base,
                      key=lambda i: (-total_z(by_id[i], PUNT0),
                                     base.index(i)))
    n = len(base)
    ceil_logn = math.ceil(math.log2(n))
    q_bounds = (n - 1, 2 * n + n * ceil_logn)

    near = list(expected)
    for i in (2, 7, 15, 26, 40):
        near[i], near[i + 1] = near[i + 1], near[i]
    reversed_seed = list(reversed(expected))
    shuffled_seed = random.Random(7).sample(base, len(base))

    print("draft_sort.py selftest (60 synthetic players):")
    print("core:")
    for label, seed in (("near-seed", near), ("reversed seed", reversed_seed),
                        ("shuffled seed", shuffled_seed)):
        state, a = scenario(seed)
        check(state["ranked"] == expected,
              f"exact oracle, {label}: final order == z order")
        check(q_bounds[0] <= state["comparisons"] <= q_bounds[1],
              f"exact oracle, {label}: questions in {q_bounds} "
              f"(got {state['comparisons']})")

    print("tolerant:")
    state, a = scenario(near, tol=0.1)
    ranked = state["ranked"]
    check(sorted(ranked) == sorted(base), "tolerant: permutation of pool")
    inv = [total_z(by_id[ranked[i + 1]], PUNT0)
           - total_z(by_id[ranked[i]], PUNT0)
           for i in range(len(ranked) - 1)]
    check(max(inv) <= 0.2 + 1e-9,
          f"tolerant: adjacent inversions bounded "
          f"(max {max(inv):.4f} <= 0.2)")

    print("meta actions:")
    state, a = scenario(near, actions=[(5, UNDO), (25, UNDO)])
    check(state["undos"] == 2 and state["ranked"] == expected,
          "undo twice mid-run, continue: final order == z order")
    state, a = scenario(near, actions=[(3, SKIP)])
    check(state["skips"] == 1 and state["ranked"] == expected
          and len(state["ranked"]) == n, "skip: player not lost, order ok")
    state, a = scenario(near, actions=[(2, TIE)])
    check(state["ranked"] == expected, "forced tie: seed order preserved")

    print("undo/redo:")
    state, a = scenario(near, actions=[(5, UNDO), (6, REDO)])
    check(state["redos"] == 1 and state["ranked"] == expected,
          "undo then redo: placement restored, order == z order")
    check(not state["redo_stack"], "redo drains the redo stack")
    state, a = scenario(near, actions=[(5, UNDO)])
    check(state["undos"] == 1 and not state["redo_stack"]
          and state["ranked"] == expected,
          "re-answering after undo (new placement) clears the redo stack")
    fresh = new_state([1, 2, 3], 3, [], "<t>", "m", 3, 3)
    before = fresh["undos"]
    do_undo(fresh, lambda st: None)
    check(fresh["undos"] == before and not fresh["ranked"],
          "undo with no placements: no-op, counter not incremented")
    check(do_redo(fresh, lambda st: None) is False
          and fresh["redos"] == 0,
          "redo with empty redo stack: no-op")

    print("quit/resume:")
    state, a = scenario(reversed_seed, quit_at=4)
    snap = a.snapshots[-1]
    check(state["comparisons"] == 4, "quit mid-search: 4 questions recorded")
    check(snap == (state["queue"][0], len(state["ranked"]),
                   len(state["placements"])),
          "quit mid-search: state untouched since the question started")
    dumped = json.loads(json.dumps(state))
    a2 = Auto(by_id, PUNT0, state=dumped)
    run(dumped, a2.ask, lambda st: None)
    check(dumped["ranked"] == expected, "resume after quit: order == z order")
    straight, _ = scenario(near)
    check(straight["done"] and len(straight["queue"]) == 0,
          "straight run marks done and empties the queue")

    print("z math:")
    def mini(vals, key):
        ps = []
        for i, v in enumerate(vals, 1):
            p = {"pts": 0.0, "three": 0.0, "reb": 0.0, "ast": 0.0,
                 "stl": 0.0, "blk": 0.0, "fg_pct": 0.0, "fga": 0.0,
                 "ft_pct": 0.0, "fta": 0.0, "to": 0.0}
            p[key] = float(v)
            ps.append(p)
        compute_z(ps)
        return [p["z"][key] for p in ps]

    # population std of [10,20,30] is sqrt(200/3), so z = (x-20)*sqrt(6)/20
    sqrt6_2 = math.sqrt(6) / 2
    zpts = mini([10, 20, 30], "pts")
    check(all(abs(a - b) < 1e-9 for a, b in
              zip(zpts, [-sqrt6_2, 0.0, sqrt6_2])),
          f"plain z on [10,20,30] -> [-sqrt6/2,0,sqrt6/2] (got {zpts})")
    zto = mini([1, 2, 3], "to")
    check(all(abs(a - b) < 1e-9 for a, b in
              zip(zto, [sqrt6_2, 0.0, -sqrt6_2])),
          f"TO negation on [1,2,3] -> [sqrt6/2,0,-sqrt6/2] (got {zto})")
    zero = mini([7, 7, 7], "pts")
    check(zero == [0.0, 0.0, 0.0], "zero-variance category -> all z = 0")

    p1 = {"fg_pct": 0.60, "fga": 10.0}
    p2 = {"fg_pct": 0.60, "fga": 20.0}
    p3 = {"fg_pct": 0.55, "fga": 15.0}
    for p in (p1, p2, p3):
        for k in CATS:
            p.setdefault(k, 0.0)
        p.setdefault("fta", 0.0)
    compute_z([p1, p2, p3])
    check(p2["z"]["fg_pct"] > p1["z"]["fg_pct"] > p3["z"]["fg_pct"],
          "volume weighting: equal pct, more attempts ranks higher")

    print("punt:")
    check(abs(total_z(by_id[1], {"to"})
              - sum(by_id[1]["z"][c] for c in CATS if c != "to")) < 1e-9,
          "punt to: total_z == sum of the other 8")
    try:
        parse_punt("xyzzy")
        check(False, "unknown punt category rejected")
    except CsvError:
        check(True, "unknown punt category rejected")
    check(parse_punt("fg%,tov") == {"fg_pct", "to"},
          "punt aliases normalize (fg%,tov -> fg_pct,to)")

    print("csv reader:")
    csv_text = (
        "# a comment\n"
        "Player,Team,GP,3PM,PTS,REB,AST,STL,BLK,FG%,FGA,FT%,FTA,TO\n"
        "Nikola Jović,DEN,65,1.7,27.7,12.9,10.7,1.4,0.8,56.9,17.4,83.1,7.4,3.7\n"
        "Short Guy,MIA,2,0.5,5.5,2.2,1.0,0.3,0.1,45.0,6.0,80.0,1.5,0.7\n"
    )
    ps, warns = load_players(io.StringIO(csv_text), "<test>", quiet=True)
    check(len(ps) == 2 and ps[0]["name"] == "Nikola Jović",
          "unicode names + comments parsed")
    check(abs(ps[0]["fg_pct"] - 0.569) < 1e-9,
          "percent column 56.9 normalized to 0.569")
    check(ps[0]["id"] == 1 and ps[1]["id"] == 2,
          "missing rank column -> ids by row order")
    ps, warns = load_players(io.StringIO(csv_text), "<test>", min_g=15,
                             quiet=True)
    check(len(ps) == 1, "min-g filter drops the 2-game player")
    try:
        load_players(io.StringIO("name,pts\nFoo,10\n"), "<test>", quiet=True)
        check(False, "missing required columns rejected")
    except CsvError:
        check(True, "missing required columns rejected")

    print("state:")
    st = new_state([1, 2, 3], 3, {"to"}, "p.csv", "abc", 200, 234)
    st["ranked"] = [1]
    st["placements"] = [1]
    st["queue"] = [2, 3]
    check(json.loads(json.dumps(st)) == st, "state round-trips through JSON")
    st2 = json.loads(json.dumps(st))
    st2["queue"].append(999)
    check(any("999" in e for e in validate_state(st2, {1, 2, 3})),
          "out-of-range id in state is caught")
    st3 = json.loads(json.dumps(st))
    st3["ranked"].append(2)          # 2 is still in the queue -> overlap
    check(any("both ranked and queued" in e
              for e in validate_state(st3, {1, 2, 3})),
          "ranked/queue overlap is caught")

    print("export:")
    tmp = os.path.join(tempfile.gettempdir(), "ds_export_test.txt")
    st4 = new_state([1, 2, 3], 3, [], "<t>", "m", 3, 3)
    st4["ranked"] = [1, 2]
    st4["placements"] = [1, 2]
    st4["queue"] = [3]
    write_export(st4, by_id, set(), tmp)
    content = open(tmp, encoding="utf-8").read()
    rows = content.splitlines()[3:]          # 2 comment lines + 1 header
    check(len(rows) == 2, "export has one line per placed player")
    fields = rows[0].split()
    check(fields[0] == "1." and fields[1] == "Player001" and fields[2] == "F",
          "export line starts: rank, name, position")
    check(abs(float(fields[3]) - round(total_z(by_id[1], set()), 2)) < 1e-9,
          "export line: z comes after position (rounded to 2dp)")
    check(len(fields) == 13, "export line: 9 stats after z "
          f"(got {len(fields) - 4})")
    check("FG%" in content.splitlines()[2]
          and content.splitlines()[2].index("FG%")
          < content.splitlines()[2].index("TO"),
          "export header lists stats in display order")
    os.remove(tmp)

    print(f"selftest: {'ALL PASS' if ok else 'FAILURES'}")
    return ok


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=os.path.join(script_dir, "players.csv"),
                    help="player data file (default: players.csv)")
    ap.add_argument("--n", type=int, default=200,
                    help="number of players to rank (default 200)")
    ap.add_argument("--pool", type=int, default=None,
                    help="compute z-scores over the first N players only")
    ap.add_argument("--punt", default="",
                    help="categories to ignore for totals/z display, "
                         "e.g. 'ft%%,to'")
    ap.add_argument("--min-g", type=int, default=0,
                    help="drop players with fewer than G games (e.g. 15)")
    ap.add_argument("--state", default=os.path.join(script_dir, "state.json"))
    ap.add_argument("--output",
                    default=os.path.join(script_dir, "draft_list.txt"))
    ap.add_argument("--export",
                    default=os.path.join(script_dir, "export.txt"),
                    help="mid-run export file for the 'e' key")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--no-clear", action="store_true")
    ap.add_argument("--restart", action="store_true",
                    help="ignore saved progress and start over")
    ap.add_argument("--selftest", action="store_true",
                    help="run the embedded algorithm tests")
    args = ap.parse_args(argv)

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    with open(args.csv, newline="", encoding="utf-8") as f:
        players, warns = load_players(f, args.csv, min_g=args.min_g)
    for w in warns:
        print(f"  warning: {w}")
    if len(players) < 2:
        print("Need at least 2 players to compare.", file=sys.stderr)
        return 2
    pool_for_z = players if not args.pool else players[:args.pool]
    compute_z(pool_for_z)
    for p in players[len(pool_for_z):]:
        p["z"] = {c: 0.0 for c in CATS}
    punt = parse_punt(args.punt)
    by_id = {p["id"]: p for p in players}
    n = min(args.n, len(players))
    if n < args.n:
        print(f"note: only {len(players)} players available, ranking {n}")
    seed_ids = [p["id"] for p in players[:n]]

    color = (not args.no_color and "NO_COLOR" not in os.environ
             and os.environ.get("TERM") != "dumb" and sys.stdout.isatty())

    state = None
    if os.path.exists(args.state) and not args.restart:
        try:
            with open(args.state, encoding="utf-8") as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"state file is unreadable ({e}) — use --restart to start "
                  f"over, or remove {args.state}", file=sys.stderr)
            return 2
        saved.setdefault("redo_stack", [])
        saved.setdefault("redos", 0)
        if saved.get("done"):
            print(f"Already finished — your list is in {args.output}.")
            print("To rank again from scratch: --restart")
            return 0
        md5 = md5_of(args.csv)
        if saved.get("csv_md5") != md5 \
                or saved.get("csv_rows") != len(players):
            print("Warning: players.csv has changed since the saved "
                  "progress.")
            key = prompt_choice("[c]ontinue with saved progress / "
                                "[r]estart?", "cr", "r")
            if key != "c":
                saved = None
        if saved is not None:
            errs = validate_state(saved, set(by_id))
            if errs:
                print("Saved progress does not match this players.csv:",
                      file=sys.stderr)
                for e in errs:
                    print(f"  - {e}", file=sys.stderr)
                print(f"Remove {args.state} or use --restart to start over.",
                      file=sys.stderr)
                return 2
            if saved.get("n") != n or set(saved.get("punt", [])) != punt:
                print(f"Using saved settings (n={saved['n']}, punt="
                      f"{','.join(saved['punt']) or 'none'}) — CLI settings "
                      f"ignored.")
            state = saved

    if state is None:
        state = new_state(seed_ids, n, punt, os.path.abspath(args.csv),
                          md5_of(args.csv), len(players), len(pool_for_z))
    save_state(state, args.state)

    punt_set = set(state["punt"])
    ui = UI(by_id, state, punt_set, color, args.no_clear, args.export)

    placed, remaining = len(state["ranked"]), len(state["queue"])
    print(f"NBA 9-cat draft ranker — {state['n']} players")
    if placed:
        print(f"Resuming: {placed} placed, {remaining} to go")
    print("Keys: 1/2 pick · t tie · u undo · s skip · r ranking · ? help · "
          "q save & quit")
    if not sys.stdin.isatty():
        print("(stdin is not a terminal: enter '1', '2', 't', 'u', 's', "
              "'r', '?', or 'q' + Enter)")

    try:
        run(state, ui.ask, lambda st: save_state(st, args.state))
    except Quit:
        save_state(state, args.state)
        print()
        print(f"Progress saved ({len(state['ranked'])}/{state['n']} placed). "
              f"Resume anytime with: python3 draft_sort.py")
        return 0
    except KeyboardInterrupt:
        save_state(state, args.state)
        print()
        print(f"Interrupted — progress saved ({len(state['ranked'])}/"
              f"{state['n']} placed). Resume with: python3 draft_sort.py")
        return 130

    write_draft_list(state, by_id, punt_set, args.output)
    print()
    print(f"Done! {state['comparisons']} comparisons, {state['ties']} ties. "
          f"Full list: {args.output}")
    for i, pid in enumerate(state["ranked"][:10], 1):
        p = by_id[pid]
        print(f"  {i:>2}. {p['display']:<24} {p['team']} {p['pos']} "
              f"z {total_z(p, punt_set):+6.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
