"""Beta and subscription access gate (specs/005-beta-subscription-access).

``policy`` decides who has access, ``repo`` loads and records access rows, ``gate`` is the
control-bot middleware and ``reconcile`` suspends or restores users whose access changed.
"""
