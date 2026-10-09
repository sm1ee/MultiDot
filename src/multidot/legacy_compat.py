"""Explicit compatibility defaults for the original B/C/A example topology.

New deployments should provide a worker registry. These IDs are used only when
the caller omits that registry; display names never determine a queue or role.
"""

LEGACY_CONTROLLER_ACTOR = "controller-a"


def legacy_workers():
    """Return a fresh registry for pre-registry configs and example fixtures."""
    return [
        {"id": "dot-b", "name": "dot-b", "role": "worker"},
        {"id": "dot-c", "name": "dot-c", "role": "worker"},
        {"id": "dot-a", "name": "dot-a", "role": "synthesis"},
    ]
