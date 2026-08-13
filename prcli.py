# /// script
# requires-python = ">=3.10"
# dependencies = ["textual>=0.80"]
# ///
"""PR CLI — vim-keybound list of my open PRs with label management.

Run:  uv run ~/work/pr-matrix/prcli.py [--repo owner/name]

Vim-style modes: NORMAL (keybinds live) and COMMAND (a picker/palette is open,
keys type into its input). Keybindings live in config.KEYMAP (mode+key →
command); the commands themselves are registered in commands.REGISTRY.
Press ':' for the command palette.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import commands
import config
import sync_prs

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option

SIZE_BADGE = {"size: small": "S", "size: medium": "M", "size: large": "L"}
ORDER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "order.json")

CHIP_STYLE = {
    **{priority: "bold red" for priority in sync_prs.PRIORITIES},
    sync_prs.L_QA_DONE: "bold green",
    sync_prs.L_QA_NEEDED: "bold blue",
    sync_prs.L_CONFLICTS: "bold blue",
    sync_prs.L_OUTDATED: "yellow",
    sync_prs.L_UNRESOLVED: "#ff8800",
}


def row_line(r: dict, selected: bool) -> Text:
    t = Text()
    t.append(f"#{r['number']:<5}", "bold cyan")
    t.append(f" {SIZE_BADGE.get(r['size'], '?')} ", "bold")
    t.append(r["title"], "white")

    # live state (from GitHub API, independent of labels)
    if r["draft"]:
        t.append(" draft", "dim italic")
    if r["conflicts"]:
        t.append(" ⚠", "bold red")
    if r["behind_by"]:
        t.append(f" ↓{r['behind_by']}", "bold yellow")
    if r["unresolved_comments"]:
        t.append(f" 💬{r['unresolved_comments']}", "bold #ff8800")
    if r.get("busy"):
        t.append(f" ({r['busy']})", "bold blue")

    # only the labels this tool manages (size shown via the badge already)
    for label in r["labels"]:
        if label in CHIP_STYLE:
            t.append(f" [{label}]", CHIP_STYLE[label])

    if selected:
        t.stylize("reverse")
    return t


class Picker(ModalScreen):
    """Type-to-filter dropdown; enter selects, esc cancels.

    Options are strings, or (id, label) pairs when the displayed label should
    differ from the value returned on selection.
    """

    CSS = """
    Picker { align: center middle; }
    #dialog { width: 60; max-height: 20; border: round $primary; background: $surface; }
    #dialog Input { border: none; }
    #dialog OptionList { border: none; max-height: 14; }
    """

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, placeholder: str, options: list):
        super().__init__()
        self.placeholder = placeholder
        self.all_options = [(o, o) if isinstance(o, str) else o for o in options]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Input(placeholder=self.placeholder)
            yield OptionList(*[Option(label, id=oid) for oid, label in self.all_options])

    def on_mount(self):
        self.query_one(Input).focus()

    def filtered(self) -> list:
        q = self.query_one(Input).value.lower()
        return [(oid, label) for oid, label in self.all_options if q in label.lower()]

    def on_input_changed(self, _):
        ol = self.query_one(OptionList)
        ol.clear_options()
        ol.add_options([Option(label, id=oid) for oid, label in self.filtered()])
        if ol.option_count:
            ol.highlighted = 0

    def on_input_submitted(self, _):
        opts = self.filtered()
        ol = self.query_one(OptionList)
        pick = None
        if ol.highlighted is not None and ol.option_count:
            pick = ol.get_option_at_index(ol.highlighted).id
        elif opts:
            pick = opts[0][0]
        self.dismiss(pick)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(event.option.id)

    DOWN_KEYS = ("down", "ctrl+j", "ctrl+n")
    UP_KEYS = ("up", "ctrl+k", "ctrl+p")

    def on_key(self, event):
        # fzf-style navigation while typing in the filter input
        if event.key in (*self.DOWN_KEYS, *self.UP_KEYS) and self.query_one(Input).has_focus:
            ol = self.query_one(OptionList)
            if ol.option_count:
                cur = ol.highlighted or 0
                ol.highlighted = (cur + (1 if event.key in self.DOWN_KEYS else -1)) % ol.option_count
            event.stop()
            event.prevent_default()

    def action_cancel(self):
        self.dismiss(None)


class ConfigView(ModalScreen):
    CSS = """
    ConfigView { align: center middle; }
    #config { width: 90%; height: 90%; padding: 1 2; border: round $primary; background: $surface; overflow-y: auto; }
    """

    BINDINGS = [Binding("escape", "close", "close", show=False)]

    def compose(self) -> ComposeResult:
        lines = ["KEYBINDS", ""]
        for (mode, key), name in config.KEYMAP.items():
            if mode == config.NORMAL:
                lines.append(f"{key:<12} {commands.help_for(name)}")

        lines.extend(["", "CUSTOM COMMANDS", ""])
        custom_commands = getattr(config, "CUSTOM_COMMANDS", {})
        if custom_commands:
            lines.extend(f"{name:<12} {template}" for name, template in custom_commands.items())
        else:
            lines.append("none configured")

        yield Static("\n".join(lines), id="config")

    def action_close(self):
        self.dismiss()


class PRCli(App):
    TITLE = "PR CLI"
    ENABLE_COMMAND_PALETTE = False  # ':' opens ours instead of ctrl+p

    CSS = """
    #list { height: 1fr; padding: 0 1; overflow-y: auto; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding(key, f"cmd('{name}')", commands.help_for(name),
                show=commands.palette_for(name))
        for (mode, key), name in config.KEYMAP.items()
        if mode == config.NORMAL
    ]

    def __init__(self, repo: str):
        super().__init__()
        self.repo = repo
        self.rows: list[dict] = []
        self.sel = 0
        self.repo_labels: list[str] = []
        self.ui_mode = config.NORMAL

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="list")
        yield Static(id="status")
        yield Footer()

    def on_mount(self):
        self.sub_title = self.repo
        self.set_status("loading PRs…")
        self.load(dry_run=True)

    def set_status(self, msg: str):
        self.query_one("#status", Static).update(msg)

    # ---------------- command dispatch (see config.py / commands.py)

    def action_cmd(self, name: str):
        if self.ui_mode != config.NORMAL:
            return
        commands.run(self, name)

    def open_modal(self, screen: ModalScreen, callback):
        self.ui_mode = config.COMMAND

        def done(result):
            self.ui_mode = config.NORMAL
            callback(result)

        self.push_screen(screen, done)

    def pick(self, placeholder: str, options: list, callback):
        self.open_modal(Picker(placeholder, options), callback)

    def open_palette(self):
        opts = [(c.name, f"{c.name} — {c.help}") for c in commands.REGISTRY.values() if c.palette]
        self.pick("command", opts, lambda name: name and commands.REGISTRY[name].run(self))

    def show_config(self):
        self.push_screen(ConfigView())

    # ---------------- data

    @work(thread=True, exclusive=True, group="load")
    def load(self, dry_run: bool):
        verb = "loaded" if dry_run else "synced"
        try:
            if dry_run:
                sync_prs.ensure_labels(self.repo, sync_prs.KNOWN_LABELS)
            rows = sync_prs.sync(self.repo, dry_run=dry_run)
            if not self.repo_labels:
                out = sync_prs.gh("label", "list", "--repo", self.repo, "--limit", "200", "--json", "name")
                self.repo_labels = sorted(l["name"] for l in json.loads(out))
        except Exception as e:
            self.call_from_thread(self.set_status, f"error: {e}")
            return
        changed = sum(1 for r in rows if r["labels_added"] or r["labels_removed"])
        msg = f"{verb} {len(rows)} PRs"
        if not dry_run:
            msg += f" — state labels updated on {changed}"
        self.call_from_thread(self.apply_rows, rows, msg)

    def apply_rows(self, rows: list[dict], msg: str):
        keep = self.rows[self.sel]["number"] if self.rows and self.sel < len(self.rows) else None
        self.rows = rows
        self.resort_rows(keep)
        self.set_status(msg)

    def resort_rows(self, keep=None):
        if keep is None:
            keep = self.rows[self.sel]["number"] if self.rows and self.sel < len(self.rows) else None
        order = self.load_order()
        if order:
            rank = {n: i for i, n in enumerate(order)}
            self.rows.sort(key=lambda r: rank.get(r["number"], len(order)))
        else:
            label_rank = {label: i for i, label in enumerate(config.SORT_LABEL_ORDER)}

            def default_sort_key(row):
                priorities = tuple(
                    sorted(label_rank[label] for label in row["labels"] if label in label_rank)
                )
                return priorities or (len(label_rank),), -row["number"]

            self.rows.sort(key=default_sort_key)
        self.sel = next((i for i, r in enumerate(self.rows) if r["number"] == keep), 0) if keep else 0
        self.render_list()

    def render_list(self):
        body = Text()
        for i, r in enumerate(self.rows):
            body.append_text(row_line(r, i == self.sel))
            body.append("\n")
        if not self.rows:
            body.append("no open PRs", "dim")
        self.query_one("#list", Static).update(body)

    def current(self) -> dict | None:
        return self.rows[self.sel] if self.rows else None

    # ---------------- manual ordering (persisted locally)

    @staticmethod
    def load_order() -> list[int]:
        try:
            with open(ORDER_FILE) as f:
                return json.load(f)
        except Exception:
            return []

    def save_order(self):
        with open(ORDER_FILE, "w") as f:
            json.dump([r["number"] for r in self.rows], f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=sync_prs.DEFAULT_REPO)
    args = ap.parse_args()
    if not sys.stdout.isatty():
        print("prcli needs a terminal; use sync_prs.py for plain output", file=sys.stderr)
        sys.exit(1)
    PRCli(args.repo).run()


if __name__ == "__main__":
    main()
