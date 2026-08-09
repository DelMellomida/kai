"""
Centralized, tunable configuration for Kai.

One file per subsystem — servo, tracking, gesture, camera, voice, rag. Each holds only the
field-tunable knobs (the values you'd realistically adjust on-device), with the explanatory
comments kept next to them. Structural/implementation constants that are coupled to code
correctness (landmark indices, the 3D pose model, wire-format sizes, dashboard status strings,
model-coupled prefixes) deliberately stay in their source modules — they are not "config".

Each source module re-imports its knobs from here, so this package is the single source of
truth. To retune: edit the value, then restart the affected process. See config/README.md.
"""
