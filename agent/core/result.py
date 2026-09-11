"""``Result[T, E]`` monad for explicit error handling without exceptions.

``Ok`` / ``Err`` are frozen, slotted, and covariant in their payload. Module
functions use ``TypeGuard`` so ``if is_ok(r):`` narrows to ``Ok[T]`` for mypy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, NoReturn, TypeGuard

from agent.core.errors import AgentError


@dataclass(frozen=True, slots=True)
class Ok[T]:
    value: T

    def is_ok(self) -> Literal[True]:
        return True

    def is_err(self) -> Literal[False]:
        return False

    def unwrap(self) -> T:
        return self.value

    def unwrap_or(self, default: T) -> T:
        return self.value

    def unwrap_or_else(self, fn: Callable[[AgentError], T]) -> T:
        return self.value

    def map[U](self, fn: Callable[[T], U]) -> Ok[U]:
        return Ok(fn(self.value))

    def map_err[E2: Exception](self, fn: Callable[[Exception], E2]) -> Ok[T]:
        return self

    def and_then[U, E: Exception](self, fn: Callable[[T], Ok[U] | Err[E]]) -> Ok[U] | Err[E]:
        return fn(self.value)

    def unwrap_err(self) -> NoReturn:
        raise ValueError(f"unwrap_err called on Ok({self.value!r})")

    def expect(self, message: str) -> T:
        return self.value


@dataclass(frozen=True, slots=True)
class Err[E: Exception]:
    error: E

    def is_ok(self) -> Literal[False]:
        return False

    def is_err(self) -> Literal[True]:
        return True

    def unwrap(self) -> NoReturn:
        raise self.error

    def unwrap_or[T](self, default: T) -> T:
        return default

    def unwrap_or_else[T](self, fn: Callable[[E], T]) -> T:
        return fn(self.error)

    def map[U](self, fn: Callable[[object], U]) -> Err[E]:
        return self

    def map_err[E2: Exception](self, fn: Callable[[E], E2]) -> Err[E2]:
        return Err(fn(self.error))

    def and_then[U](self, fn: Callable[[object], Ok[U] | Err[E]]) -> Err[E]:
        return self

    def unwrap_err(self) -> E:
        return self.error

    def expect(self, message: str) -> NoReturn:
        raise type(self.error)(message) from self.error


type Result[T, E: Exception] = Ok[T] | Err[E]


def is_ok[T, E: Exception](r: Result[T, E]) -> TypeGuard[Ok[T]]:
    return isinstance(r, Ok)


def is_err[T, E: Exception](r: Result[T, E]) -> TypeGuard[Err[E]]:
    return isinstance(r, Err)


def ok[T](value: T) -> Ok[T]:
    return Ok(value)


def err[E: Exception](error: E) -> Err[E]:
    return Err(error)


def collect[T, E: Exception](results: list[Result[T, E]]) -> Result[list[T], E]:
    """Short-circuiting sequence: first ``Err`` wins, else all ``Ok`` values."""
    values: list[T] = []
    for r in results:
        if is_err(r):
            return r
        values.append(r.value)
    return Ok(values)
