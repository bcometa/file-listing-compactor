# File Listing Converter & Browser

Streamlit app for $300 Data Recovery. Upload a UFS Explorer file-listing HTML report (including ones too large to open in any browser), then:

- Browse the folder tree with per-folder total size and file counts
- Sort folders by size, file count, or name
- Search all file and folder names
- Download a compact standalone HTML viewer (roughly 85% smaller) to send to the customer

## Files

- `app.py` — the Streamlit app
- `ufs_parser.py` — parses UFS Explorer reports, builds the tree, generates the compact HTML
- `viewer_template.html` — template for the downloadable customer-facing viewer
- `.streamlit/config.toml` — raises the upload limit to 400 MB
- `_testwrap.py` — test helper only, not needed for deployment (safe to delete)

## Run locally

```
pip install -r requirements.txt
streamlit run app.py
```

## Deploy to Streamlit Community Cloud

1. Push this folder to a GitHub repo (private repos work).
2. Go to https://share.streamlit.io and sign in with GitHub.
3. Click "Create app" → pick the repo, branch, and `app.py`.
4. Deploy. The app gets a public URL.

## Password

The app is gated behind a password entry page. The password lives only in Streamlit secrets — it is not in the source code, so the repo is safe to make public. The app refuses to start until a secret is set.

On Community Cloud: app → Settings → Secrets → paste:

```
password = "11390"
```

Locally: create `.streamlit/secrets.toml` in this folder with that same line (and don't commit it — see `.gitignore`).

Notes for Cloud:

- Anyone with the URL can reach the password page, but not the app itself. For stronger protection you can additionally restrict viewers to specific email addresses in the app settings on share.streamlit.io.
- Uploads pass through Streamlit's servers and live in the app's memory while in use; nothing is written to disk permanently.
- Community Cloud gives limited RAM. An 83 MB report parses fine; if a much larger report ever gets killed for memory, run the app locally for that one.
