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

    # Compact the top of the page. Streamlit's own default
    # `.block-container` padding-top is 6rem (reserved so page content
    # never sits under the fixed header/toolbar strip) -- see the bundled
    # `StyledAppViewBlockContainer` styles. That is far more clearance than
    # this app's own header actually needs, and was the single largest
    # contributor to the "Live Today" landing page's excessive whitespace
    # before any KPI cards are visible. 3rem keeps a comfortable margin
    # above the toolbar while cutting the blank area roughly in half.
    st.markdown("""
    <style>
    .block-container {
        padding-top: 3rem;
    }
    </style>
    """, unsafe_allow_html=True)

    # Apply small, targeted spacing adjustments to the dashboard navigation
    # and Live Today control row. These keyed containers are scoped so the
    # rules do not affect other dashboard views or KPI content.
    st.markdown("""
    <style>
    .st-key-sv_nav_row {
        margin-bottom: 0.75rem;
    }

    .st-key-sv_live_controls_row {
        margin-bottom: 0.5rem;
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
    # Split the header into a large branding area and an admin button area.
    # Widened from the original [12, 1] (sized for an icon-only "⚙️"
    # button) to give "⚙️ Admin Settings" 's real text room without
    # wrapping -- see this button's own comment below for why it has
    # visible text now (WCAG 4.1.2 Name, Role, Value / 2.4.6 Headings and
    # Labels).
    header_left, header_right = st.columns([10, 3])

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
    # Visible text, not an icon-only "⚙️" -- a bare emoji has no reliable
    # accessible name (Streamlit's help= renders as a separate hover
    # tooltip, never the button's own name), so the label itself must
    # carry the meaning (WCAG 4.1.2 Name, Role, Value).
    with header_right:
        if show_admin_button and st.button("⚙️ Admin Settings", help="Admin Settings"):
            st.switch_page("pages/1_admin_settings.py")
