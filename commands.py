"""All user-invokable commands, registered uniformly.

Keybinds (config.KEYMAP) and the ':' palette both dispatch through REGISTRY,
so a command added here is automatically available to both — bind it in
config.py and it shows up in the footer; palette=True lists it under ':'.

Each command receives the running PRCli app. Blocking gh calls run in worker
threads via app.run_worker so the UI stays responsive.
"""

import shlex
import subprocess
import webbrowser
import os
from dataclasses import dataclass
from typing import Callable

import config
import sync_prs
# labels owned by the sync script — not offered in the add/remove pickers
MANAGED = {
    sync_prs.L_CONFLICTS,
    sync_prs.L_OUTDATED,
    sync_prs.L_UNRESOLVED,
    *sync_prs.SIZES,
    *sync_prs.PRIORITIES,
}


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    run: Callable  # (app) -> None
    palette: bool = True  # listed in the ':' palette and shown in the footer


REGISTRY: dict[str, Command] = {}


def help_for(name: str) -> str:
    if name.startswith("custom:"):
        return f"custom command: {name.removeprefix('custom:')}"
    return REGISTRY[name].help


def palette_for(name: str) -> bool:
    return not name.startswith("custom:") and REGISTRY[name].palette


def run(app, name: str):
    if name.startswith("custom:"):
        run_custom(app, name.removeprefix("custom:"))
    else:
        REGISTRY[name].run(app)


def command(name: str, help: str, palette: bool = True):
    def deco(fn):
        REGISTRY[name] = Command(name, help, fn, palette)
        return fn

    return deco


# ---------------- movement


@command("cursor_down", "move down", palette=False)
def cursor_down(app):
    _move(app, 1)


@command("cursor_up", "move up", palette=False)
def cursor_up(app):
    _move(app, -1)


def _move(app, delta: int):
    if app.rows:
        app.sel = (app.sel + delta) % len(app.rows)
        app.render_list()


@command("cursor_top", "jump to top", palette=False)
def cursor_top(app):
    app.sel = 0
    app.render_list()


@command("cursor_bottom", "jump to bottom", palette=False)
def cursor_bottom(app):
    app.sel = max(0, len(app.rows) - 1)
    app.render_list()


@command("reorder_down", "move PR down (persisted)", palette=False)
def reorder_down(app):
    _reorder(app, 1)


@command("reorder_up", "move PR up (persisted)", palette=False)
def reorder_up(app):
    _reorder(app, -1)


def _reorder(app, delta: int):
    if not app.rows:
        return
    j = app.sel + delta
    if not 0 <= j < len(app.rows):
        return
    app.rows[app.sel], app.rows[j] = app.rows[j], app.rows[app.sel]
    app.sel = j
    app.save_order()
    app.render_list()


# ---------------- labels


def refresh_derived(r: dict):
    labels = set(r["labels"])
    r["qa_lane"] = (
        "ready to release"
        if sync_prs.L_QA_DONE in labels
        else "needs qa"
        if sync_prs.L_QA_NEEDED in labels
        else "untracked"
    )
    r["size"] = next((s for s in sync_prs.SIZES if s in labels), None)
    r["priority"] = next((p for p in sync_prs.PRIORITIES if p in labels), None)


def mutate(app, r: dict, add: list[str], remove: list[str]):
    r["labels"] = sorted((set(r["labels"]) | set(add)) - set(remove))
    refresh_derived(r)
    app.resort_rows()
    app.run_worker(lambda: _push_labels(app, r, add, remove), thread=True)


def _push_labels(app, r: dict, add: list[str], remove: list[str]):
    try:
        sync_prs.ensure_labels(app.repo, add)
        sync_prs.edit_labels(app.repo, r["number"], add, remove)
        app.call_from_thread(app.set_status, f"#{r['number']}: labels saved")
    except Exception as e:
        app.call_from_thread(app.set_status, f"#{r['number']}: label edit failed — {e}")


@command("set_priority", "set priority")
def set_priority(app):
    r = app.current()
    if not r:
        return

    def done(pick):
        if not pick:
            return
        mutate(app, r, [pick], [p for p in sync_prs.PRIORITIES if p != pick])

    app.pick(f"set priority for #{r['number']}", sync_prs.PRIORITIES, done)


@command("add_tag", "add tag", palette=False)
def add_tag(app):
    r = app.current()
    if not r:
        return
    options = [l for l in app.repo_labels if l not in MANAGED and l not in r["labels"]]

    def done(pick):
        if pick:
            mutate(app, r, [pick], [])

    app.pick(f"add tag to #{r['number']}", options, done)


