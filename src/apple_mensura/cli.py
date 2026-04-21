import asyncio
import time

import niquests
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)

app = typer.Typer()
console = Console()

DOWNLOAD_URL = "https://mensura.cdn-apple.com/api/v1/gm/large"
UPLOAD_URL = "https://mensura.cdn-apple.com/api/v1/gm/slurp"


async def run_download(
    concurrency: int,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
    bytes_ref: list[int] | None = None,
) -> tuple[int, float]:

    def on_chunk(n: int) -> None:
        if bytes_ref is not None:
            bytes_ref[0] += n
            if progress is not None and task_id is not None:
                progress.update(task_id, completed=bytes_ref[0])

    async with niquests.AsyncSession() as c:
        c.trust_env = False
        start = time.perf_counter()

        async def worker() -> None:
            r = await c.get(DOWNLOAD_URL, stream=True)
            r.raise_for_status()
            async for chunk in await r.iter_content():
                on_chunk(len(chunk))

        tasks = [worker() for i in range(concurrency)]
        await asyncio.gather(*tasks)

        cost = time.perf_counter() - start
    return bytes_ref[0] if bytes_ref is not None else 0, cost


async def run_upload(
    concurrency: int,
    upload_size_mb: int,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
    bytes_ref: list[int] | None = None,
) -> tuple[int, float]:
    async def data_provider(total_mb: int, chunk_size: int = 16384):
        total_bytes = total_mb * 1024 * 1024
        bytes_sent = 0
        chunk = b"\0" * chunk_size
        while bytes_sent < total_bytes:
            yield chunk
            bytes_sent += chunk_size
            if bytes_ref is not None:
                bytes_ref[0] += chunk_size
                if progress is not None and task_id is not None:
                    progress.update(task_id, completed=bytes_ref[0])

    async with niquests.AsyncSession() as c:
        c.trust_env = False
        start = time.perf_counter()

        async def worker() -> None:
            r = await c.post(
                UPLOAD_URL,
                data=data_provider(upload_size_mb // concurrency),
                timeout=60,
            )
            r.raise_for_status()

        tasks = [worker() for i in range(concurrency)]
        await asyncio.gather(*tasks)

        cost = time.perf_counter() - start
    return bytes_ref[0] if bytes_ref is not None else 0, cost


@app.command()
def speedtest(
    download: bool = typer.Option(True),
    upload: bool = typer.Option(True),
    download_workers: int = typer.Option(1, min=1),
    upload_workers: int = typer.Option(1, min=1),
    download_timeout: float = typer.Option(10, min=0),
    upload_timeout: float = typer.Option(10, min=0),
    upload_size: int = typer.Option(100, min=1),
):
    async def _main() -> None:
        if download:
            download_progress = Progress(
                SpinnerColumn(),
                TextColumn("下载中"),
                BarColumn(bar_width=32),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeElapsedColumn(),
                console=console,
            )
            d_total_bytes, d_total_time = 0, 0.0
            d_bytes_ref = [0]
            with download_progress:
                d_task_id = download_progress.add_task("", total=None, completed=0)
                try:
                    d_total_bytes, d_total_time = await asyncio.wait_for(
                        run_download(
                            download_workers,
                            progress=download_progress,
                            task_id=d_task_id,
                            bytes_ref=d_bytes_ref,
                        ),
                        download_timeout,
                    )
                except asyncio.TimeoutError:
                    d_total_bytes = d_bytes_ref[0]
                    d_total_time = download_timeout
                d_mbps = (d_total_bytes * 8) / d_total_time / 1_000_000

            line = f"[green]下载:[/green] {d_mbps:.2f} Mbps"
            console.print(line)
            console.print()

        if upload:
            upload_progress = Progress(
                SpinnerColumn(),
                TextColumn("上传中"),
                BarColumn(bar_width=32),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeElapsedColumn(),
                console=console,
            )
            u_total_bytes, u_total_time = 0, 0.0
            u_bytes_ref = [0]
            with upload_progress:
                u_task_id = upload_progress.add_task("", total=None, completed=0)
                try:
                    u_total_bytes, u_total_time = await asyncio.wait_for(
                        run_upload(
                            concurrency=upload_workers,
                            upload_size_mb=upload_size,
                            progress=upload_progress,
                            task_id=u_task_id,
                            bytes_ref=u_bytes_ref,
                        ),
                        upload_timeout,
                    )
                except asyncio.TimeoutError:
                    u_total_bytes = u_bytes_ref[0]
                    u_total_time = upload_timeout
                    upload_progress.update(u_task_id, completed=u_total_bytes)
            if u_total_time > 0:
                if u_total_bytes == 0:
                    console.print("[yellow]上传: 无有效数据[/yellow]")
                else:
                    u_mbps = (u_total_bytes * 8) / u_total_time / 1_000_000
                    line = f"[green]上传:[/green] {u_mbps:.2f} Mbps"
                    console.print(line)

    asyncio.run(_main())
