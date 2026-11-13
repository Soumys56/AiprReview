
# import sys
# import asyncio
# from pathlib import Path
# import json
# PROJECT_ROOT = Path(__file__).resolve().parent.parent

# if str(PROJECT_ROOT) not in sys.path:
#     sys.path.insert(0, str(PROJECT_ROOT))

# import streamlit as st

# from config.setting import settings
# from components.github_fetchpr_client import githubFetch
# from components.github_fetchpr_client import GitHubPRDiff
# from utilitis.utitis import separate_before_after


# # -----------------------------
# # GitHub API functions
# # -----------------------------

# async def get_all_prs():
#     github_req = githubFetch(
#         settings.REPO,
#         settings.OWNER
#     )

#     return await github_req.github_fetch_all_pr()


# async def get_pr_changes(pr_number):
#     github_req = GitHubPRDiff(
#         settings.REPO,
#         settings.OWNER,
#         pr_number=pr_number
#     )
#     return await github_req.github_fetch_pr_diff()


# # -----------------------------
# # Streamlit Configuration
# # -----------------------------

# st.set_page_config(
#     page_title="AI PR REVIEW",
#     page_icon="📊",
#     layout="wide",
#     initial_sidebar_state="expanded"
# )

# st.title("🚀 AI Pull Request Review")
# st.caption(f"Repository: {settings.OWNER}/{settings.REPO}")


# # -----------------------------
# # Fetch All Pull Requests
# # -----------------------------

# try:
#     with st.spinner("Fetching Pull Requests..."):
#         prs = asyncio.run(get_all_prs())

#     if not prs:
#         st.warning("No Pull Requests found.")
#         st.stop()

# except Exception as e:
#     st.error(f"Failed to fetch Pull Requests: {e}")
#     st.stop()


# # -----------------------------
# # PR Dropdown
# # -----------------------------

# pr_options = {
#     f"PR #{pr['number']} - {pr['title']}": pr['number']
#     for pr in prs
# }

# selected_pr = st.selectbox(
#     "Choose your Pull Request:",
#     options=list(pr_options.keys())
# )

# selected_pr_number = pr_options[selected_pr]

# st.info(f"Selected Pull Request: {selected_pr}")


# # -----------------------------
# # Fetch Selected PR Changes
# # -----------------------------

# try:
#     with st.spinner("Fetching changed files..."):
#         changed_files = asyncio.run(
#             get_pr_changes(selected_pr_number)
#         )
#         print(changed_files)

#     if not changed_files:
#         st.warning("No changed files found.")
#         st.stop()

# except Exception as e:
#     st.error(f"Failed to fetch PR changes: {e}")
#     st.stop()

# # -----------------------------
# # Display Before / After
# # -----------------------------

# st.subheader("📂 Changed Files")

# # Normalize response into a list
# if isinstance(changed_files, dict):
#     changed_files = [changed_files]

# for file in changed_files:

#     filename = file.get("filename", "Unknown file")

#     st.markdown(f"### 📄 {filename}")

#     st.caption(
#         f"Status: {file.get('status', 'unknown')} | "
#         f"Additions: +{file.get('additions', 0)} | "
#         f"Deletions: -{file.get('deletions', 0)}"
#     )

#     patch = file.get("patch", "")

#     if file.get("before_changes") is not None:
#         comparison = file

#     elif patch:
#         comparison = separate_before_after(
#             patch,
#             filename
#         )

#     else:
#         st.warning("Patch unavailable for this file.")
#         continue

#     before = comparison.get("before_changes", "")
#     after = comparison.get("after_changes", "")

#     col1, col2 = st.columns(2)

#     with col1:
#         st.markdown("#### 🔴 Before Changes")
#         st.code(before or "No previous content")

#     with col2:
#         st.markdown("#### 🟢 After Changes")
#         st.code(after or "No updated content")

#     st.divider()
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st
from config.setting import settings
from components.github_fetchpr_client import githubFetch, GitHubPRDiff
from utilitis.utitis import separate_before_after
from components.index_store import IndexStore
from components.repo_mirror import RepoMirror
from service.pipeline import Stage2Pipeline

