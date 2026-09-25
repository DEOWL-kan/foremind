"""The supervisor (DESIGN §1.1, §10): `tick` runs one pass, `ready` the ready queue and waits, `stuck` stuck
detection and takeover, `quota` the quota state machine. It never calls a model to judge anything."""
