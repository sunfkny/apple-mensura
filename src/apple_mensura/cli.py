import asyncio
import math
import re
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from decimal import Decimal

import httpx2
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


def parse_quantity(
    value: str, units: dict[str, int], default_unit: str, option: str
) -> Decimal:
    match = re.fullmatch(r"\s*(\d+(?:\.\d*)?|\.\d+)\s*([a-zA-Z]*)\s*", value)
    if match is None:
        raise typer.BadParameter("请输入正数和支持的单位", param_hint=option)
    unit = match[2].lower() or default_unit
    if unit not in units:
        raise typer.BadParameter("不支持该单位", param_hint=option)
    quantity = Decimal(match[1]) * units[unit]
    if quantity <= 0:
        raise typer.BadParameter("必须大于 0", param_hint=option)
    return quantity


def parse_size(value: str | None, option: str) -> int | None:
    if value is None:
        return None
    units = {
        alias: 1024**power
        for power, aliases in (
            (2, ("m", "mb", "mib")),
            (3, ("g", "gb", "gib")),
            (4, ("t", "tb", "tib")),
        )
        for alias in aliases
    }
    size = int(parse_quantity(value, units, "mb", option))
    if size < 1:
        raise typer.BadParameter("大小不能小于 1 字节", param_hint=option)
    return size


def parse_duration(value: str | None, option: str) -> float | None:
    if value is None:
        return None
    duration = float(
        parse_quantity(value, {"s": 1, "m": 60, "h": 3600, "d": 86400}, "s", option)
    )
    if not math.isfinite(duration) or duration <= 0:
        raise typer.BadParameter("时间超出支持范围", param_hint=option)
    return duration


def worker_size(size: int | None, concurrency: int, index: int) -> int | None:
    if size is None:
        return None
    quotient, remainder = divmod(size, concurrency)
    return quotient + (index < remainder)


async def run_workers(
    concurrency: int,
    worker: Callable[[int], Coroutine[None, None, None]],
    duration: float | None,
    transferred: list[int],
    progress: Progress | None,
    task_id: TaskID | None,
) -> float:
    async def update_progress() -> None:
        while True:
            if progress is not None and task_id is not None:
                progress.update(task_id, completed=transferred[0])
            await asyncio.sleep(0.1)

    start = time.perf_counter()
    deadline = asyncio.timeout(duration)
    updater = (
        asyncio.create_task(update_progress())
        if progress is not None and task_id is not None
        else None
    )
    try:
        async with deadline, asyncio.TaskGroup() as group:
            for index in range(concurrency):
                group.create_task(worker(index))
    except TimeoutError:
        if not deadline.expired():
            raise
    finally:
        if updater is not None:
            updater.cancel()
            await asyncio.gather(updater, return_exceptions=True)
        if progress is not None and task_id is not None:
            progress.update(task_id, completed=transferred[0])
    return time.perf_counter() - start


async def run_download(
    concurrency: int,
    size: int | None = None,
    duration: float | None = None,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
    bytes_ref: list[int] | None = None,
) -> tuple[int, float]:
    transferred = bytes_ref if bytes_ref is not None else [0]
    async with httpx2.AsyncClient(
        trust_env=False, follow_redirects=True, timeout=None
    ) as client:

        async def worker(index: int) -> None:
            remaining = worker_size(size, concurrency, index)
            while remaining is None or remaining > 0:
                received = 0
                async with client.stream("GET", DOWNLOAD_URL) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        count = len(chunk)
                        if remaining is not None:
                            count = min(count, remaining)
                            remaining -= count
                        received += count
                        transferred[0] += count
                        if remaining == 0:
                            return
                if received == 0:
                    raise RuntimeError("下载响应为空，无法继续测速")

        elapsed = await run_workers(
            concurrency, worker, duration, transferred, progress, task_id
        )
    return transferred[0], elapsed


async def run_upload(
    concurrency: int,
    size: int | None = None,
    duration: float | None = None,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
    bytes_ref: list[int] | None = None,
) -> tuple[int, float]:
    transferred = bytes_ref if bytes_ref is not None else [0]

    class UploadComplete(Exception):
        """请求体发送完毕，关闭连接并结束上传计时。"""

    async def trace(event: str, info: dict[str, object]) -> None:
        if size is not None and event in (
            "http11.send_request_body.complete",
            "http2.send_request_body.complete",
        ):
            raise UploadComplete

    async def data_provider(total_bytes: int | None) -> AsyncIterator[bytes]:
        chunk = b"\0" * 16384
        remaining = total_bytes
        while remaining is None or remaining > 0:
            data = chunk if remaining is None else chunk[:remaining]
            yield data
            count = len(data)
            if remaining is not None:
                remaining -= count
            transferred[0] += count

    async with httpx2.AsyncClient(
        trust_env=False, follow_redirects=True, timeout=None
    ) as client:

        async def worker(index: int) -> None:
            total_bytes = worker_size(size, concurrency, index)
            if total_bytes == 0:
                return
            try:
                response = await client.post(
                    UPLOAD_URL,
                    content=data_provider(total_bytes),
                    extensions={"trace": trace},
                )
                response.raise_for_status()
            except UploadComplete:
                pass

        elapsed = await run_workers(
            concurrency, worker, duration, transferred, progress, task_id
        )
    return transferred[0], elapsed


@app.command()
def speedtest(
    download: bool = typer.Option(True),
    upload: bool = typer.Option(True),
    download_workers: int = typer.Option(8, min=1),
    upload_workers: int = typer.Option(8, min=1),
    download_timeout: str | None = typer.Option(
        None, "--download-timeout", "--download-time", help="下载时间上限，单位 s/m/h/d"
    ),
    upload_timeout: str | None = typer.Option(
        None, "--upload-timeout", "--upload-time", help="上传时间上限，单位 s/m/h/d"
    ),
    upload_size: str | None = typer.Option(
        None, help="上传合计大小上限，单位 MB/GB/TB（M/MiB 等价）"
    ),
    download_size: str | None = typer.Option(
        None, help="下载合计大小上限，单位 MB/GB/TB（M/MiB 等价）"
    ),
):
    d_timeout = parse_duration(download_timeout, "--download-timeout")
    u_timeout = parse_duration(upload_timeout, "--upload-timeout")
    d_size = parse_size(download_size, "--download-size")
    u_size = parse_size(upload_size, "--upload-size")
    if d_timeout is None and d_size is None:
        d_timeout, d_size = 10.0, 512 * 1024**2
    if u_timeout is None and u_size is None:
        u_timeout, u_size = 10.0, 64 * 1024**2

    async def measure(
        label: str,
        is_download: bool,
        workers: int,
        size: int | None,
        duration: float | None,
    ) -> None:
        progress = Progress(
            SpinnerColumn(),
            TextColumn(f"{label}中"),
            BarColumn(bar_width=32),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            console=console,
            speed_estimate_period=3.0,
        )
        operation = run_download if is_download else run_upload
        with progress:
            task_id = progress.add_task("", total=size, completed=0)
            total_bytes, elapsed = await operation(
                workers, size, duration, progress=progress, task_id=task_id
            )
        if total_bytes == 0 or elapsed <= 0:
            console.print(f"[yellow]{label}: 无有效数据[/yellow]")
        else:
            mbps = total_bytes * 8 / elapsed / 1_000_000
            console.print(f"[green]{label}:[/green] {mbps:.2f} Mbps")

    async def main() -> None:
        if download:
            await measure("下载", True, download_workers, d_size, d_timeout)
            console.print()
        if upload:
            await measure("上传", False, upload_workers, u_size, u_timeout)

    asyncio.run(main())
