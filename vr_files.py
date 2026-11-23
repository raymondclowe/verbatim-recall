"""File-corpus source for verbatim-recall.

Why this exists: the session index can only point at turns of a *transcript*. A fresh
subagent's opening turn has no transcript, so the only useful thing to hand it is the
dated record on disk that names the exact entity in its work order — the site, the row,
the venue. This module builds that corpus and answers one question:

    given a work order, which file was the last one written about this entity?

Two layers, because they cost different things:

  * path index  -- one walk of the roots, no file reads. Maps a site key to its files
                   with mtimes. Cheap enough to rebuild on a timer (46k files ~ 2s).
  * body search -- per lookup: ripgrep over that site's own files for the entity's words.
                   Site-scoped on purpose: a hotel name is only ambiguous across sites,
                   never inside one. Mechanical, one process, nothing cached on disk --
                   a token index over the same corpus answers a little faster and costs
                   hundreds of megabytes, which is not a trade a plugin should make for
                   a line of text read once at the top of a turn.

A miss is silence. A wrong pointer is worse than none, so every candidate has to be
supported by the entity's own words in the file body, not by a filename guess.

Stdlib only, never raises: the plugin treats an exception here as "no file layer".
"""

import json
import os
import re
import shutil
import subprocess
import time

V = 2

# ---- limits -------------------------------------------------------------------
MAX_FILES = 120000            # path-index walk cap; keeps a bad root from hanging a turn
MAX_SITE_FILES = 900          # newest files per site that a lookup is allowed to search
MAX_BYTES = 400_000           # skip anything bigger; these are notes, not dumps
TEXT_EXT = {
    ".md", ".txt", ".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml", ".html",
    ".htm", ".py", ".sh", ".sql", ".log", ".diff", ".patch", ".toml", ".ini", ".xml",
}
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", "venv",
    ".venv", "dist", "build", ".next", ".cache", "vendor", "site-packages",
}
# Roots that are archives rather than working records: indexed for paths (cheap) but
# never searched for a body match.
SKIP_TEXT = {"backup", "scratch", "tools", "pipeline", "newpage_income", "cache"}

STOP = set("""
a an the and or but if then than that this these those there here it its it's is are
was were be been being am do does did doing have has had having will would shall should
can could may might must not no nor so as at by for from in into of off on onto out over
per to up via with within without about across after again against all almost along also
although always among another any anyone anything are around back bad be because been
before being below best better between both bring but buy call called came can cannot
card care case change changed check city come comes coming could country current date
day days deal did different do does doing done down each early easy edit edited else end
even ever every few file files find first five fix fixed follow for found four from full
get gets give go going good got great group had half hand has have he head help her here
high him his hold home hotel how however idea if important in include included info
information into is issue it item its job just keep kept key kind know known last late
later least leave left less let level like line link list little live long look looking
made make makes making many may maybe me mean means meet might mind minute more most
much must my name named near need needs never new next nice night no none nor not note
notes nothing now number of often old on once one only open or order other our out over
own page pages part people per place plan please point post price prices put question
quick quite rather read really report reported request right room rooms run said same
save saw say says second see seem seen send sent set several she should show side since
site sites small so some something soon sort start state still stop such sure take taken
task than thank that the their them then there these they thing things think this those
though three through time times to today together too took top total try turn two under
until update updated up upon us use used useful using very visit want was way we week
well went were what when where whether which while who whole why will with within work
worked working would write written year years yes yet you your row rows price prices
opening hours close closed com www http https site_name value values name title cost
# what to do to a record, never what the record is about
stale refresh rebuild rewrite replace rework verify audit improve expand correct delete remove
add move sync clean prune sweep recheck rename publish deploy migrate convert
fee ticket restaurant restaurants hotel hotels
""".split())

_DOMAIN = re.compile(r"\b([a-z0-9][a-z0-9-]{1,62})\.(com|hk|net|asia|tw|org|co|info|biz)\b")
_WORD = re.compile(r"[a-z][a-z0-9-]{2,40}")
_NUM = re.compile(r"\b\d{3,12}\b")


# ---- paths --------------------------------------------------------------------
def home():
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def default_index_path():
    return os.path.join(home(), "cache", "verbatim-recall", "files-index.json")


