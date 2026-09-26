"""Compatibility name for measurements created before `adaptive_bep`.

Existing configurations and checkpoints can continue to use the `journal`
method name. New measurements should use `adaptive_bep`.
"""

from strategies.adaptive_bep import Strategy, load_frame
