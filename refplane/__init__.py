"""Reference-plane continuity check for KiCad boards (standard library only)."""

from .check import Config, Finding, guess_ref_nets, guess_roles, parse_roles, run
from .pcb import read_board
from .project import load_netclasses

__all__ = ["Config", "Finding", "guess_ref_nets", "guess_roles", "load_netclasses", "parse_roles",
           "read_board", "run"]
