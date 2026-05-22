#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         app_ui_service.py
#
#  Description: Provides shared Streamlit page styling and header
#               rendering for the SortView dashboard. This file hides
#               Streamlit's default sidebar navigation, applies custom
#               SortView branding styles, and renders the main dashboard
#               header with optional admin navigation.
#
#***************************************************************

import streamlit as st


#***************************************************************
#
#  Function:     apply_page_chrome
#
#  Description: Applies custom page-level styling for the SortView
#               dashboard. This hides Streamlit's default sidebar
#               navigation and adds custom CSS for the SortView title
#               and download buttons.
#
#  Parameters:  None
#
#  Returns:     None
#
#***************************************************************

def apply_page_chrome():
    # Hide Streamlit's default sidebar page navigation.
    st.markdown("""
    <style>
        [data-testid="stSidebarNav"] {
            display: none;
        }
    </style>
    """, unsafe_allow_html=True)

    # Apply SortView branding styles and custom download button styling.
    st.markdown("""
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;800&display=swap" rel="stylesheet">

    <style>
    .sortview-title {
        font-family: 'Orbitron', sans-serif;
        font-size: 52px;
        font-weight: 800;
        letter-spacing: 3px;
        background: linear-gradient(90deg, #60a5fa, #a78bfa, #34d399);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        text-shadow:
            0 0 6px rgba(96, 165, 250, 0.4),
            0 0 12px rgba(167, 139, 250, 0.25);
        margin-bottom: -4px;
    }

    div.stDownloadButton > button {
        background: linear-gradient(135deg, #2563eb, #1d4ed8);
        color: white;
        border-radius: 10px;
        padding: 0.7em 1.4em;
        font-weight: 600;
        border: none;
        box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
    }

    div.stDownloadButton > button:hover {
        background: linear-gradient(135deg, #1d4ed8, #1e3a8a);
        transform: translateY(-1px);
    }
    </style>
    """, unsafe_allow_html=True)


#***************************************************************
#
#  Function:     render_app_header
#
#  Description: Renders the main SortView dashboard header. The header
#               includes the Hanly Analytics caption, SortView title,
#               selected library/branch/system details, and an optional
#               admin settings button.
#
#  Parameters:  library_name - Display name of the selected library.
#               branch_name - Display name of the selected branch.
#               system_name - Display name of the AMH or library system.
#               show_admin_button - Boolean flag that controls whether
#                                   the admin settings button is shown.
#
#  Returns:     None
#
#***************************************************************

def render_app_header(library_name, branch_name, system_name, show_admin_button=True):
    # Split the header into a large branding area and a small admin button area.
    header_left, header_right = st.columns([12, 1])

    # Render the main title and selected system information.
    with header_left:
        st.caption("Hanly Analytics")
        st.markdown('<div class="sortview-title">SORTVIEW</div>', unsafe_allow_html=True)
        st.markdown(
            f"<div style='color:#6b7280; font-size:0.95rem; margin-bottom:10px;'>"
            f"{library_name} • {branch_name} • {system_name}"
            f"</div>",
            unsafe_allow_html=True
        )

    # Render the admin settings shortcut when the current user has access.
    with header_right:
        if show_admin_button:
            if st.button("⚙️", help="Admin Settings"):
                st.switch_page("pages/1_admin_settings.py")
