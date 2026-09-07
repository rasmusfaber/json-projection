from collections.abc import Iterable, Mapping
from typing import Protocol

__version__: str

Spec = Iterable[str] | Mapping[str, bool | Spec]
"""A keep-spec: keys to keep (each kept whole), or a mapping of key ->
True | nested spec | {"__all__": spec}."""

Data = bytes | bytearray | memoryview | str

class _BinaryReader(Protocol):
    def read(self, size: int, /) -> bytes: ...

class Projection:
    """A compiled keep-spec. Build once, apply to many documents."""

    def __init__(self, spec: Spec) -> None: ...
    def apply(self, data: Data, *, strict: bool = False) -> bytes: ...
    def __call__(self, data: Data, *, strict: bool = False) -> bytes: ...
    def stream(self) -> ProjectionStream: ...
    def apply_stream(self, source: _BinaryReader, *, chunk_size: int = 65536) -> bytes: ...

class ProjectionStream:
    """Strict, single-document stream. Create with Projection.stream()."""

    def feed(self, chunk: bytes) -> None: ...
    def finish(self) -> bytes: ...

def project(data: Data, spec: Spec, *, strict: bool = False) -> bytes:
    """Return `data` with every member not named in `spec` removed.

    Kept members are copied verbatim. On a JSON error or a non-object root the input is
    returned unchanged unless `strict` is true, in which case ValueError is raised.
    """
