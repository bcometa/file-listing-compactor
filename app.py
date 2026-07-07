"""$300 Data Recovery — File Listing Converter & Browser.

Upload a UFS Explorer file-listing HTML report (even the giant ones that
won't open in a browser), then browse, sort, and search it here — and
download a compact standalone HTML viewer to send to the customer.
"""
import hashlib
import hmac
import re
from pathlib import Path

import pandas as pd
import streamlit as st

import ufs_parser as up

st.set_page_config(page_title="File Listing Browser", page_icon="📁", layout="wide")

# ---------------- password gate ----------------
# Password lives in Streamlit secrets only (never in this public repo).
try:
    PASSWORD = st.secrets["password"]
except Exception:
    st.error(
        "No password configured. Add `password = \"...\"` in the app's "
        "Secrets (Streamlit Cloud → app → Settings → Secrets) or in "
        "`.streamlit/secrets.toml` when running locally."
    )
    st.stop()


def check_password() -> bool:
    def entered():
        if hmac.compare_digest(st.session_state.get("pw", ""), PASSWORD):
            st.session_state["auth"] = True
            del st.session_state["pw"]
        else:
            st.session_state["auth"] = False

    if st.session_state.get("auth"):
        return True
    st.title("🔒 File Listing Browser")
    st.text_input("Password", type="password", key="pw", on_change=entered)
    if st.session_state.get("auth") is False:
        st.error("Incorrect password.")
    return False


if not check_password():
    st.stop()

TEMPLATE = (Path(__file__).parent / "viewer_template.html").read_text(encoding="utf-8")


@st.cache_data(show_spinner=False, max_entries=3)
def parse_upload(file_bytes: bytes, _key: str):
    import io
    return up.parse_report(io.BytesIO(file_bytes))


@st.cache_data(show_spinner=False, max_entries=3)
def build_compact_html(_payload, key: str) -> bytes:
    return up.to_compact_html(_payload, TEMPLATE)


def node_at(tree, path):
    """path is a list of (index, name) into successive dirs arrays."""
    node = tree
    for idx, name in path:
        if idx < len(node[6]) and node[6][idx][0] == name:
            node = node[6][idx]
        else:  # tree changed (new upload) — reset
            return None
    return node


st.title("📁 File Listing Converter & Browser")

uploaded = st.file_uploader(
    "Upload a UFS Explorer file-listing report (.html)",
    type=["html", "htm"],
    help="Works with reports too large to open in a browser.",
)

if not uploaded:
    st.info("Upload a file-listing report to get started.")
    st.stop()

file_bytes = uploaded.getvalue()
key = hashlib.sha1(file_bytes).hexdigest()

if st.session_state.get("file_key") != key:
    st.session_state["file_key"] = key
    st.session_state["path"] = []

try:
    with st.spinner("Parsing report… (large reports take ~30–60 seconds)"):
        payload = parse_upload(file_bytes, key)
except up.ParseError as e:
    st.error(str(e))
    st.stop()

tree = payload["tree"]
vol = payload["vol"] or uploaded.name

# ---------------- header ----------------
c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
c1.subheader(vol)
c2.metric("Files", f"{tree[3]:,}")
c3.metric("Folders", f"{tree[4]:,}")
c4.metric("Total size", up.fmt_size(tree[2]))

ticket = re.match(r"^(\d{4,6})\b", uploaded.name)
out_name = (ticket.group(1) + "-" if ticket else "") + "File-Listing-compact.html"
compact = build_compact_html(payload, key)
st.download_button(
    f"⬇️ Download compact viewer ({len(compact) / 1e6:.0f} MB standalone HTML for the customer)",
    data=compact,
    file_name=out_name,
    mime="text/html",
    type="primary",
)

tab_browse, tab_search = st.tabs(["🗂 Browse", "🔎 Search"])

