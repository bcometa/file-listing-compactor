"""Parser for UFS Explorer HTML file-listing reports.

Converts the giant inline-JavaScript report into a compact nested tree:

    folder node: [name, ts, total_size, file_count, dir_count, files, dirs]
    file entry:  [name, size, ts]

Orphaned subtrees (folders not reachable from the declared root, e.g. a
detached Lost & Found tree) are attached so nothing is hidden.
"""
import re
import io
import json
import gzip
import base64

FLD_RE = re.compile(r"^var _fld_(\d+) = \{")
P_RE = re.compile(r"^'p': BigInt\('(\d+)'\),")
N_RE = re.compile(r"^'n': \"((?:[^\"\\]|\\.)*)\",")
ROW_RE = re.compile(
    r"^\[BigInt\('(\d+)'\),(\d+),([01]),(\d+),"
    r"\"(?:[^\"\\]|\\.)*\",\"((?:[^\"\\]|\\.)*)\",(\d+)\],?$"
)
ROOT_RE = re.compile(r"^var _rootFolder = BigInt\('(\d+)'\);")
VOL_RE = re.compile(r'^var _vol_name = "((?:[^"\\]|\\.)*)";')


def _unesc(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")


class ParseError(Exception):
    pass


def parse_report(binary_stream):
    """Parse a UFS Explorer report. Returns {'vol': str, 'tree': node}.

    Handles both report variants:
    - Variant A (e.g. #72288): each folder's 'c' array contains rows for BOTH
      files (flag 1) and subfolders (flag 0), and blocks also carry 'p'.
    - Variant B (e.g. #72974): 'c' arrays contain ONLY file rows; the folder
      hierarchy exists solely through each block's 'p' parent pointer.
    The tree is therefore built from 'p' pointers (authoritative in both);
    flag-0 rows are used only as metadata (folder timestamps).
    """
    text = io.TextIOWrapper(binary_stream, encoding="utf-8-sig", errors="replace")
    folders = {}   # id -> {'n', 'p', 'files', 'dmeta': {child_id: ts}}
    root_id = None
    vol_name = ""
    cur = None

    def parse_row(line):
        m = ROW_RE.match(line)
        if not m:
            return False
        _id, size, isfile, ts, name, _cc = m.groups()
        if isfile == "1":
            cur["files"].append([_unesc(name), int(size), int(ts)])
        else:
            cur["dmeta"][int(_id)] = (_unesc(name), int(ts))
        return True

    for line in text:
        line = line.rstrip("\n")
        if cur is not None:
            if line.startswith("'p'"):
                m = P_RE.match(line)
                if m:
                    cur["p"] = int(m.group(1))
                continue
            if line.startswith("'n'"):
                m = N_RE.match(line)
                if m:
                    cur["n"] = _unesc(m.group(1))
                continue
            if line.startswith("'c'"):
                rest = line[len("'c': ["):]
                if rest == "]};":
                    cur = None
                    continue
                closed = rest.endswith("]};")
                if closed:
                    rest = rest[:-3]
                parse_row(rest)
                if closed:
                    cur = None
                continue
            if line.startswith("]};"):
                cur = None
                continue
            parse_row(line)
            continue
        m = FLD_RE.match(line)
        if m:
            cur = {"n": "", "p": None, "files": [], "dmeta": {}}
            folders[int(m.group(1))] = cur
            continue
        m = ROOT_RE.match(line)
        if m:
            root_id = int(m.group(1))
            continue
        m = VOL_RE.match(line)
        if m:
            vol_name = _unesc(m.group(1))

    if root_id is None or not folders:
        raise ParseError(
            "This doesn't look like a UFS Explorer file-listing report "
            "(no folder data / root marker found)."
        )

    # ---- build hierarchy from 'p' parent pointers ---------------------
    children = {}  # parent folder id -> [child folder ids]
    for fid, rec in folders.items():
        p = rec["p"]
        if p is not None and p != fid and p in folders:
            children.setdefault(p, []).append(fid)
    if not children:
        # Parent pointers unusable (all self/missing) — fall back to the
        # variant-A flag-0 child rows for linkage instead.
        for fid, rec in folders.items():
            for cid in rec["dmeta"]:
                if cid in folders and cid != fid:
                    children.setdefault(fid, []).append(cid)

    visited = set()
    order = []

    def grow(seed_id, seed_node):
        stack = [(seed_id, seed_node)]
        while stack:
            fid, node = stack.pop()
            order.append(node)
            rec = folders[fid]
            for cid in children.get(fid, ()):
                if cid in visited:
                    continue
                visited.add(cid)
                crec = folders[cid]
                meta = rec["dmeta"].get(cid)
                cname = crec["n"] or (meta[0] if meta else "")
                child = [cname, meta[1] if meta else 0, 0, 0, 0,
                         crec["files"], []]
                node[6].append(child)
                stack.append((cid, child))

    root_rec = folders.get(root_id)
    root_name = (root_rec["n"] if root_rec and root_rec["n"] else "") or vol_name or "Root"
    root_node = [root_name, 0, 0, 0, 0,
                 root_rec["files"] if root_rec else [], []]
    visited.add(root_id)
    if root_rec:
        grow(root_id, root_node)
    else:
        order.append(root_node)

    # attach orphan trees (parent chains that never reach the root)
    def chain_top(fid):
        seen = set()
        while fid not in seen:
            seen.add(fid)
            p = folders[fid]["p"]
            if p is None or p == fid or p not in folders or p in visited:
                return fid
            fid = p
        return fid

    tops = {chain_top(f) for f in folders if f not in visited}
    tops = {t for t in tops if t not in visited}
    extra_files = []  # accumulated once at the end (O(n), not O(n^2))
    for t in sorted(tops):
        visited.add(t)
        trec = folders[t]
        tnode = [trec["n"] or "(Recovered items)", 0, 0, 0, 0, trec["files"], []]
        pre = len(order)
        grow(t, tnode)
        if trec["n"]:
            root_node[6].append(tnode)
        else:
            # unnamed top: merge its children straight into the root
            extra_files.extend(tnode[5])
            root_node[6].extend(tnode[6])
            order.pop(pre)  # drop the placeholder top node itself
    if extra_files:
        root_node[5] = root_node[5] + extra_files

    # ---- totals + alphabetical base order (children before parents) ---
    for node in reversed(order):
        fs = sum(f[1] for f in node[5])
        fc = len(node[5])
        dc = len(node[6])
        for c in node[6]:
            fs += c[2]
            fc += c[3]
            dc += c[4]
        node[2], node[3], node[4] = fs, fc, dc
        node[5].sort(key=lambda f: f[0].lower())
        node[6].sort(key=lambda d: d[0].lower())

    # Flat-report detection: many folder blocks but no reconstructable
    # hierarchy means UFS Explorer exported the report without folder
    # names/links (seen on ticket #72974 — 270k unnamed, self-parented
    # folder blocks). The listing is still complete, just flat.
    flat = len(folders) >= 50 and root_node[4] == 0

    return {"vol": vol_name, "tree": root_node, "flat": flat}


def to_compact_html(payload, template_str):
    """Splice gzip+base64 payload into the standalone viewer template.

    Streams json straight into gzip so the full uncompressed JSON string
    (which can be hundreds of MB for huge listings) never exists in memory.
    """
    buf = io.BytesIO()
    gz = gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0)
    with io.TextIOWrapper(gz, encoding="utf-8") as writer:
        json.dump(payload, writer, ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(buf.getvalue()).decode()
    head, tail = template_str.split("__DATA__")
    return (head + b64 + tail).encode("utf-8")


def payload_from_compact_html(compact_bytes):
    """Recover the parsed payload from a compact viewer HTML (for preview)."""
    m = re.search(rb'<script id="payload"[^>]*>([^<]*)</script>', compact_bytes)
    if not m:
        raise ParseError("Couldn't find embedded data in compact HTML.")
    return json.loads(gzip.decompress(base64.b64decode(m.group(1))))


def walk_search(tree, query, limit=5000):
    """Case-insensitive substring search. Returns rows:
    (name, path, size_bytes, ts, is_dir)."""
    q = query.lower()
    res = []
    stack = [(tree, "")]
    while stack:
        node, path = stack.pop()
        here = path + (node[0] or "") + "/"
        for f in node[5]:
            if q in f[0].lower():
                res.append((f[0], here, f[1], f[2], False))
                if len(res) >= limit:
                    return res, True
        for d in node[6]:
            if q in d[0].lower():
                res.append((d[0], here, d[2], d[1], True))
                if len(res) >= limit:
                    return res, True
            stack.append((d, here))
    return res, False


def fmt_size(b):
    if b < 1024:
        return f"{b} B"
    for unit in ("KB", "MB", "GB", "TB"):
        b /= 1024.0
        if b < 1024:
            return f"{b:.0f} {unit}" if b >= 100 else f"{b:.1f} {unit}"
    return f"{b:.1f} PB"


def fmt_ts(ts):
    if not ts:
        return ""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m/%d/%Y %H:%M")
