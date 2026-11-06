"""Revision of the file selected by the default reader endpoint."""
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def file_rev(work) -> str:
    from .routers.reader import _path_sha1

    path = work.converted_path if work.converted_status == "ready" and work.converted_path and Path(work.converted_path).exists() else work.file_path
    if not path and work.site == "calibre" and work.calibre_id:
        from .config import CALIBRE_CACHE_DIR

        path = str(CALIBRE_CACHE_DIR / f"{work.calibre_id}.{(work.file_format or 'epub').lower()}")
    if not path:
        return ""
    try:
        return _path_sha1(Path(path))
    except OSError as exc:
        log.warning("Cannot read revision for work %s (%s): %s", work.id, path, exc)
        return ""
