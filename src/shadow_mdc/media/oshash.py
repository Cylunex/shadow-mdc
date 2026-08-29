import struct
from pathlib import Path

_CHUNK_SIZE = 64 * 1024
_MASK = 0xFFFFFFFFFFFFFFFF


def compute_oshash(path: str | Path) -> str | None:
    file_path = Path(path)
    size = file_path.stat().st_size
    if size < _CHUNK_SIZE * 2:
        return None
    value = size
    with file_path.open("rb") as stream:
        for offset in (0, size - _CHUNK_SIZE):
            stream.seek(offset)
            chunk = stream.read(_CHUNK_SIZE)
            if len(chunk) != _CHUNK_SIZE:
                return None
            for (number,) in struct.iter_unpack("<Q", chunk):
                value = (value + number) & _MASK
    return f"{value:016x}"
