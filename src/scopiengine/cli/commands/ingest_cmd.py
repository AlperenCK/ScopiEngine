"""``scopi ingest`` — stream a log file into an index, with checkpointed resume.

``file`` runs one ingestion to completion (or until ``--follow`` is stopped);
``status``/``reset`` inspect and clear the checkpoints it leaves behind. See
``docs/INGEST.md`` for the full resume/rotation/tuning story.
"""

from __future__ import annotations

import dataclasses
import signal
import threading
from pathlib import Path
from typing import Annotated

import typer

from scopiengine.cli.output import echo, error, print_json, print_table
from scopiengine.engine import Engine
from scopiengine.ingest.checkpoint import FromMode
from scopiengine.ingest.pipeline import IngestOptions, IngestPipeline, IngestStats
from scopiengine.ingest.sources.local import LocalFileSource
from scopiengine.storage.factory import open_storage
from scopiengine.storage.models import Checkpoint

__all__ = ["app"]

app = typer.Typer(
    name="ingest",
    help="Stream large log files into an index, with checkpointed resume.",
    no_args_is_help=True,
)

_FROM_CHOICES = ("start", "end", "checkpoint")
_ID_MODE_CHOICES = ("offset", "content", "uuid")


def _checkpoint_row(cp: Checkpoint) -> tuple[str, str, str, int, int, str, str]:
    return (
        cp.cp_key,
        cp.index_name,
        cp.source_uri,
        cp.byte_offset,
        cp.record_count,
        cp.state,
        cp.updated_at,
    )


def _checkpoint_dict(cp: Checkpoint) -> dict[str, object]:
    return dataclasses.asdict(cp)


def _progress_line(stats: IngestStats) -> str:
    eta = stats.eta_seconds
    eta_text = f"{eta:.0f}s" if eta is not None else "unknown"
    size_text = str(stats.file_size) if stats.file_size else "?"
    return (
        f"ingest: {stats.bytes_read}/{size_text} bytes ({stats.bytes_per_second / 1024:.1f} KiB/s)"
        f" | {stats.docs_indexed} docs ({stats.docs_per_second:.1f}/s)"
        f" | queue={stats.queue_depth} segments={stats.segment_count} eta={eta_text}"
    )


