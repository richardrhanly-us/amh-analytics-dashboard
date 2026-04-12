import streamlit as st


def apply_page_chrome():
    st.markdown("""
    <style>
        [data-testid="stSidebarNav"] {
            display: none;
        }
    </style>
    """, unsafe_allow_html=True)

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


def render_app_header(library_name, branch_name, system_name):
    header_left, header_right = st.columns([12, 1])

    with header_left:
        st.caption("Hanly Analytics")
        st.markdown('<div class="sortview-title">SORTVIEW</div>', unsafe_allow_html=True)
        st.markdown(
            f"<div style='color:#6b7280; font-size:0.95rem; margin-bottom:10px;'>"
            f"{library_name} • {branch_name} • {system_name}"
            f"</div>",
            unsafe_allow_html=True
        )

    with header_right:
        if st.button("⚙️", help="Admin Settings"):
            st.switch_page("pages/1_admin_settings.py")
