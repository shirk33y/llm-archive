from __future__ import annotations


BASE53 = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz"


def to_base53(num: int) -> str:
    if num == 0:
        return BASE53[0]
    digits = []
    while num:
        digits.append(BASE53[num % 53])
        num //= 53
    return "".join(reversed(digits))


def from_base53(s: str) -> int:
    result = 0
    for char in s:
        result = result * 53 + BASE53.index(char)
    return result