def default_roots(roots=None):
    if roots:
        return [os.path.abspath(os.path.expanduser(r)) for r in roots if r]
    out = []
    for cand in (os.path.join(home(), "wl-knowledge"),
                 os.path.join(os.getcwd(), "wl-knowledge"),
                 os.path.expanduser("~/wl-knowledge")):
        cand = os.path.abspath(cand)
        if os.path.isdir(cand) and cand not in out:
            out.append(cand)
    return out


def _rel(path, roots):
    for r in roots:
        if path.startswith(r + os.sep):
            return os.path.relpath(path, r).replace(os.sep, "/")
    return path.replace(os.sep, "/")


def _site_key(rel):
    """The entity a file belongs to: `sites/<site>/...` -> site, else the top folder."""
    parts = rel.split("/")
    if len(parts) > 2 and parts[0] == "sites":
        return parts[1]
    if len(parts) > 2 and parts[0] == "findings":
        return parts[1]
    return parts[0] if len(parts) > 1 else ""


def _walk(roots, budget=MAX_FILES):
    seen = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in SKIP_DIRS and not d.startswith("."))
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext and ext not in TEXT_EXT:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not st.st_size or st.st_size > MAX_BYTES:
                    continue
                seen += 1
                if seen > budget:
                    return
                yield p, st.st_mtime, st.st_size


# ---- index build --------------------------------------------------------------
def build(roots=None, path=None):
    roots = default_roots(roots)
    path = path or default_index_path()
    sites = {}
    known = set()
    n = 0
    for p, mtime, size in _walk(roots):
        rel = _rel(p, roots)
        key = _site_key(rel)
        if not key:
            continue
        sites.setdefault(key, []).append([rel, int(mtime), size])
        n += 1
        low = rel.lower()
        known.add(key)
        m = _DOMAIN.search(low)
        if m:
            known.add(m.group(0))
    for key in list(sites):
        sites[key].sort(key=lambda r: -r[1])
        # a site's own name and its domain both point at the same file list
        known.add(key)
        known.add(key + ".com")
    idx = {"v": V, "built": int(time.time()), "roots": roots, "files": n,
           "known": sorted(known), "sites": sites}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(idx, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass
    return idx


def load(refresh=6 * 3600, roots=None, path=None):
    """Return the path index, rebuilding it when missing or older than `refresh`."""
    path = path or default_index_path()
    try:
        with open(path) as fh:
            idx = json.load(fh)
        if idx.get("v") == V and idx.get("sites"):
            if refresh and time.time() - idx.get("built", 0) > refresh:
                _rebuild_bg(roots, path)
            return idx
    except (OSError, ValueError):
        pass
    try:
        return build(roots, path)
    except Exception:
        return {"v": V, "built": 0, "roots": [], "files": 0, "known": [], "sites": {}}


def _rebuild_bg(roots, path):
    """Rebuild behind the current turn when the index has gone stale.

    The old index keeps serving until the new one replaces it, and only one process
    builds at a time (the lock is a plain file; a stale lock is ignored after 30 min).
    """
    lock = path + ".lock"
    try:
        if os.path.exists(lock):
            if time.time() - os.path.getmtime(lock) < 1800:
                return
        open(lock, "w").close()
    except OSError:
        return
    try:
        if os.name == "posix":
            if os.fork():
                return
        build(roots, path)
    except Exception:
        pass
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass
    if os.name == "posix":
        os._exit(0)


# ---- body search (per site, on demand) ----------------------------------------
def _skip_rel(rel):
    return any(p in SKIP_TEXT for p in rel.split("/")[:-1])


def _read_text(p):
    try:
        with open(p, "rb") as fh:
            raw = fh.read(MAX_BYTES)
        return raw.decode("utf-8", "ignore").lower()
    except OSError:
        return ""


def _tokens(text):
    out = []
    for w in _WORD.findall(text):
        if w not in STOP and len(w) >= 3:
            out.append(w)
    out.extend(_NUM.findall(text))
    return out


def _grep(root, cand, pats):
    """{rel: {pattern, ...}} for the candidate files whose body carries a pattern.

    ripgrep when it is installed (one process, ~20 ms for a few hundred files), plain
    Python over the same files when it is not. Both are lexical: the question is "does
    this file say these words", not "is it semantically close", which is what keeps the
    answer checkable and the cost bounded.
    """
    found = {}
    if not pats or not cand:
        return found
    rg = shutil.which("rg")
    if rg:
        # -H is load-bearing: with a single file ripgrep drops the path prefix
        cmd = [rg, "-i", "-o", "-w", "-H", "--no-messages", "--color", "never", "--no-heading"]
        for p in pats:
            cmd += ["-e", re.escape(p)]
        cmd += [os.path.join(root, f[0]) for f in cand]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=25).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            path, _, w = line.rpartition(":")
            if not path or not w:
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            found.setdefault(rel, set()).add(w.strip().lower())
        if found:
            return found
    for f in cand:                       # no ripgrep, or it printed nothing: read the files
        rel = f[0]
        body = _read_text(os.path.join(root, rel))
        if not body:
            continue
        for p in pats:
            if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", body):
                found.setdefault(rel, set()).add(p)
    return found


