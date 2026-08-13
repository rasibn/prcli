"""Keybinding config: (mode, key) -> command name.

Modes (vim-style):
  NORMAL   list focused — all keybinds below are live
  COMMAND  the ':' palette or a tag picker is open — keys type into the
           input, no NORMAL keybinds fire (enter selects, esc cancels)

Edit this table to rebind keys. Command names must exist in commands.REGISTRY.
A key entry like "j,down" binds several keys to the same command.
"""

NORMAL = "normal"
COMMAND = "command"

# Default priority order, from highest to lowest. PRs without a priority label
# come last. PRs within a priority group are ordered by number, newest first.
SORT_LABEL_ORDER = [
    "P0 - Immediate",
    "P1 - High",
    "P3 - Low",
    "P4 - Lowest",
    "ready to release",
    "size: small",
    "size: medium",
    "size: large",
]

# Optional shell commands available from the command palette. The selected
# PR's head branch replaces {branch}.
CUSTOM_COMMANDS = {
    # "checkout": "git checkout {branch}",
}
# Bind one directly with a custom: name, for example:
# (NORMAL, "X"): "custom:checkout",

KEYMAP = {
    # movement
    (NORMAL, "j,down"): "cursor_down",
    (NORMAL, "k,up"): "cursor_up",
    (NORMAL, "g"): "cursor_top",
    (NORMAL, "G"): "cursor_bottom",
    (NORMAL, "J"): "reorder_down",
    (NORMAL, "K"): "reorder_up",
    # labels / git
    (NORMAL, "p"): "set_priority",
    (NORMAL, "t"): "add_tag",
    (NORMAL, "T"): "remove_tag",
    (NORMAL, "r"): "refresh",
    (NORMAL, "d"): "toggle_draft",
    (NORMAL, "s"): "sync",
    (NORMAL, "b,u"): "rebase",
    (NORMAL, "o,enter"): "open_pr",
    (NORMAL, "c,y"): "copy_branch",
    (NORMAL, "C"): "copy_config_path",
    (NORMAL, "?"): "show_config",
    # app
    (NORMAL, "colon"): "palette",
    (NORMAL, "q"): "quit",
}