st.set_page_config(
    page_title="AI PR REVIEW",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.title("🚀 AI Pull Request Review")
st.caption(f"Repository: {settings.OWNER}/{settings.REPO}")


@st.cache_data(ttl=120, show_spinner="Fetching pull requests...")
def get_all_prs(state: str):
    return asyncio.run(
        githubFetch(settings.REPO, settings.OWNER).github_fetch_all_pr()
    )


@st.cache_data(ttl=120, show_spinner="Fetching changed files...")
def get_pr_changes(pr_number: int):
    return asyncio.run(
        GitHubPRDiff(settings.REPO, settings.OWNER, pr_number).github_fetch_pr_diff()
    )


@st.cache_resource
def get_pipeline():
    cache_dir = getattr(settings, "REPO_CACHE_DIR", ".cache/mirrors")
    git_host = getattr(settings, "GIT_HOST", "github.com")
    store = IndexStore(settings.INDEX_DB_PATH)
    mirror = RepoMirror(
        settings.OWNER,
        settings.REPO,
        settings.GITHUB_TOKEN,
        cache_dir,
        git_host,
    )
    return Stage2Pipeline(store, mirror, settings.OWNER, settings.REPO)


# -----------------------------
# PR Filter & Selector
# -----------------------------
state = st.radio("PR state", ["open"], horizontal=True)

try:
    prs = get_all_prs(state)
except Exception as e:
    st.error(f"Failed to fetch Pull Requests: {e}")
    st.stop()

if not prs:
    st.warning("No Pull Requests found.")
    st.stop()

pr_options = {f"PR #{p['number']} - {p['title']}": p["number"] for p in prs}
selected = st.selectbox("Choose your Pull Request:", list(pr_options))
pr_number = pr_options[selected]

# -----------------------------
# Tabs: Changed Files & Stage 2 Context
# -----------------------------
tab_diff, tab_stage2 = st.tabs(["📂 Changed files", "🧠 Stage 2 context"])

with tab_diff:
    try:
        changed_files = get_pr_changes(pr_number)
    except Exception as e:
        st.error(f"Failed to fetch PR changes: {e}")
        st.stop()

    if not changed_files:
        st.warning("No changed files found.")
        st.stop()

    if isinstance(changed_files, dict):
        changed_files = [changed_files]

    for file in changed_files:
        filename = file.get("filename", "Unknown file")
        st.markdown(f"### 📄 {filename}")
        st.caption(
            f"Status: {file.get('status', 'unknown')} | "
            f"+{file.get('additions', 0)} | -{file.get('deletions', 0)}"
        )

        patch = file.get("patch")
        if file.get("before_changes") is not None:
            cmp = file
        elif patch:
            cmp = separate_before_after(patch, filename)
        else:
            st.warning("Patch unavailable (binary or too large).")
            continue

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("#### 🔴 Before Changes")
            st.code(cmp.get("before_changes") or "No previous content")
        with c2:
            st.markdown("#### 🟢 After Changes")
            st.code(cmp.get("after_changes") or "No updated content")
        st.divider()

with tab_stage2:
    if st.button("Run Stage 2 (impact analysis)", type="primary"):
        with st.spinner("Indexing base commit (first run is slow) and analysing PR..."):
            try:
                result = asyncio.run(get_pipeline().run(pr_number))
            except Exception as e:
                st.error(f"Stage 2 failed: {e}")
                st.stop()
        st.session_state[f"stage2_{pr_number}"] = result

    result = st.session_state.get(f"stage2_{pr_number}")
    if result:
        if result.get("warnings"):
            for w in result["warnings"]:
                st.warning(w)

        if not result["chunks"]:
            st.info("No semantic or file-window chunks generated for this PR.")

        blast_icons = {"high": "🔴", "medium": "🟠", "low": "🟡", "none": "🟢"}

        for ch in result["chunks"]:
            if ch["chunk_type"] != "symbol":
                st.info(f"{ch['file']}: {ch['reason']} (sent as raw patch)")
                continue

            imp = ch["impact"]
            icon = blast_icons.get(imp["blast_radius"], "⚪")

            with st.expander(f"{icon} {ch['symbol']} · {ch['change_type']} · {ch['file']}"):
                if ch["old_signature"] != ch["new_signature"]:
                    st.write(f"`{ch['old_signature']}` → `{ch['new_signature']}`")

                for c in imp["direct_callers"]:
                    st.write(
                        f"- {c['status']} · `{c['symbol']}` ({c['file']}:{c['line']}) "
                        f"{'updated' if c['updated_in_pr'] else 'NOT in PR'} {c.get('detail', '')}"
                    )

                st.code(ch["symbol_diff"] or ch["code"], language="diff")

        st.download_button(
            "Download chunks JSON",
            json.dumps(result, indent=2),
            file_name=f"pr_{pr_number}_stage2.json",
            mime="application/json",
        )