"""Release markers explicitly present in a media filename, independent of providers."""

import re
from pathlib import PurePath, PureWindowsPath

_LTX = re.compile(r"(?i)[._-]LTX(?=$|[._\s-])")


def filename_tags(path: str | PurePath) -> tuple[str, ...]:
    """Recognize a delimited LTX suffix; never infer it from a directory or code prefix."""

    # Handle stored UNC/Windows paths even when the service runs on Linux.
    name = PureWindowsPath(str(path)).name
    stem = PurePath(name).stem
    return ("LTX",) if _LTX.search(stem) else ()
