import asyncio

from app.media_stream import file_iterator


def test_file_iterator_yields_requested_inclusive_range(tmp_path) -> None:
    path = tmp_path / "video.bin"
    path.write_bytes(bytes(range(256)))

    async def collect() -> bytes:
        return b"".join([chunk async for chunk in file_iterator(path, 10, 24)])

    assert asyncio.run(collect()) == bytes(range(10, 25))
