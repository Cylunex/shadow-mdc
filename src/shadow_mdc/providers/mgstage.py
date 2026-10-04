import re
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, parse_date
from .html_fields import field_links, field_text, first_image_artwork, integer_minutes

# MGStage product ids carry a numeric label prefix that release names usually drop
# (GANA-2850 → 200GANA-2850). Idea from javinizer-go ``scraper/mgstage``
# ``expandMGStagePrefixes`` (MIT); we only try prefixes known for the label instead
# of a blind 7-prefix sweep, so codes that are not on MGStage cost one request.
_MGSTAGE_LABEL_PREFIXES: dict[str, tuple[str, ...]] = {
    "GANA": ("200",),
    "LUXU": ("259",),
    "ARA": ("261",),
    "DCV": ("277",),
    "MIUM": ("300",),
    "MAAN": ("300",),
    "NTK": ("300",),
    "SCUTE": ("229",),
    "ORECO": ("230",),
    "KIRAY": ("314",),
    "NAMA": ("332",),
    "SIMM": ("345",),
    "JAC": ("390",),
    "INSTV": ("413",),
    "MFC": ("435",),
    "HHH": ("451",),
}
_LETTERS_CODE = re.compile(r"^([A-Z]+)-(\d+[A-Z]?)$")


def mgstage_product_candidates(code: str) -> tuple[str, ...]:
    """``GANA-2850`` → ``("GANA-2850", "200GANA-2850")``; prefixed codes stay as-is."""

    normalized = code.strip().upper()
    candidates = [normalized]
    matched = _LETTERS_CODE.match(normalized)
    if matched:
        letters, number = matched.groups()
        candidates += [f"{prefix}{letters}-{number}" for prefix in _MGSTAGE_LABEL_PREFIXES.get(letters, ())]
    return tuple(dict.fromkeys(candidates))


class MgstageProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="mgstage",
            name="MGStage",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        requested_code, family = extract_code(requested)
        if requested_code is None or family is not ContentFamily.JAV or requested_code.startswith("FC2-"):
            return []
        root: HTMLParser | None = None
        url = ""
        product_code = ""
        for candidate in mgstage_product_candidates(requested_code):
            url = f"{self._base_url}/product/product_detail/{quote(candidate)}/"
            html = await self._get_text(self.descriptor.id, url, headers={"Cookie": "adc=1"})
            page = HTMLParser(html)
            # Unknown ids render a generic landing page (HTTP 200, no product table).
            if page.css_first(".detail_left") is None:
                continue
            root = page
            product_code = field_text(page, ("品番",)) or candidate
            break
        if root is None:
            return []
        title = first_text(root, (".common_detail_cover h1", "h1"))
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        code, family = extract_code(product_code)
        if code is None or family is not ContentFamily.JAV:
            raise ProviderError(self.descriptor.id, "parse", "valid JAV code missing")
        if code.upper().endswith(requested_code.upper()) and code.upper() != requested_code.upper():
            # 200GANA-2850 found for GANA-2850: keep the code the catalog asked for.
            code = requested_code
        runtime = integer_minutes(field_text(root, ("収録時間",)))
        plot_node = root.css_first("#introduction dd")
        plot = plot_node.text(separator="\n", strip=True) if plot_node is not None else None
        return [
            ProviderRecord(
                provider=self.descriptor.id,
                external_id=code,
                source_url=url,
                code=code,
                title=title,
                original_title=title,
                family=ContentFamily.JAV,
                release_date=parse_date(field_text(root, ("配信開始日",)) or ""),
                runtime_seconds=runtime * 60 if runtime is not None else None,
                studio=next(iter(field_links(root, ("メーカー",))), None),
                series=next(iter(field_links(root, ("シリーズ",))), None),
                plot=plot,
                actors=field_links(root, ("出演",)),
                tags=field_links(root, ("ジャンル",)),
                artwork=first_image_artwork(root, self._base_url, ("#EnlargeImage",)),
                language="ja",
            )
        ]
