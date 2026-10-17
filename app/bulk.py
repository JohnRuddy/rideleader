"""Bulk include/exclude actions for waypoint candidates. Pure functions over objects with
.kind .included .pinned .offset_m .s_m; the caller commits.

Pinned waypoints are never excluded by the filtering actions (within / max_n / one_per_km),
so a leader's explicit choice survives a bulk trim. 'remove_all' is an explicit request and
excludes everything in the category.
"""

ACTIONS = {"add_all", "remove_all", "within", "max_n", "one_per_km", "only_open"}


def _set(w, value: bool) -> int:
    if w.included == value:
        return 0
    w.included = value
    return 1


def apply_bulk(wps, action: str, kind: str, value: float | None = None, open_ids: set | None = None) -> int:
    """Returns the number of waypoints whose `included` flag changed."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    pool = [w for w in wps if w.kind == kind]
    changed = 0
    if action == "add_all":
        return sum(_set(w, True) for w in pool)
    if action == "remove_all":
        return sum(_set(w, False) for w in pool)
    if action == "only_open":
        # select exactly the stops open on arrival for every pace group; pinned ones are never deselected
        if open_ids is None:
            raise ValueError("only_open needs opening-hours results")
        return sum(_set(w, True) if w.id in open_ids else (0 if w.pinned else _set(w, False)) for w in pool)
    if value is None or value <= 0:
        raise ValueError(f"{action} needs a positive value")
    if action == "within":
        # select exactly the candidates within `value` metres of the route
        for w in pool:
            ok = (w.offset_m or 0) <= value
            changed += _set(w, True) if ok else (0 if w.pinned else _set(w, False))
        return changed
    included = [w for w in pool if w.included]
    if action == "max_n":
        keep = sorted(included, key=lambda w: (w.offset_m or 0, w.s_m))[: int(value)]
        for w in included:
            if w not in keep and not w.pinned:
                changed += _set(w, False)
        return changed
    # one_per_km: bin by distance along route, keep the one closest to the route in each bin
    bins: dict[int, list] = {}
    for w in included:
        bins.setdefault(int(w.s_m // (value * 1000)), []).append(w)
    for group in bins.values():
        best = min(group, key=lambda w: (not w.pinned, w.offset_m or 0))
        for w in group:
            if w is not best and not w.pinned:
                changed += _set(w, False)
    return changed
