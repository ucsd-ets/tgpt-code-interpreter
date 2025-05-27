# Copyright 2024 IBM Corp.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations
from typing import Annotated, TypeAliasType

import re
from datetime import timedelta
from pydantic import Field

Hash = TypeAliasType("Hash", Annotated[str, Field(pattern=r"^[0-9a-zA-Z_-]{1,255}$")])
AbsolutePath = TypeAliasType(
    "AbsolutePath", Annotated[str, Field(pattern=r"^/[^/].*$")]
)

_DURATION_RX = re.compile(r"\s*(?P<num>\d+)\s*(?P<unit>[smhdw])\s*$", re.I)
_UNIT_KW = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_duration(spec: str | None) -> timedelta | None:
    if not spec:
        return None

    m = _DURATION_RX.fullmatch(spec)
    if not m:
        raise ValueError(f"Unsupported duration literal: {spec!r}")

    value = int(m.group("num"))
    kwarg = _UNIT_KW[m.group("unit").lower()]
    return timedelta(**{kwarg: value})
