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
    """Parse a UFS Explorer report. Returns {'vol': str, 'tree': node}."""
    text = io.TextIOWrapper(binary_stream, encoding="utf-8-sig", errors="replace")
    folders = {}   # id -> {'n', 'p', 'files', 'dirs'}
    root_id = None
    vol_name = ""
    cur = None

    def parse_row(line):
        m = ROW_RE.match(line)
        if not m:
            return False
        _id, size, isfile, ts, name, _cc = m.groups()
        name = _unesc(name)
        if isfile == "1":
            cur["files"].append([name, int(size), int(ts)])
        else:
            cur["dirs"].append((int(_id), name, int(ts)))
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
            cur = {"n": "", "p": None, "files": [], "dirs": []}
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

    # ---- build nested tree iteratively --------------------------------
    def make_node(fid, fallback_name, ts):
        rec = folders.get(fid)
        name = (rec["n"] if rec and rec["n"] else fallback_name) or ""
        files = rec["files"] if rec else []
        dirs = rec["dirs"] if rec else []
        return [name, ts, 0, 0, 0, files, []], dirs

    visited = set()
    order = []

    def grow(seed_node, seed_dirs):
        stack = [(seed_node, seed_dirs)]
        while stack:
            node, dirs = stack.pop()
            order.append(node)
            for did, dname, dts in dirs:
                if did in visited:
                    continue
                visited.add(did)
                child, cdirs = make_node(did, dname, dts)
                node[6].append(child)
                stack.append((child, cdirs))

    root_node, root_dirs = make_node(root_id, vol_name or "Root", 0)
    visited.add(root_id)
    grow(root_node, root_dirs)

    # attach orphan trees (chains that never reach the root)
    def chain_top(fid):
        seen = set()
        while fid not in seen:
            seen.add(fid)
            p = folders[fid]["p"]
            if p is None or p == fid or p not in folders:
                return fid
            fid = p
        return fid

    tops = {chain_top(f) for f in folders if f not in visited}
    tops = {t for t in tops if t not in visited}
    for t in sorted(tops):
        visited.add(t)
        tnode, tdirs = make_node(t, folders[t]["n"] or "(Recovered items)", 0)
        pre = len(order)
        grow(tnode, tdirs)
        if folders[t]["n"]:
            root_node[6].append(tnode)
        else:
            # unnamed top: merge its children straight into the root
            root_node[5].extend(tnode[5])
            root_node[6].extend(tnode[6])
            order.pop(pre)  # drop the placeholder top node itself

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

    return {"vol": vol_name, "tree": root_node}


def to_compact_html(payload, template_str):
    """Splice gzip+base64 payload into the standalone viewer template."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(gzip.compress(raw, 9)).decode()
    head, tail = template_str.split("__DATA__")
    return (head + b64 + tail).encode("utf-8")


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
