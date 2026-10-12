"""`ratings` — the one command MoveInSync needs.

ratings refresh     download new reviews, tag them, rebuild the report and open it
ratings doctor      check the setup (key, internet, data) and say what to fix
ratings recommend   build the recommendation, backlog exports and reply playbook
"""

from __future__ import annotations

import os
import sys
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskID, TextColumn, TimeElapsedColumn
from rich.table import Table

from .config import Settings, load_settings
from .store import Store
from .taxonomy import Taxonomy, load_taxonomy

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Play Store ratings monitor for the MoveInSync employee app.",
)
console = Console()


def _ctx() -> tuple[Settings, Taxonomy, Store]:
    settings = load_settings()
    tax = load_taxonomy(settings.root / "taxonomy.yaml")
    return settings, tax, Store(settings.database)


def _step(n: int, total: int, text: str) -> None:
    console.print(f"\n[bold cyan]Step {n}/{total}[/] {text}")


# ── fetch ─────────────────────────────────────────────────────────────────────────────────────
def do_fetch(settings: Settings, store: Store, full: bool = False) -> tuple[int, int]:
    from .fetch import fetch_listing, fetch_reviews

    listing = fetch_listing(store, settings.app)
    console.print(
        f"  Store rating now: [bold]{listing['score']:.2f}★[/] from {listing['ratings']:,} ratings "
        f"(version {listing.get('version')})"
    )
    first_run = store.review_count(settings.app.id) == 0
    with Progress(
        TextColumn("  {task.description}"),
        TextColumn("[dim]{task.fields[back_to]}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as bar:
        task = bar.add_task("Reviews read: 0", back_to="")

        def progress(n: int, oldest: str) -> None:
            bar.update(task, description=f"Reviews read: {n:,}", back_to=f"back to {oldest}")

        res = fetch_reviews(store, settings.app, full=full, progress=progress)
    console.print(
        f"  {'Downloaded the full review history' if first_run or full else 'Checked recent reviews'}: "
        f"[bold]{res.new:,}[/] new, {res.updated:,} edited since last run."
    )

    year_ago = datetime.now(UTC) - timedelta(days=settings.analysis.comparison_days + 30)
    for ref in [*settings.benchmarks, *settings.sibling_apps]:
        try:
            fetch_listing(store, ref)
            if ref in settings.benchmarks:
                fetch_reviews(store, ref, since=year_ago)
        except Exception as e:  # a benchmark failing must never stop the main report
            console.print(f"  [yellow]Skipped {ref.name}: {e}[/]")
    console.print(f"  Comparison apps updated ({len(settings.benchmarks) + len(settings.sibling_apps)}).")
    return res.new, res.updated


# ── tag ───────────────────────────────────────────────────────────────────────────────────────
def do_tag(
    settings: Settings, tax: Taxonomy, store: Store, models: list[str] | None = None, limit: int | None = None
) -> int:
    from .tagger import ReviewIn, Tagger, tag_reviews

    rows = store.untagged(settings.app.id, settings.tag_since, tax.version)
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print("  Every review is already tagged.")
        return 0
    taggers = [Tagger(settings.llm, tax, model=m) for m in (models or settings.llm.models)]
    items = [ReviewIn(r["review_id"], r["score"], r["content"], r["content_hash"]) for r in rows]
    console.print(
        f"  {len(items):,} reviews to tag with {len(taggers)} model{'s' if len(taggers) > 1 else ''}: "
        + ", ".join(f"[bold]{t.model}[/]" for t in taggers)
    )
    with Progress(
        TextColumn("  {task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as bar:
        tasks: dict[str, TaskID] = {}

        def on_progress(model: str, done: int, total: int) -> None:
            if model not in tasks:
                tasks[model] = bar.add_task(model.split("/")[-1], total=total)
            bar.update(tasks[model], completed=done, total=total)

        tagged = tag_reviews(
            items,
            taggers,
            taxonomy_version=tax.version,
            cache=store,
            on_progress=on_progress,
            strategy=settings.llm.strategy,
        )
    saved = store.save_tags(tagged)
    calls = sum(t.requests for t in taggers)
    console.print(f"  Tagged [bold]{saved:,}[/] reviews ({calls:,} model requests).")
    if saved < len(items):
        console.print(f"  [yellow]{len(items) - saved} could not be tagged this time; the next run retries them.[/]")
    return saved


# ── report ────────────────────────────────────────────────────────────────────────────────────
def do_report(settings: Settings, tax: Taxonomy, store: Store) -> Path:
    from .analysis import analyse
    from .report import render_report

    data = analyse(settings, tax, store)
    path = render_report(settings, tax, data)
    return path


# ── commands ──────────────────────────────────────────────────────────────────────────────────
@app.command()
def refresh(
    open_report: Annotated[bool, typer.Option("--open/--no-open", help="Open the report when done.")] = True,
    skip_tagging: Annotated[bool, typer.Option(help="Only download and report; don't call the model.")] = False,
) -> None:
    """Download new reviews, tag them, rebuild the report and open it. [bold]Start here.[/]"""
    settings, tax, store = _ctx()
    run = store.start_run(" + ".join(settings.llm.models))
    try:
        _step(1, 3, "Downloading the latest Play Store data")
        new, updated = do_fetch(settings, store)
        _step(2, 3, "Tagging new reviews by issue")
        tagged = 0
        if skip_tagging:
            console.print("  Skipped (--skip-tagging).")
        elif not settings.llm.api_key:
            console.print(
                f"  [yellow]No {settings.llm.api_key_env} in .env — skipping tagging. "
                "The report will reuse earlier tags. Run `ratings doctor` for help.[/]"
            )
        else:
            tagged = do_tag(settings, tax, store)
        _step(3, 3, "Building the report")
        path = do_report(settings, tax, store)
        store.finish_run(run, "ok", new_reviews=new, updated_reviews=updated, tagged=tagged)
    except KeyboardInterrupt:
        store.finish_run(run, "cancelled")
        console.print("\n[yellow]Stopped. Everything downloaded or tagged so far is saved.[/]")
        raise typer.Exit(1) from None
    except Exception as e:
        store.finish_run(run, "failed", note=str(e)[:500])
        console.print(f"\n[bold red]Refresh failed:[/] {e}\nRun [bold]ratings doctor[/] to check the setup.")
        raise typer.Exit(1) from None
    console.print(f"\n[bold green]Done.[/] Report: {path}")
    if open_report and not os.environ.get("CI"):
        webbrowser.open(path.resolve().as_uri())


@app.command()
def fetch(full: Annotated[bool, typer.Option(help="Re-download the entire history.")] = False) -> None:
    """Only download store data and reviews."""
    settings, _, store = _ctx()
    do_fetch(settings, store, full=full)


@app.command()
def tag(
    models: Annotated[list[str] | None, typer.Option("--model", help="Override the models in config.yaml.")] = None,
    limit: Annotated[int | None, typer.Option(help="Tag at most this many reviews.")] = None,
) -> None:
    """Only tag reviews that are new, edited, or tagged with an older taxonomy."""
    settings, tax, store = _ctx()
    do_tag(settings, tax, store, models=models, limit=limit)


@app.command()
def report(open_report: Annotated[bool, typer.Option("--open/--no-open")] = True) -> None:
    """Only rebuild the report from data already downloaded."""
    settings, tax, store = _ctx()
    path = do_report(settings, tax, store)
    console.print(f"Report: {path}")
    if open_report and not os.environ.get("CI"):
        webbrowser.open(path.resolve().as_uri())


@app.command()
def recommend(
    open_report: Annotated[bool, typer.Option("--open/--no-open", help="Open the recommendation when done.")] = True,
    rebaseline: Annotated[
        bool, typer.Option(help="Reset every Fix-tracker baseline in backlog.yaml to today's numbers.")
    ] = False,
    freeze: Annotated[
        bool,
        typer.Option(
            "--freeze/--no-freeze",
            help="Write missing baselines into backlog.yaml. Scheduled runs use --no-freeze so baselines never move.",
        ),
    ] = True,
) -> None:
    """Build the recommendation, backlog exports and reply playbook from backlog.yaml + the data."""
    from .analysis import analyse, load_frame
    from .recommend import freeze_baselines, load_backlog_model, write_all

    settings, tax, store = _ctx()
    path = settings.root / "backlog.yaml"
    if not path.exists():
        console.print(f"[red]No backlog.yaml in {settings.root}.[/]")
        raise typer.Exit(1)
    load_backlog_model(path, tax)  # validate before touching the file
    data = analyse(settings, tax, store)
    if freeze:
        frozen = freeze_baselines(path, data, rebaseline=rebaseline)
        if frozen:
            console.print(f"  Fix-tracker baselines written for: {', '.join(frozen)}")
    backlog = load_backlog_model(path, tax)
    paths = write_all(settings.root / "deliverables", data, tax, backlog, load_frame(store, settings.app.id))
    for p in paths.values():
        console.print(f"  {p}")
    console.print("[bold green]Done.[/] Run `ratings report` to refresh the monitor's Fix tracker.")
    if open_report and not os.environ.get("CI"):
        webbrowser.open(paths["recommendation"].resolve().as_uri())


@app.command(name="eval")
def evaluate(
    models: Annotated[list[str] | None, typer.Option("--model", help="Model(s) to compare. Repeatable.")] = None,
    which: Annotated[str | None, typer.Option("--set", help="Reference set: dev, test, or all.")] = "test",
) -> None:
    """Score model(s) — and their vote — against the reference labels."""
    from .evaluate import run_eval

    settings, tax, store = _ctx()
    run_eval(settings, tax, store, models or settings.llm.models, console, None if which == "all" else which)


@app.command()
def doctor() -> None:
    """Check the setup and explain how to fix anything missing."""
    ok = True
    table = Table(show_header=False, box=None)
    try:
        settings, tax, store = _ctx()
        table.add_row("[green]✓[/]", f"Project folder: {settings.root}")
        table.add_row("[green]✓[/]", f"Taxonomy v{tax.version}: {len(tax.codes)} issue codes")
    except Exception as e:
        console.print(f"[red]✗ {e}[/]")
        raise typer.Exit(1) from None

    if settings.llm.api_key:
        try:
            from openai import OpenAI

            client = OpenAI(base_url=settings.llm.base_url, api_key=settings.llm.api_key, timeout=60)
            from .tagger import parse_model

            model_id, extra, _ = parse_model(settings.llm.model)
            client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": "Reply OK"}],
                max_tokens=400,
                extra_body=extra or None,
            )
            table.add_row("[green]✓[/]", f"Model reachable: {settings.llm.model}")
        except Exception as e:
            ok = False
            table.add_row("[red]✗[/]", f"Model call failed: {str(e)[:200]}")
    else:
        ok = False
        table.add_row(
            "[red]✗[/]",
            f"{settings.llm.api_key_env} missing. Get a free key at https://build.nvidia.com, then add a line "
            f"{settings.llm.api_key_env}=nvapi-... to {settings.root / '.env'}",
        )
    try:
        from google_play_scraper import app as play_app

        play_app(settings.app.id, lang=settings.app.lang, country=settings.app.country)
        table.add_row("[green]✓[/]", "Google Play reachable")
    except Exception as e:
        ok = False
        table.add_row("[red]✗[/]", f"Google Play not reachable: {str(e)[:200]}")

    n = store.review_count(settings.app.id)
    table.add_row("[green]✓[/]" if n else "[yellow]•[/]", f"Reviews stored: {n:,}")
    last = store.conn.execute("SELECT finished_at, status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        table.add_row(
            "[green]✓[/]" if last["status"] == "ok" else "[yellow]•[/]",
            f"Last run: {last['finished_at']} ({last['status']})",
        )
    console.print(table)
    console.print(
        "\n[bold green]All good.[/]" if ok else "\n[bold yellow]Fix the items marked ✗, then run `ratings refresh`.[/]"
    )
    if not ok:
        raise typer.Exit(1)


def main() -> None:
    if sys.platform == "win32":  # make ★ and → print in the Windows console
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                reconfigure(encoding="utf-8")
    app()