def _score(root, files, words, nums, ident=""):
    """Files ranked by how many of the entity's own words they carry, newest first.

    The filename counts as part of the record: 2026-11-22-daven-haven-1589-field-fill.md
    is evidence about daven haven without opening it.
    """
    cand = [f for f in files[:MAX_SITE_FILES] if not _skip_rel(f[0])]
    if not cand:
        return {}
    pats = [w for w in dict.fromkeys(words) if len(w) >= 3] + [n for n in dict.fromkeys(nums) if n]
    found = _grep(root, cand, pats)
    # a filename naming the entity is strong, and costs nothing to check
    for f in cand:
        stem = set(_tokens(f[0].rsplit("/", 1)[-1]))
        for p in pats:
            if p in stem:
                found.setdefault(f[0], set()).add(p)
    if not found:
        return {}
    n = len(cand)
    df = {}
    for ts in found.values():
        for t in ts:
            df[t] = df.get(t, 0) + 1
    mtimes = {f[0]: f[1] for f in cand}
    hits = {}
    for rel, ts in found.items():
        # [score, terms matched, mtime, terms worth naming, terms that are not the site key]
        h = hits.setdefault(rel, [0.0, 0, mtimes.get(rel, 0), [], set()])
        for w in ts:
            d = df.get(w, 1)
            weight = 0.2 if d > n * 0.6 else 1.0 + (1.0 - d / n)
            h[0] += weight
            h[1] += 1
            if w != ident:
                h[4].add(w)
            if w in nums:
                h[0] += 1.5        # a row number is the narrowest handle there is
            # the site key is always worth naming in the pointer, however common it is --
            # it is what makes the line checkable at a glance.
            if (d <= n * 0.4 or w == ident) and len(h[3]) < 6 and w not in h[3]:
                h[3].append(w)
    return hits


# ---- lookup -------------------------------------------------------------------
def _entities(msg):
    """The words a work order is really about: site keys, venue words, row numbers."""
    low = msg.lower()
    ent, sites, nums = [], [], []
    for m in _DOMAIN.finditer(low):
        sites.append(m.group(1))
        sites.append(m.group(0))
    for w in _WORD.findall(low):
        if w in STOP or len(w) < 3:
            continue
        ent.append(w)
    nums = _NUM.findall(low)
    return list(dict.fromkeys(sites)), list(dict.fromkeys(ent)), nums


def _base(key):
    return key.split(".")[0].lower()


def _match_sites(idx, sites, ent):
    keys = list(idx.get("sites") or {})
    hit = []
    joined = " ".join(ent)
    for s in sites:
        for k in keys:
            if k == s or k.startswith(s + ".") or s.startswith(k):
                if k not in hit:
                    hit.append(k)
    if hit:
        # one line per real entity: castle-dell-ovo and castel-dell-ovo.com are one site
        out, seen = [], set()
        for k in hit:
            b = _base(k)
            if b in seen:
                continue
            seen.add(b)
            out.append(k)
        return out[:3]
    # no domain in the message: look for a site key spelled out in the words
    for k in keys:
        base = k.split(".")[0].replace("-", " ").replace("_", " ")
        toks = [t for t in base.split() if len(t) > 2 and t not in STOP]
        if toks and all(t in ent for t in toks):
            hit.append(k)
    return hit[:3]


