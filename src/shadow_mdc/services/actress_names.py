"""Compatibility re-export; the implementation lives in ``shadow_mdc.actress_aliases``.

Moved out of ``services`` so ``db.repository`` can use alias-aware actor merging
without importing the ``services`` package (circular import).
"""

from ..actress_aliases import (
    ActressNameMap as ActressNameMap,
)
from ..actress_aliases import (
    actor_identity_key as actor_identity_key,
)
from ..actress_aliases import (
    get_actress_name_map as get_actress_name_map,
)
from ..actress_aliases import (
    merge_actor_names as merge_actor_names,
)
from ..actress_aliases import (
    normalize_actress_key as normalize_actress_key,
)
