# GitHub Manager (Flask)

A small Flask web application that lets you manage GitHub repositories using a
**Personal Access Token (PAT)** — no OAuth app required.

## Features

- 🔐 **Login** with your GitHub **username + Personal Access Token**
- 📦 **Create** new repositories (public / private, optional README)
- 📂 **Browse** repository contents (folders & files, breadcrumb navigation)
- ⬆️ **Upload** one or more files into any folder
- 🗑️ **Delete** files
- 🗜️ **Upload a ZIP archive** → it is automatically **unzipped** and **pushed**
  to the repository in a single commit (uses the Git Data API, so it is fast
  even for hundreds of files)

## Requirements

- Python 3.9+
- A GitHub Personal Access Token with the **`repo`** scope
  (add **`delete_repo`** if you also want to delete repositories).

## Setup

```bash
pip install -r requirements.txt
export SECRET_KEY="some-random-secret"   # optional but recommended
python app.py
```

Then open <http://localhost:5000>.

## Creating a token

1. Go to <https://github.com/settings/tokens/new?scopes=repo>
2. Give it a name, tick the **repo** scope, and generate.
3. Copy the token (`ghp_...`) and paste it on the login page.

## How the ZIP upload works

1. The ZIP is read in memory and extracted with Python's `zipfile`.
2. Directories and OS metadata (`__MACOSX`, `.DS_Store`) are skipped.
3. If every entry shares one top-level folder, it can be stripped
   (checkbox on by default).
4. All files are pushed with the **Git Data API**:
   `blobs → tree → commit → update ref`, producing one clean commit.

## Security notes

- The token is kept only in the Flask **session** (a signed cookie) and is
  never written to disk.
- Always run behind HTTPS in production and set a strong `SECRET_KEY`.
- The app never logs the token.

## Project structure

```
.
├── app.py               # Flask app + GitHub REST helpers
├── requirements.txt
├── README.md
├── static/
│   └── css/style.css
└── templates/
    ├── base.html
    ├── login.html
    ├── dashboard.html
    └── repo.html
```
