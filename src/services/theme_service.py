def get_theme_palette(theme_base):
    if theme_base == "dark":
        return {
            "info_bg": "rgba(37, 99, 235, 0.14)",
            "info_border": "#3b82f6",
            "info_title": "#93c5fd",
            "info_text": "#dbeafe",
            "danger_bg": "rgba(220, 38, 38, 0.14)",
            "danger_border": "#ef4444",
            "danger_title": "#fca5a5",
            "danger_text": "#fee2e2",
        }

    return {
        "info_bg": "#eff6ff",
        "info_border": "#2563eb",
        "info_title": "#1d4ed8",
        "info_text": "#1e3a8a",
        "danger_bg": "#fef2f2",
        "danger_border": "#dc2626",
        "danger_title": "#991b1b",
        "danger_text": "#7f1d1d",
    }