# ---------------- browse ----------------
with tab_browse:
    path = st.session_state.get("path", [])
    node = node_at(tree, path)
    if node is None:
        st.session_state["path"] = []
        node = tree
        path = []

    # breadcrumbs
    crumbs = st.columns(min(len(path), 8) + 1, gap="small")
    if crumbs[0].button("🏠 Root", key="bc_root"):
        st.session_state["path"] = []
        st.rerun()
    for i, (_, name) in enumerate(path[-8:]):
        real_i = len(path) - min(len(path), 8) + i
        label = name if len(name) <= 24 else name[:21] + "…"
        if crumbs[i + 1].button(f"📁 {label}", key=f"bc_{real_i}"):
            st.session_state["path"] = path[: real_i + 1]
            st.rerun()

    sort_by = st.radio(
        "Sort by",
        ["Size (largest first)", "File count", "Name"],
        horizontal=True,
        label_visibility="collapsed",
    )

    # ---- subfolders table ----
    dirs = list(enumerate(node[6]))
    if sort_by.startswith("Size"):
        dirs.sort(key=lambda x: -x[1][2])
    elif sort_by.startswith("File"):
        dirs.sort(key=lambda x: -x[1][3])
    if dirs:
        st.caption(f"{len(dirs):,} subfolders — click a row to open")
        df = pd.DataFrame(
            {
                "Folder": [d[0] for _, d in dirs],
                "Size": [up.fmt_size(d[2]) for _, d in dirs],
                "Bytes": [d[2] for _, d in dirs],
                "Files": [d[3] for _, d in dirs],
                "Subfolders": [d[4] for _, d in dirs],
                "Modified": [up.fmt_ts(d[1]) for _, d in dirs],
            }
        )
        sel = st.dataframe(
            df,
            hide_index=True,
            width="stretch",
            height=min(38 * len(df) + 40, 420),
            on_select="rerun",
            selection_mode="single-row",
            key=f"dirs_{key}_{len(path)}_{sort_by}",
            column_config={
                "Files": st.column_config.NumberColumn(format="%d"),
                "Bytes": st.column_config.NumberColumn(help="Exact size in bytes"),
            },
        )
        rows = sel.selection.rows if sel and sel.selection else []
        if rows:
            child_idx = dirs[rows[0]][0]
            st.session_state["path"] = path + [(child_idx, node[6][child_idx][0])]
            st.rerun()

    # ---- files table ----
    files = node[5]
    if files:
        if sort_by.startswith("Name"):
            fsorted = files
        else:
            fsorted = sorted(files, key=lambda f: -f[1])
        st.caption(f"{len(fsorted):,} files in this folder")
        fdf = pd.DataFrame(
            {
                "File": [f[0] for f in fsorted],
                "Size": [up.fmt_size(f[1]) for f in fsorted],
                "Bytes": [f[1] for f in fsorted],
                "Modified": [up.fmt_ts(f[2]) for f in fsorted],
            }
        )
        st.dataframe(fdf, hide_index=True, width="stretch",
                     height=min(38 * len(fdf) + 40, 560))
    if not files and not dirs:
        st.caption("This folder is empty.")

# ---------------- search ----------------
with tab_search:
    q = st.text_input("Search all file and folder names", placeholder="e.g. .jpg, QuickBooks, IMG_")
    if q and len(q.strip()) >= 2:
        with st.spinner("Searching…"):
            res, truncated = up.walk_search(tree, q.strip())
        if truncated:
            st.warning(f"Showing first {len(res):,} matches — narrow the search for full results.")
        else:
            st.caption(f"{len(res):,} matches")
        if res:
            rdf = pd.DataFrame(
                {
                    "Type": ["📁" if r[4] else "📄" for r in res],
                    "Name": [r[0] for r in res],
                    "Size": [up.fmt_size(r[2]) for r in res],
                    "Bytes": [r[2] for r in res],
                    "Modified": [up.fmt_ts(r[3]) for r in res],
                    "Path": [r[1] for r in res],
                }
            )
            st.dataframe(rdf, hide_index=True, width="stretch", height=600)
    elif q:
        st.caption("Type at least 2 characters.")
