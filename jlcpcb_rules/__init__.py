"""JLCPCB manufacturing capabilities as KiCad 10 custom design rules."""

from .generator import render, render_file
from .profile import Profile, load_capabilities

__all__ = ["Profile", "load_capabilities", "render", "render_file"]
