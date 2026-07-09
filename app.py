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

st.set_page_config(page_title="File Listing Compactor", page_icon="📁", layout="wide")

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
    st.title("🔒 File Listing Compactor")
    st.text_input("Password", type="password", key="pw", on_change=entered)
    if st.session_state.get("auth") is False:
        st.error("Incorrect password.")
    return False


if not check_password():
    st.stop()

TEMPLATE = (Path(__file__).parent / "viewer_template.html").read_text(encoding="utf-8")

# ===== SLACK CONFIGURATION (same secrets as the ACE Report Converter) =====
try:
    SLACK_BOT_TOKEN = st.secrets["SLACK_BOT_TOKEN"]
    SLACK_CHANNEL_ID = st.secrets["SLACK_CHANNEL_ID"]
    SLACK_UPLOAD_PASSWORD = st.secrets["SLACK_UPLOAD_PASSWORD"]
except KeyError:
    SLACK_BOT_TOKEN = None
    SLACK_CHANNEL_ID = None
    SLACK_UPLOAD_PASSWORD = None


def upload_to_slack(zip_data, ticket_label, channel_id, token, zip_name):
    """Upload the ZIP file to the Slack file-listing channel."""
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        client = WebClient(token=token)
        client.files_upload_v2(
            channel=channel_id,
            file=zip_data,
            filename=zip_name,
            title=f"File Listing for Ticket {ticket_label}",
            initial_comment=f"📊 Compacted file listing generated for ticket #{ticket_label}",
        )
        return True, "File uploaded successfully to Slack!"
    except SlackApiError as e:
        return False, f"Slack API Error: {e.response['error']}"
    except Exception as e:
        return False, f"Upload failed: {str(e)}"


def convert(file_bytes: bytes):
    """Parse the report and build the compact HTML, then immediately free
    the huge parsed tree. Only the small compact HTML + summary stats stay
    resident — this is what keeps the app inside Streamlit Cloud's memory
    limit (holding the tree while serving a download is what crashed it)."""
    import gc
    import io
    payload = up.parse_report(io.BytesIO(file_bytes))
    t = payload["tree"]
    stats = {"vol": payload["vol"], "files": t[3], "dirs": t[4], "size": t[2]}
    compact = up.to_compact_html(payload, TEMPLATE)
    del payload, t
    gc.collect()
    return compact, stats


# Preview only: rebuild the tree from the compressed data on demand.
# _compact has a leading underscore so Streamlit doesn't hash 10+ MB of
# bytes on every rerun; `key` alone controls caching. max_entries=1 keeps
# at most one tree in memory, and only while preview is in use.
@st.cache_resource(show_spinner=False, max_entries=1)
def load_tree(key: str, _compact: bytes):
    return up.payload_from_compact_html(_compact)["tree"]


def node_at(tree, path):
    """path is a list of (index, name) into successive dirs arrays."""
    node = tree
    for idx, name in path:
        if idx < len(node[6]) and node[6][idx][0] == name:
            node = node[6][idx]
        else:  # tree changed (new upload) — reset
            return None
    return node


st.title("📁 File Listing Compactor")

uploaded = st.file_uploader(
    "Upload a UFS Explorer file-listing report (.html, or a .zip containing one)",
    type=["html", "htm", "zip"],
    help="Works with reports too large to open in a browser.",
)

if not uploaded:
    st.info("Upload a file-listing report to get started.")
    st.stop()

file_bytes = uploaded.getvalue()
src_name = uploaded.name
if src_name.lower().endswith(".zip"):
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            members = [
                m for m in z.namelist()
                if m.lower().endswith((".html", ".htm"))
                and not m.startswith("__MACOSX")
            ]
            if not members:
                st.error("No .html file found inside that zip.")
                st.stop()
            if len(members) > 1:
                members = [st.selectbox("Multiple HTML files in zip — pick one:", members)]
            src_name = Path(members[0]).name
            file_bytes = z.read(members[0])
    except zipfile.BadZipFile:
        st.error("That zip file couldn't be opened.")
        st.stop()
key = hashlib.sha1(file_bytes).hexdigest()

if st.session_state.get("file_key") != key:
    try:
        with st.spinner("Converting report… (large reports take ~30–60 seconds)"):
            compact, stats = convert(file_bytes)
    except up.ParseError as e:
        st.error(str(e))
        st.stop()
    st.session_state["file_key"] = key
    st.session_state["compact"] = compact
    st.session_state["stats"] = stats
    st.session_state["path"] = []

compact = st.session_state["compact"]
stats = st.session_state["stats"]
del file_bytes
vol = stats["vol"] or src_name

# ---------------- header ----------------
c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
c1.subheader(vol)
c2.metric("Files", f"{stats['files']:,}")
c3.metric("Folders", f"{stats['dirs']:,}")
c4.metric("Total size", up.fmt_size(stats["size"]))

ticket = re.match(r"^(\d{4,6})\b", src_name) or re.match(r"^(\d{4,6})\b", uploaded.name)
ticket_label = ticket.group(1) if ticket else src_name
out_name = (ticket.group(1) + "-" if ticket else "") + "File-Listing-compact.html"

dl_col, slack_col = st.columns([2, 2])
with dl_col:
    st.download_button(
        f"⬇️ Download compact viewer ({len(compact) / 1e6:.0f} MB standalone HTML for the customer)",
        data=compact,
        file_name=out_name,
        mime="text/html",
        type="primary",
    )
with slack_col:
    if SLACK_BOT_TOKEN:
        if st.button("💬 Upload to Slack File Listing Channel", type="primary"):
            st.session_state["show_slack_password"] = True
            st.session_state["slack_uploaded"] = False
    else:
        st.caption("Slack upload unavailable — add SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, "
                   "and SLACK_UPLOAD_PASSWORD to the app's secrets.")

if st.session_state.get("show_slack_password"):
    st.markdown("#### 🔐 Enter Password to Upload to Slack")
    pw_col, _ = st.columns([2, 3])
    with pw_col:
        slack_pw = st.text_input(
            "Password",
            type="password",
            key="slack_password_input",
            placeholder="Enter password to upload",
        )
        b1, b2 = st.columns(2)
        with b1:
            if st.button("✅ Confirm Upload", type="primary", width="stretch"):
                if slack_pw == SLACK_UPLOAD_PASSWORD:
                    import io
                    import zipfile as _zf
                    zip_buf = io.BytesIO()
                    with _zf.ZipFile(zip_buf, "w", _zf.ZIP_DEFLATED) as z:
                        z.writestr(out_name, compact)
                    zip_name = out_name.replace(".html", ".zip")
                    with st.spinner("Uploading to Slack..."):
                        ok, msg = upload_to_slack(
                            zip_buf.getvalue(), ticket_label,
                            SLACK_CHANNEL_ID, SLACK_BOT_TOKEN, zip_name,
                        )
                    if ok:
                        st.success(f"✅ {msg}")
                        st.balloons()
                        st.session_state["show_slack_password"] = False
                    else:
                        st.error(f"❌ {msg}")
                else:
                    st.error("❌ Incorrect password")
        with b2:
            if st.button("❌ Cancel", width="stretch"):
                st.session_state["show_slack_password"] = False
                st.rerun()

preview = st.toggle(
    "Preview in app (browse & search)",
    value=False,
    help="Loads the whole listing into memory — leave off if you only need the download.",
)
if not preview:
    st.stop()

tree = load_tree(key, compact)

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
