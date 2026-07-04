"""Data models for the users domain."""

from __future__ import annotations

from dataclasses import dataclass

# Cross-file type: referenced from service.py, repository.py, api.py.
UserId = str


@dataclass
class User:
    """A user record."""

    id: UserId
    name: str
    email: str
    active: bool = True
