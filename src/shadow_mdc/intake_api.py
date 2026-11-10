"""Supported intake HTTP routes sharing the existing domain queue and recovery logic."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from .api_models import AssetOut
from .db.models import MediaAsset
from .intake_core import IntakeRepo, require_service_token


class LibraryAssetsOut(BaseModel):
    items: list[AssetOut]
    total: int
    offset: int
    limit: int


router = APIRouter(prefix="/api/intake/v1", dependencies=[Depends(require_service_token)])

@router.get("/libraries/{library_id}/assets")
def library_assets(
    library_id: str,
    repo: IntakeRepo,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 48,
    state: str | None = None,
) -> LibraryAssetsOut:
    """Read a bounded page, including unrecognized assets produced by a scan."""
    if repo.get_library(library_id) is None:
        raise HTTPException(404, "library not found")
    filters = [MediaAsset.library_id == library_id]
    if state is not None:
        filters.append(MediaAsset.state == state)
    else:
        filters.append(MediaAsset.state != "ignored")
    total = repo._session.scalar(select(func.count()).select_from(MediaAsset).where(*filters)) or 0
    rows = repo._session.scalars(
        select(MediaAsset).where(*filters).order_by(MediaAsset.id).offset(offset).limit(limit)
    ).all()
    return LibraryAssetsOut(
        items=[AssetOut.model_validate(row) for row in rows], total=total, offset=offset, limit=limit
    )