@command("remove_tag", "remove tag", palette=False)
def remove_tag(app):
    r = app.current()
    if not r:
        return
    options = [l for l in r["labels"] if l not in MANAGED]
    if not options:
        app.set_status(
            f"#{r['number']}: no removable tags (sync-managed tags are protected)"
        )
        return

    def done(pick):
        if pick:
            mutate(app, r, [], [pick])

    app.pick(f"remove tag from #{r['number']}", options, done)


# ---------------- draft / sync / rebase / open


@command("toggle_draft", "toggle draft")
def toggle_draft(app):
    r = app.current()
    if not r:
        return
    to_draft = not r["draft"]
    r["busy"] = "toggling draft…"
    app.render_list()
    app.run_worker(lambda: _push_draft(app, r, to_draft), thread=True)


def _push_draft(app, r: dict, to_draft: bool):
    try:
        args = ["pr", "ready", str(r["number"]), "--repo", app.repo]
        if to_draft:
            args.append("--undo")
        sync_prs.gh(*args)
        r["draft"] = to_draft
        app.call_from_thread(
            app.set_status,
            f"#{r['number']}: {'converted to draft' if to_draft else 'marked ready for review'}",
        )
    except Exception as e:
        app.call_from_thread(
            app.set_status, f"#{r['number']}: draft toggle failed — {e}"
        )
    r["busy"] = ""
    app.call_from_thread(app.render_list)


@command("sync", "sync labels")
def sync(app):
    app.set_status("syncing state labels…")
    app.load(dry_run=False)


@command("refresh", "refresh PRs", palette=False)
def refresh(app):
    app.set_status("refreshing PRs…")
    app.load(dry_run=True)


@command("rebase", "rebase wth base")
def rebase(app):
    r = app.current()
    if not r:
        return
    if r["conflicts"]:
        app.set_status(
            f"#{r['number']}: has conflicts — resolve locally, rebase update would fail"
        )
        return
    if not r["behind_by"]:
        app.set_status(f"#{r['number']}: already up to date with {r['base']}")
        return
    r["busy"] = "rebasing…"
    app.render_list()
    app.run_worker(lambda: _do_rebase(app, r), thread=True)


def _do_rebase(app, r: dict):
    try:
        sync_prs.rebase_update(r["node_id"])
        app.call_from_thread(
            app.set_status,
            f"#{r['number']}: rebased onto {r['base']} — press s to re-sync",
        )
        r["behind_by"] = 0
    except Exception as e:
        app.call_from_thread(app.set_status, f"#{r['number']}: rebase failed — {e}")
    r["busy"] = ""
    app.call_from_thread(app.render_list)


@command("open_pr", "open browser", palette=False)
def open_pr(app):
    r = app.current()
    if r:
        webbrowser.open(r["url"])


@command("copy_branch", "copy branch name", palette=False)
def copy_branch(app):
    r = app.current()
    if not r:
        return
    try:
        subprocess.run(["pbcopy"], input=r["branch"], text=True, check=True)
        app.set_status(f"#{r['number']}: branch copied")
    except Exception as e:
        app.set_status(f"#{r['number']}: copy failed — {e}")


@command("copy_config_path", "copy config path", palette=False)
def copy_config_path(app):
    try:
        path = os.path.abspath(config.__file__)
        subprocess.run(["pbcopy"], input=path, text=True, check=True)
        app.set_status("config path copied")
    except Exception as e:
        app.set_status(f"config path copy failed — {e}")


@command("show_config", "show config")
def show_config(app):
    app.show_config()


# ---------------- custom commands


def run_custom(app, name: str):
    r = app.current()
    custom_commands = getattr(config, "CUSTOM_COMMANDS", {})
    if not r:
        return
    if name not in custom_commands:
        app.set_status(f"unknown custom command: {name}")
        return
    command = custom_commands[name].replace("{branch}", shlex.quote(r["branch"]))
    app.set_status(f"running {name} for #{r['number']}…")
    app.run_worker(lambda: _run_custom_command(app, r, name, command), thread=True)


def _run_custom_command(app, r, name: str, command: str):
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode:
            detail = result.stderr.strip() or f"exit status {result.returncode}"
            msg = f"#{r['number']}: {name} failed — {detail}"
        else:
            msg = f"#{r['number']}: {name} completed"
    except Exception as e:
        msg = f"#{r['number']}: {name} failed — {e}"
    app.call_from_thread(app.set_status, msg)


# ---------------- app


@command("palette", "command palette", palette=False)
def palette(app):
    app.open_palette()


@command("quit", "quit")
def quit(app):
    app.exit()
