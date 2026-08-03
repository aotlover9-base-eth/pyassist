"""Dark minimal theme.

A restrained palette: near-black background, one accent, generous whitespace
and no borders where a little padding will do. Colours are defined once here as
constants and referenced from the CSS so the two never drift.
"""

from __future__ import annotations

# ----------------------------------------------------------------------
# palette
# ----------------------------------------------------------------------
BG = "#0d0f12"
BG_ALT = "#12151a"
BG_RAISED = "#171b21"
BORDER = "#1f242c"
TEXT = "#dfe3e8"
TEXT_DIM = "#7d8590"
TEXT_FAINT = "#565d67"
ACCENT = "#4c8dff"
ACCENT_DIM = "#26456f"
SUCCESS = "#3fb950"
WARNING = "#d29922"
ERROR = "#f85149"

CSS = f"""
Screen {{
    background: {BG};
    color: {TEXT};
}}

Header {{
    background: {BG};
    color: {TEXT_DIM};
    height: 1;
}}

Footer {{
    background: {BG};
    color: {TEXT_FAINT};
}}

Footer > .footer--key {{
    background: {BG};
    color: {ACCENT};
}}

/* ---------------------------------------------------------------- layout */
#body {{
    height: 1fr;
}}

#sidebar {{
    width: 32;
    background: {BG_ALT};
    border-right: solid {BORDER};
    padding: 1 1 0 2;
}}

#sidebar.hidden {{
    display: none;
}}

#sidebar-title {{
    color: {TEXT_FAINT};
    text-style: bold;
    padding-bottom: 1;
}}

#conversation-list {{
    background: transparent;
    border: none;
    height: 1fr;
}}

#conversation-list > ListItem {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 0 1;
}}

#conversation-list > ListItem.--highlight {{
    background: {ACCENT_DIM};
    color: {TEXT};
}}

#conversation-list:focus > ListItem.--highlight {{
    background: {ACCENT_DIM};
    color: {TEXT};
}}

#chat-pane {{
    width: 1fr;
}}

#transcript {{
    height: 1fr;
    padding: 1 3;
    background: {BG};
    scrollbar-size-vertical: 1;
    scrollbar-background: {BG};
    scrollbar-color: {BORDER};
    scrollbar-color-hover: {TEXT_FAINT};
}}

/* -------------------------------------------------------------- messages */
MessageBubble {{
    height: auto;
    margin-bottom: 1;
}}

MessageBubble .role {{
    text-style: bold;
}}

MessageBubble.user .role {{
    color: {ACCENT};
}}

MessageBubble.assistant .role {{
    color: {SUCCESS};
}}

MessageBubble .content {{
    padding-left: 0;
    color: {TEXT};
}}

MessageBubble.user .content {{
    color: {TEXT};
}}

ReasoningPanel {{
    height: auto;
    color: {TEXT_FAINT};
    text-style: italic;
    padding: 0 0 1 2;
    border-left: solid {BORDER};
    margin-bottom: 1;
}}

ToolCallPanel {{
    height: auto;
    background: {BG_RAISED};
    border-left: solid {ACCENT_DIM};
    padding: 0 2;
    margin-bottom: 1;
    color: {TEXT_DIM};
}}

ToolCallPanel.ok {{
    border-left: solid {SUCCESS};
}}

ToolCallPanel.failed {{
    border-left: solid {ERROR};
}}

NoticePanel {{
    height: auto;
    padding: 0 2;
    margin-bottom: 1;
    border-left: solid {WARNING};
    color: {WARNING};
    background: {BG_RAISED};
}}

NoticePanel.error {{
    border-left: solid {ERROR};
    color: {ERROR};
}}

WelcomePanel {{
    height: auto;
    padding: 2 0;
    color: {TEXT_DIM};
}}

/* ------------------------------------------------------------- composer */
#composer-area {{
    height: auto;
    max-height: 12;
    background: {BG_ALT};
    border-top: solid {BORDER};
    padding: 0 2;
}}

#attachment-bar {{
    height: auto;
    color: {TEXT_FAINT};
    padding: 0 1;
}}

#attachment-bar.hidden {{
    display: none;
}}

Composer {{
    height: auto;
    max-height: 8;
    background: {BG_ALT};
    border: none;
    padding: 1 1;
    color: {TEXT};
}}

Composer:focus {{
    border: none;
}}

#status-bar {{
    height: 1;
    padding: 0 2;
    color: {TEXT_FAINT};
    background: {BG};
}}

/* -------------------------------------------------------------- screens */
SettingsScreen, SearchScreen, FilePickerScreen, ApprovalScreen, HelpScreen {{
    align: center middle;
}}

#dialog {{
    width: 82;
    max-width: 96%;
    height: auto;
    max-height: 90%;
    background: {BG_ALT};
    border: solid {BORDER};
    padding: 1 2;
}}

#dialog.wide {{
    width: 100;
}}

#dialog-title {{
    text-style: bold;
    color: {TEXT};
    padding-bottom: 1;
}}

#dialog-hint {{
    color: {TEXT_FAINT};
    padding-bottom: 1;
}}

.field-label {{
    color: {TEXT_DIM};
    padding-top: 1;
}}

Input {{
    background: {BG};
    border: solid {BORDER};
    color: {TEXT};
    padding: 0 1;
}}

Input:focus {{
    border: solid {ACCENT};
}}

TextArea {{
    background: {BG};
    border: solid {BORDER};
    color: {TEXT};
    height: 6;
}}

TextArea:focus {{
    border: solid {ACCENT};
}}

Checkbox {{
    background: transparent;
    border: none;
    color: {TEXT_DIM};
    padding: 0;
}}

Checkbox:focus {{
    color: {TEXT};
}}

#settings-scroll {{
    height: auto;
    max-height: 24;
}}

#button-row {{
    height: auto;
    align-horizontal: right;
    padding-top: 1;
}}

Button {{
    background: {BG_RAISED};
    color: {TEXT};
    border: none;
    min-width: 12;
    margin-left: 1;
}}

Button:focus {{
    text-style: bold;
}}

Button.-primary {{
    background: {ACCENT_DIM};
    color: {TEXT};
}}

Button.-error {{
    background: #4a1f1f;
    color: {ERROR};
}}

#approval-detail {{
    height: auto;
    max-height: 16;
    background: {BG};
    border: solid {BORDER};
    padding: 1;
    color: {TEXT_DIM};
    margin-bottom: 1;
}}

#results-list {{
    height: auto;
    max-height: 20;
    background: transparent;
    border: none;
}}

#results-list > ListItem {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 0 1;
}}

#results-list > ListItem.--highlight {{
    background: {ACCENT_DIM};
    color: {TEXT};
}}

DirectoryTree {{
    height: 16;
    background: {BG};
    border: solid {BORDER};
    color: {TEXT_DIM};
    padding: 0 1;
}}

DirectoryTree:focus {{
    border: solid {ACCENT};
}}

#help-body {{
    height: auto;
    max-height: 26;
    color: {TEXT_DIM};
}}

LoadingIndicator {{
    background: transparent;
    color: {ACCENT};
}}

Toast {{
    background: {BG_RAISED};
    color: {TEXT};
    border: solid {BORDER};
}}
"""