def _fmt_day(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts + 8 * 3600)) + " HKT"
    except (ValueError, OSError):
        return "undated"


def lookup(msg, top_k=3, idx=None):
    """Return ([line, ...], [{"path": rel, ...}, ...]) for the entities in `msg`."""
    if idx is None:
        idx = load()
    sites = idx.get("sites") or {}
    if not msg or not sites:
        return [], []
    site_names, ent, nums = _entities(msg)
    keys = _match_sites(idx, site_names, ent)
    if not keys:
        return [], []
    root = (idx.get("roots") or [None])[0]
    lines, refs = [], []
    for key in keys:
        files = sites.get(key) or []
        if not files:
            continue
        # the key itself is a word like any other: a message that names the site gets
        # the file that names it, even when nothing else in the order is in the body.
        words = ent + [k for k in site_names if k in key] + [key]
        try:
            hits = _score(root, files, words, nums, ident=key)
        except Exception:
            continue
        if not hits:
            continue
        for rel, h in hits.items():          # a filename naming the entity is strong
            stem = rel.rsplit("/", 1)[-1].lower()
            if sum(1 for w in h[3] if w in stem) >= 2:
                h[0] += 1.2
        ranked = sorted(hits.items(), key=lambda kv: (-kv[1][0], -kv[1][2]))
        best, meta = ranked[0]
        if meta[1] < 2 and not any(w in nums for w in meta[3]):
            # one shared word is not evidence of the same entity -- unless that word is the
            # site key itself, which is an identifier rather than a coincidence of vocabulary.
            if meta[3] != [key]:
                continue
        older = len(ranked) - 1
        tail = f"; +{older} more matching file{'s' if older != 1 else ''}" if older else ""
        site_only = not (meta[4] - set(site_names))
        if site_only:
            # nothing in the order names an entity inside the site: hand over the site's
            # newest record and say so plainly, rather than dress it up as a match.
            lines.append(
                f"[verbatim-recall] latest file on {_base(key)}: {_fmt_day(meta[2])} — {best} "
                f"(no word of this order matched beyond the site name{tail}) — skim it if the "
                f"job is site-wide."
            )
        else:
            words = ", ".join(meta[3][:5])
            lines.append(
                f"[verbatim-recall] file record for {_base(key)}: newest {_fmt_day(meta[2])} — "
                f"{best} (matched: {words}{tail}) — read it before changing anything it names."
            )
        refs.append({"path": best, "key": key, "mtime": meta[2],
                     "terms": meta[3][:5], "score": round(meta[0], 2)})
        # a dated record naming the entity says what was decided; the aggregate store
        # says what the value is now. Hand over both when the top hit is not a record.
        if re.match(r"\d{4}-\d{2}-\d{2}", best.rsplit("/", 1)[-1]):
            ranked = []
        for rel, h in ranked[1:]:
            stem = rel.rsplit("/", 1)[-1]
            if re.match(r"\d{4}-\d{2}-\d{2}", stem) and h[1] >= 2:
                lines.append(
                    f"[verbatim-recall] last dated record for {_base(key)}: "
                    f"{_fmt_day(h[2])} — {rel}"
                )
                refs.append({"path": rel, "key": key, "mtime": h[2],
                             "terms": h[3][:5], "score": round(h[0], 2)})
                break
        if len(lines) >= max(1, int(top_k or 1)):
            break
    return lines, refs


# ---- cli ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        t0 = time.time()
        i = build()
        print("built", i["files"], "files", len(i["sites"]), "sites ->", default_index_path(),
              "(%.1fs)" % (time.time() - t0))
    else:
        q = " ".join(sys.argv[1:]) or "fix the price of Daven Haven Lodge, trail-ridge-road.com row 1589"
        t0 = time.time()
        lines, refs = lookup(q)
        for ln in lines:
            print(ln)
        print("-- %d line(s) in %.2fs" % (len(lines), time.time() - t0))
