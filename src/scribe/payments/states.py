"""FSM states of the payments flows. Kept apart from the routers so the access gate can name
them without importing the handlers (specs/015 T015)."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PaySupportStates(StatesGroup):
    describe = State()  # /paysupport: waiting for the person's description (research P11)
