"""Attachment-point placement v2 (pure numpy): see `api` for the entry points and SPEC section 5."""
from . import curves, frames, geodesic, groups, mesh, quality, regions, sample, weights  # noqa: F401
from .api import (generate, matrices, named_matrices, place_on_head, place_on_head_full,  # noqa: F401
                  point_name, prepare)
from .groups import DEFAULTS, family_of, mirror_of  # noqa: F401