@app.command("file")
def ingest_file(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Path to the log file to ingest.")],
    index: Annotated[str, typer.Option("--index", help="Target index name.")],
    follow: Annotated[
        bool,
        typer.Option("--follow", help="Keep polling for appended data instead of exiting at EOF."),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume", help="Continue from the saved checkpoint (shorthand for --from checkpoint)."
        ),
    ] = False,
    from_: Annotated[
        str | None,
        typer.Option("--from", help="start|end|checkpoint. Overrides --resume when given."),
    ] = None,
    processor: Annotated[
        list[str] | None,
        typer.Option("--processor", help="Named ingest processor to apply, in order; repeatable."),
    ] = None,
    multiline_start: Annotated[
        str | None,
        typer.Option(
            "--multiline-start", help="Regex matching a new record's first line (folds the rest)."
        ),
    ] = None,
    batch_size: Annotated[
        int | None, typer.Option("--batch-size", help="Documents per flush.")
    ] = None,
    batch_bytes: Annotated[
        int | None, typer.Option("--batch-bytes", help="Approximate bytes per flush.")
    ] = None,
    buffer_mb: Annotated[
        int | None,
        typer.Option("--buffer-mb", help="Index write-buffer size before an extra segment flush."),
    ] = None,
    queue_size: Annotated[
        int | None, typer.Option("--queue", help="Bounded queue depth between reader and indexer.")
    ] = None,
    id_mode: Annotated[str, typer.Option("--id-mode", help="offset|content|uuid")] = "offset",
    regex_pattern: Annotated[
        str | None,
        typer.Option(
            "--regex-pattern", help="Named-group pattern for the regex_extract processor."
        ),
    ] = None,
    flush_interval: Annotated[
        float | None,
        typer.Option("--flush-interval", help="Seconds before a partial batch flushes anyway."),
    ] = None,
    quiet_progress: Annotated[
        bool, typer.Option("--no-progress", help="Suppress periodic progress lines on stderr.")
    ] = False,
) -> None:
    """Ingest ``path`` into ``--index``, checkpointing as it goes."""
    if from_ is not None and from_ not in _FROM_CHOICES:
        raise typer.BadParameter(f"--from must be one of {_FROM_CHOICES}")
    if id_mode not in _ID_MODE_CHOICES:
        raise typer.BadParameter(f"--id-mode must be one of {_ID_MODE_CHOICES}")
    if not path.exists():
        raise typer.BadParameter(f"no such file: {path}")

    resolved_from: FromMode = from_ if from_ is not None else ("checkpoint" if resume else "start")  # type: ignore[assignment]

    settings = ctx.obj.settings
    if buffer_mb is not None:
        settings = dataclasses.replace(settings, buffer_mb=buffer_mb)

    options = IngestOptions(
        index=index,
        processors=tuple(processor) if processor else (),
        multiline_start=multiline_start,
        follow=follow,
        from_mode=resolved_from,
        id_mode=id_mode,  # type: ignore[arg-type]
        batch_size=batch_size,
        batch_bytes=batch_bytes,
        flush_interval=flush_interval,
        queue_size=queue_size,
        regex_pattern=regex_pattern,
    )

    stop_event = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    previous_sigint = signal.signal(signal.SIGINT, _stop)
    previous_sigterm = signal.signal(signal.SIGTERM, _stop)
    try:
        with Engine.open(settings) as engine:
            pipeline = IngestPipeline(engine, LocalFileSource(path), options)
            reporter = None if quiet_progress else lambda stats: error(_progress_line(stats))
            stats = pipeline.run(stop_event=stop_event, progress=reporter)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)

    if ctx.obj.json_output:
        print_json(
            {
                "index": index,
                "state": stats.state,
                "bytes_read": stats.bytes_read,
                "records_read": stats.records_read,
                "docs_indexed": stats.docs_indexed,
                "docs_dropped": stats.docs_dropped,
                "truncated_lines": stats.truncated_lines,
                "batches_flushed": stats.batches_flushed,
                "checkpoint": _checkpoint_dict(stats.checkpoint) if stats.checkpoint else None,
            }
        )
    else:
        echo(
            f"ingest {stats.state}: {stats.docs_indexed} docs, {stats.bytes_read} bytes,"
            f" {stats.batches_flushed} batch(es), {stats.truncated_lines} truncated line(s)"
        )


@app.command("status")
def status(
    ctx: typer.Context,
    index: Annotated[str | None, typer.Option("--index", help="Restrict to one index.")] = None,
) -> None:
    """List ingestion checkpoints, newest first."""
    with open_storage(ctx.obj.settings.storage) as backend:
        checkpoints = backend.list_checkpoints(index)
    if ctx.obj.json_output:
        print_json([_checkpoint_dict(cp) for cp in checkpoints])
    else:
        print_table(
            ["cp_key", "index", "source", "offset", "records", "state", "updated_at"],
            [_checkpoint_row(cp) for cp in checkpoints],
        )


@app.command("reset")
def reset(ctx: typer.Context, cp_key: str) -> None:
    """Delete a checkpoint, so its next run starts fresh at byte 0."""
    with open_storage(ctx.obj.settings.storage) as backend:
        existing = backend.load_checkpoint(cp_key)
        if existing is None:
            error(f"no checkpoint found for key {cp_key!r}")
            raise typer.Exit(code=1)
        backend.delete_checkpoint(cp_key)
    if ctx.obj.json_output:
        print_json({"cp_key": cp_key, "result": "deleted"})
    else:
        echo(f"deleted checkpoint {cp_key!r}")
